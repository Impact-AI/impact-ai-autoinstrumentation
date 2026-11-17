// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kubetest"
)

const coverKey = "impact_test_covercovercovercovercovercovercovercove1"

// syncBuffer is a buffer the code under test writes to from its own goroutines.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func closeTo(got, want time.Duration) bool {
	d := got - want
	return d > -time.Millisecond && d < time.Millisecond
}

func TestCoverUsageErrors(t *testing.T) {
	good := []string{"--ingest-endpoint=https://ingest.example.test", "--instrumentation=impact-ai/impact"}
	without := func(prefix string, extra ...string) []string {
		var out []string
		for _, a := range good {
			if !strings.HasPrefix(a, prefix) {
				out = append(out, a)
			}
		}
		return append(out, extra...)
	}
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no endpoint", without("--ingest-endpoint"), "ingest endpoint"},
		{"an endpoint that is not http(s)", without("--ingest-endpoint", "--ingest-endpoint=ftp://x"), "ingest endpoint"},
		{"an endpoint without a host", without("--ingest-endpoint", "--ingest-endpoint=https://"), "ingest endpoint"},
		{"no instrumentation", without("--instrumentation"), "instrumentation"},
		{"instrumentation without a namespace", without("--instrumentation", "--instrumentation=impact"), "instrumentation"},
		{"instrumentation with an uppercase namespace", without("--instrumentation", "--instrumentation=Impact/impact"), "instrumentation"},
		{"instrumentation with a slash too many", without("--instrumentation", "--instrumentation=a/b/c"), "instrumentation"},
		{"instrumentation with an empty name", without("--instrumentation", "--instrumentation=impact-ai/"), "instrumentation"},
		{"a poll interval that is not a duration", append(append([]string{}, good...), "--poll-interval=often"), "poll-interval"},
		{"a poll interval under the minimum", append(append([]string{}, good...), "--poll-interval=4s"), "poll-interval"},
		{"an unknown flag", append(append([]string{}, good...), "--bogus"), "bogus"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := runCover(context.Background(), tc.args, func(string) string { return "" }, &stdout, &stderr, testDeps())
			if code != 2 {
				t.Errorf("exit code = %d, want 2 (stderr: %s)", code, stderr.String())
			}
			if !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("stderr = %q, want it to mention %q", stderr.String(), tc.want)
			}
		})
	}
}

func TestCoverHelpAndVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := runCover(context.Background(), []string{"--version"}, func(string) string { return "" }, &stdout, &stderr, testDeps()); code != 0 {
		t.Errorf("--version exit code = %d (%s)", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "impact-cover ") {
		t.Errorf("--version printed %q", stdout.String())
	}
	stderr.Reset()
	if code := runCover(context.Background(), []string{"--help"}, func(string) string { return "" }, &stdout, &stderr, testDeps()); code != 0 {
		t.Errorf("--help exit code = %d", code)
	}
	for _, flag := range []string{"--ingest-endpoint", "--ingest-key-file", "--instrumentation", "--listen", "--health-listen", "--cert-dir", "--state-dir", "--poll-interval"} {
		if !strings.Contains(stderr.String(), strings.TrimPrefix(flag, "-")) {
			t.Errorf("--help does not describe %s: %s", flag, stderr.String())
		}
	}
}

func TestCoverTakesItsSettingsFromTheEnvironmentToo(t *testing.T) {
	// Everything is set by the environment; the certificate is missing, so the run stops after reading its settings.
	env := map[string]string{
		"IMPACT_INGEST_ENDPOINT":     "https://ingest.example.test",
		"IMPACT_INSTRUMENTATION":     "impact-ai/impact",
		"IMPACT_COVER_CERT_DIR":      filepath.Join(t.TempDir(), "no-certificates"),
		"IMPACT_COVER_LISTEN":        "127.0.0.1:0",
		"IMPACT_COVER_HEALTH_LISTEN": "127.0.0.1:0",
		"IMPACT_COVER_STATE_DIR":     t.TempDir(),
		"IMPACT_COVER_POLL_INTERVAL": "30s",
	}
	var stdout, stderr bytes.Buffer
	code := runCover(context.Background(), nil, func(k string) string { return env[k] }, &stdout, &stderr, testDeps())
	if code != 1 {
		t.Errorf("exit code = %d, want 1 (the settings are fine, the certificate is missing): %s", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "tls.crt") {
		t.Errorf("stderr does not name the missing certificate: %s", stderr.String())
	}
}

func TestCoverStopsWithOneWhenTheCertificateIsMissingOrABindFails(t *testing.T) {
	args := func(certDir string, extra ...string) []string {
		return append([]string{
			"--ingest-endpoint=https://ingest.example.test", "--instrumentation=impact-ai/impact",
			"--cert-dir=" + certDir, "--state-dir=" + t.TempDir(), "--listen=127.0.0.1:0", "--health-listen=127.0.0.1:0",
		}, extra...)
	}
	var stdout, stderr bytes.Buffer
	if code := runCover(context.Background(), args(t.TempDir()), func(string) string { return "" }, &stdout, &stderr, testDeps()); code != 1 {
		t.Errorf("no certificate: exit code = %d (%s)", code, stderr.String())
	}

	// A port that is taken.
	certs := t.TempDir()
	writeSelfSigned(t, certs)
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	stderr.Reset()
	code := runCover(context.Background(), args(certs, "--listen="+taken.Addr().String()), func(string) string { return "" }, &stdout, &stderr, testDeps())
	if code != 1 || !strings.Contains(stderr.String(), "listen") {
		t.Errorf("a taken port: exit code = %d (%s)", code, stderr.String())
	}
	stderr.Reset()
	code = runCover(context.Background(), args(certs, "--health-listen="+taken.Addr().String()), func(string) string { return "" }, &stdout, &stderr, testDeps())
	if code != 1 || !strings.Contains(stderr.String(), "listen") {
		t.Errorf("a taken health port: exit code = %d (%s)", code, stderr.String())
	}
}

// writeSelfSigned puts tls.crt and tls.key for 127.0.0.1 into dir and returns a pool that trusts the certificate.
func writeSelfSigned(t *testing.T, dir string) *x509.CertPool {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, _ := rand.Int(rand.Reader, big.NewInt(1<<62))
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "cover-test"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, _ := x509.MarshalECPrivateKey(key)
	for name, block := range map[string]*pem.Block{
		"tls.crt": {Type: "CERTIFICATE", Bytes: der},
		"tls.key": {Type: "EC PRIVATE KEY", Bytes: keyDER},
	} {
		if err := os.WriteFile(filepath.Join(dir, name), pem.EncodeToMemory(block), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cert, _ := x509.ParseCertificate(der)
	pool := x509.NewCertPool()
	pool.AddCert(cert)
	return pool
}

// coverIngest plays the ingest route that serves the list.
type coverIngest struct {
	*httptest.Server
	mu       sync.Mutex
	mode     string // "ok", "revoked" or "down"
	requests []http.Header
}

func newCoverIngest(t *testing.T, list string) *coverIngest {
	t.Helper()
	in := &coverIngest{mode: "ok"}
	in.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		in.mu.Lock()
		in.requests = append(in.requests, r.Header.Clone())
		mode := in.mode
		in.mu.Unlock()
		if r.URL.Path != "/v1/inventory/cover" || r.Method != http.MethodGet {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		switch {
		case mode == "down":
			w.WriteHeader(http.StatusServiceUnavailable)
		case mode == "revoked" || r.Header.Get("Authorization") != "Bearer "+coverKey:
			w.WriteHeader(http.StatusUnauthorized)
		case r.Header.Get("If-None-Match") == `"v3"`:
			w.WriteHeader(http.StatusNotModified)
		default:
			w.Header().Set("ETag", `"v3"`)
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, list)
		}
	}))
	t.Cleanup(in.Close)
	return in
}

func (in *coverIngest) setMode(mode string) {
	in.mu.Lock()
	in.mode = mode
	in.mu.Unlock()
}

func (in *coverIngest) seen() []http.Header {
	in.mu.Lock()
	defer in.mu.Unlock()
	return append([]http.Header(nil), in.requests...)
}

const coverList = `{"schema":"impact.cover.v1","version":3,"automatic":true,"workloads":[
	{"namespace":"shop","kind":"Deployment","name":"support-assistant","lang":"python","container":"app"}]}`

func admissionReview(namespace, deployment string) []byte {
	pod := map[string]any{
		"metadata": map[string]any{
			"generateName": deployment + "-5d9c7b6f8d-", "namespace": namespace,
			"labels":          map[string]any{"pod-template-hash": "5d9c7b6f8d"},
			"ownerReferences": []any{map[string]any{"kind": "ReplicaSet", "name": deployment + "-5d9c7b6f8d", "controller": true}},
		},
		"spec": map[string]any{"containers": []any{map[string]any{"name": "app", "env": []any{map[string]any{"name": "OPENAI_API_KEY", "value": fakeKey}}}}},
	}
	b, _ := json.Marshal(map[string]any{
		"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
		"request": map[string]any{"uid": "uid-1", "operation": "CREATE", "namespace": namespace, "object": pod},
	})
	return b
}

type running struct {
	t       *testing.T
	cancel  context.CancelFunc
	done    chan int
	stderr  *syncBuffer
	sleeps  chan time.Duration
	release chan struct{}
	webhook string
	health  string
	client  *http.Client
	state   string
}

// startCover runs `reporter cover` with the poll loop under the test's control: it sleeps only when told to go on.
func startCover(t *testing.T, ingest *coverIngest) *running {
	t.Helper()
	certs, state, keyDir := t.TempDir(), t.TempDir(), t.TempDir()
	pool := writeSelfSigned(t, certs)
	keyFile := filepath.Join(keyDir, "ingest-key")
	if err := os.WriteFile(keyFile, []byte(coverKey+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &running{
		t: t, cancel: cancel, done: make(chan int, 1), stderr: &syncBuffer{},
		sleeps: make(chan time.Duration), release: make(chan struct{}), state: state,
		client: &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}},
	}
	d := deps{
		now:    func() time.Time { return fixedNow },
		jitter: func() float64 { return 0.5 },
		sleep: func(ctx context.Context, wait time.Duration) error {
			select {
			case r.sleeps <- wait:
			case <-ctx.Done():
				return ctx.Err()
			}
			select {
			case <-r.release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
	}
	args := []string{
		"--ingest-endpoint=" + ingest.URL, "--ingest-key-file=" + keyFile, "--instrumentation=impact-ai/impact",
		"--cert-dir=" + certs, "--state-dir=" + state, "--listen=127.0.0.1:0", "--health-listen=127.0.0.1:0", "--poll-interval=30s",
	}
	var stdout bytes.Buffer
	go func() { r.done <- runCover(ctx, args, func(string) string { return "" }, &stdout, r.stderr, d) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-r.done:
		case <-time.After(10 * time.Second):
			t.Error("cover did not stop")
		}
	})

	// The addresses it listens on are in its first log line.
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		for _, line := range strings.Split(r.stderr.String(), "\n") {
			var rec struct {
				Msg     string `json:"msg"`
				Webhook string `json:"webhook"`
				Health  string `json:"health"`
			}
			if json.Unmarshal([]byte(line), &rec) == nil && rec.Msg == "cover started" {
				r.webhook, r.health = rec.Webhook, rec.Health
				return r
			}
		}
		select {
		case code := <-r.done:
			t.Fatalf("cover stopped with code %d: %s", code, r.stderr.String())
		case <-time.After(10 * time.Millisecond):
		}
	}
	t.Fatalf("cover did not start: %s", r.stderr.String())
	return nil
}

// nextWait waits for the poll loop to say it finished a poll and is about to sleep.
func (r *running) nextWait() time.Duration {
	r.t.Helper()
	select {
	case d := <-r.sleeps:
		return d
	case code := <-r.done:
		r.t.Fatalf("cover stopped with code %d: %s", code, r.stderr.String())
	case <-time.After(15 * time.Second):
		r.t.Fatalf("no poll finished: %s", r.stderr.String())
	}
	return 0
}

func (r *running) goOn() {
	r.t.Helper()
	select {
	case r.release <- struct{}{}:
	case <-time.After(5 * time.Second):
		r.t.Fatal("the poll loop is not waiting")
	}
}

// mutate sends an AdmissionReview for a pod of the Deployment over TLS and returns the answer.
func (r *running) mutate(namespace, deployment string) (allowed bool, patch []byte) {
	r.t.Helper()
	resp, err := r.client.Post("https://"+r.webhook+"/mutate", "application/json", bytes.NewReader(admissionReview(namespace, deployment)))
	if err != nil {
		r.t.Fatalf("POST /mutate: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		r.t.Fatalf("POST /mutate = %d: %s", resp.StatusCode, body)
	}
	var out struct {
		Response struct {
			UID     string `json:"uid"`
			Allowed bool   `json:"allowed"`
			Patch   []byte `json:"patch"`
		} `json:"response"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		r.t.Fatalf("answer: %v\n%s", err, body)
	}
	if out.Response.UID != "uid-1" {
		r.t.Errorf("uid = %q", out.Response.UID)
	}
	return out.Response.Allowed, out.Response.Patch
}

func (r *running) healthStatus(path string) int {
	resp, err := http.Get("http://" + r.health + path)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

func TestCoverFromIngestListToInjectionAnnotationAndBack(t *testing.T) {
	ingest := newCoverIngest(t, coverList)
	r := startCover(t, ingest)

	// Both listeners are up and the certificate is loaded: ready, before any list is there to apply (or after: either).
	deadline := time.Now().Add(10 * time.Second)
	for r.healthStatus("/readyz") != http.StatusOK && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := r.healthStatus("/readyz"); got != http.StatusOK {
		t.Fatalf("/readyz = %d", got)
	}
	if got := r.healthStatus("/healthz"); got != http.StatusOK {
		t.Errorf("/healthz = %d", got)
	}

	// The first poll: the key is the bearer, there is no ETag yet, and the list is in use.
	if wait := r.nextWait(); !closeTo(wait, 30*time.Second) {
		t.Errorf("the next poll is in %v, want the 30s interval", wait)
	}
	seen := ingest.seen()
	if len(seen) != 1 || seen[0].Get("Authorization") != "Bearer "+coverKey || seen[0].Get("If-None-Match") != "" {
		t.Fatalf("first poll = %+v", seen)
	}
	allowed, patch := r.mutate("shop", "support-assistant")
	if !allowed || len(patch) == 0 {
		t.Fatalf("a pod of a listed workload: allowed %v, patch %q", allowed, patch)
	}
	var ops []map[string]any
	if err := json.Unmarshal(patch, &ops); err != nil || len(ops) != 2 || ops[0]["path"] != "/metadata/annotations" ||
		ops[1]["path"] != "/metadata/labels/impact.ai~1auto-covered" || ops[1]["value"] != "true" {
		t.Fatalf("patch = %s (%v): the annotations, and the marker as a label", patch, err)
	}
	annotations, _ := ops[0]["value"].(map[string]any)
	if annotations["instrumentation.opentelemetry.io/inject-python"] != "impact-ai/impact" ||
		annotations["instrumentation.opentelemetry.io/container-names"] != "app" ||
		annotations["impact.ai/auto-covered"] != "2026-11-03T12:00:00Z" {
		t.Errorf("annotations = %v", annotations)
	}
	if allowed, patch := r.mutate("shop", "other"); !allowed || len(patch) != 0 {
		t.Errorf("a pod of an unlisted workload: allowed %v, patch %q", allowed, patch)
	}
	if allowed, patch := r.mutate("elsewhere", "support-assistant"); !allowed || len(patch) != 0 {
		t.Errorf("a pod of the same name in another namespace: allowed %v, patch %q", allowed, patch)
	}
	if _, err := os.Stat(filepath.Join(r.state, "list.json")); err != nil {
		t.Errorf("the list was not saved: %v", err)
	}

	// Ingest goes down: the last list stays in use, and the loop backs off.
	ingest.setMode("down")
	r.goOn()
	if wait := r.nextWait(); !closeTo(wait, 2*time.Second) {
		t.Errorf("after a failed poll the next one is in %v, want 2s", wait)
	}
	if _, patch := r.mutate("shop", "support-assistant"); len(patch) == 0 {
		t.Error("ingest being down stopped the coverage: the last list must stay in use")
	}

	// It comes back: the next poll is conditional, the answer is 304, and nothing changes.
	ingest.setMode("ok")
	r.goOn()
	if wait := r.nextWait(); !closeTo(wait, 30*time.Second) {
		t.Errorf("after a good poll the next one is in %v, want 30s", wait)
	}
	seen = ingest.seen()
	if last := seen[len(seen)-1]; last.Get("If-None-Match") != `"v3"` || last.Get("Authorization") != "Bearer "+coverKey {
		t.Errorf("the poll after the outage = %+v, want If-None-Match \"v3\" and the key", last)
	}
	if _, patch := r.mutate("shop", "support-assistant"); len(patch) == 0 {
		t.Error("a 304 stopped the coverage")
	}

	// The key is revoked: nothing is injected any more, and the saved list is gone.
	ingest.setMode("revoked")
	r.goOn()
	r.nextWait()
	if allowed, patch := r.mutate("shop", "support-assistant"); !allowed || len(patch) != 0 {
		t.Errorf("with the key refused: allowed %v, patch %q", allowed, patch)
	}
	if _, err := os.Stat(filepath.Join(r.state, "list.json")); !os.IsNotExist(err) {
		t.Errorf("the saved list is still there after the key was refused: %v", err)
	}

	// Nothing in the logs but names: no key, no planted value.
	for _, leak := range []string{coverKey, fakeKey} {
		if strings.Contains(r.stderr.String(), leak) {
			t.Errorf("the log contains %q:\n%s", leak, r.stderr.String())
		}
	}

	// A clean shutdown on cancellation.
	r.cancel()
	select {
	case code := <-r.done:
		if code != 0 {
			t.Errorf("exit code = %d, want 0", code)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cover did not stop")
	}
	if got := r.healthStatus("/healthz"); got != 0 {
		t.Errorf("the health port still answers after shutdown (%d)", got)
	}
	r.done <- 0 // the cleanup waits for a value
}

func TestCoverStartsFromTheSavedListWhenIngestIsDown(t *testing.T) {
	ingest := newCoverIngest(t, coverList)
	ingest.setMode("down")

	certs, state, keyDir := t.TempDir(), t.TempDir(), t.TempDir()
	pool := writeSelfSigned(t, certs)
	keyFile := filepath.Join(keyDir, "ingest-key")
	_ = os.WriteFile(keyFile, []byte(coverKey), 0o600)
	saved, _ := json.Marshal(map[string]any{"etag": `"v3"`, "list": json.RawMessage(coverList)})
	if err := os.WriteFile(filepath.Join(state, "list.json"), saved, 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sleeps := make(chan time.Duration, 4)
	stderr := &syncBuffer{}
	done := make(chan int, 1)
	d := deps{now: func() time.Time { return fixedNow }, jitter: func() float64 { return 0.5 }, sleep: func(ctx context.Context, wait time.Duration) error {
		select {
		case sleeps <- wait:
		default:
		}
		<-ctx.Done()
		return ctx.Err()
	}}
	args := []string{
		"--ingest-endpoint=" + ingest.URL, "--ingest-key-file=" + keyFile, "--instrumentation=impact-ai/impact",
		"--cert-dir=" + certs, "--state-dir=" + state, "--listen=127.0.0.1:0", "--health-listen=127.0.0.1:0",
	}
	var stdout bytes.Buffer
	go func() { done <- runCover(ctx, args, func(string) string { return "" }, &stdout, stderr, d) }()

	select {
	case <-sleeps: // the first poll has failed
	case <-time.After(15 * time.Second):
		t.Fatalf("no poll: %s", stderr.String())
	}
	var webhook string
	for _, line := range strings.Split(stderr.String(), "\n") {
		var rec struct {
			Msg     string `json:"msg"`
			Webhook string `json:"webhook"`
		}
		if json.Unmarshal([]byte(line), &rec) == nil && rec.Msg == "cover started" {
			webhook = rec.Webhook
		}
	}
	if webhook == "" {
		t.Fatalf("no start line: %s", stderr.String())
	}
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}
	resp, err := client.Post("https://"+webhook+"/mutate", "application/json", bytes.NewReader(admissionReview("shop", "support-assistant")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), `"patchType":"JSONPatch"`) {
		t.Errorf("with ingest down at start, the saved list was not used: %s", body)
	}
	if seen := ingest.seen(); len(seen) == 0 || seen[0].Get("If-None-Match") != `"v3"` {
		t.Errorf("the first poll should be conditional on the saved ETag: %+v", seen)
	}
	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Errorf("exit code = %d", code)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cover did not stop")
	}
}

// labelledFixtureAPI is the fixture cluster with an impact.ai label on the support-assistant Deployment and on the
// shop namespace, and one label that is not impact.ai's on each.
func labelledFixtureAPI(t *testing.T) *kubetest.Server {
	t.Helper()
	api := fixtureAPI(t)
	relist := func(path, file string, edit func(item map[string]any)) {
		var list struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(readFixture(t, file), &list); err != nil {
			t.Fatal(err)
		}
		items := make([]any, len(list.Items))
		for i, item := range list.Items {
			edit(item)
			items[i] = item
		}
		api.HandleList(path, items, 2)
	}
	label := func(item map[string]any, labels map[string]any) {
		meta := item["metadata"].(map[string]any)
		existing, _ := meta["labels"].(map[string]any)
		if existing == nil {
			existing = map[string]any{}
		}
		for k, v := range labels {
			existing[k] = v
		}
		meta["labels"] = existing
	}
	relist("/apis/apps/v1/deployments", "deployments.json", func(item map[string]any) {
		if item["metadata"].(map[string]any)["name"] == "support-assistant" {
			label(item, map[string]any{"impact.ai/opt-out": "true", "owner-email": "someone-private"})
		}
	})
	relist("/api/v1/namespaces", "namespaces.json", func(item map[string]any) {
		if item["metadata"].(map[string]any)["name"] == "shop" {
			label(item, map[string]any{"impact.ai/team": "ai", "cost-center": "4711"})
		}
	})
	return api
}

type dryRunSnapshot struct {
	Workloads []map[string]any `json:"workloads"`
}

func snapshotOf(t *testing.T, api *kubetest.Server, extra ...string) ([]byte, dryRunSnapshot) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), append(append(baseArgs(api), "--dry-run"), extra...), testEnv(), &stdout, &stderr, testDeps()); code != 0 {
		t.Fatalf("exit code %d, stderr:\n%s", code, stderr.String())
	}
	var snap dryRunSnapshot
	if err := json.Unmarshal(stdout.Bytes(), &snap); err != nil {
		t.Fatal(err)
	}
	return stdout.Bytes(), snap
}

func workloadNamed(t *testing.T, snap dryRunSnapshot, name string) map[string]any {
	t.Helper()
	for _, w := range snap.Workloads {
		if w["name"] == name {
			return w
		}
	}
	t.Fatalf("no workload %s", name)
	return nil
}

func TestWithoutTheCoverFieldsFlagTheSnapshotIsTheGoldenOneWhateverTheLabels(t *testing.T) {
	out, _ := snapshotOf(t, labelledFixtureAPI(t))
	compareGolden(t, out)
}

func TestTheCoverFieldsFlagAddsTheImpactLabelsAndNothingElse(t *testing.T) {
	api := labelledFixtureAPI(t)
	out, snap := snapshotOf(t, api, "--cover-fields")
	assistant := workloadNamed(t, snap, "support-assistant")
	if got := assistant["labels"]; fmt.Sprint(got) != "map[impact.ai/opt-out:true]" {
		t.Errorf("labels = %v, want only the impact.ai label", got)
	}
	if got := assistant["namespaceLabels"]; fmt.Sprint(got) != "map[impact.ai/team:ai]" {
		t.Errorf("namespaceLabels = %v, want only the impact.ai label of the namespace", got)
	}
	if web := workloadNamed(t, snap, "web"); web["labels"] != nil || web["namespaceLabels"] == nil {
		t.Errorf("web has no label of its own but shares its namespace's: %v", web)
	}
	for _, leak := range []string{"someone-private", "owner-email", "cost-center", "4711"} {
		if strings.Contains(string(out), leak) {
			t.Errorf("the snapshot contains %q", leak)
		}
	}

	// The environment says the same as the flag.
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), append(baseArgs(api), "--dry-run"), testEnv("IMPACT_COVER_FIELDS", "true"), &stdout, &stderr, testDeps()); code != 0 {
		t.Fatalf("exit code %d: %s", code, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"namespaceLabels"`) {
		t.Error("IMPACT_COVER_FIELDS=true did not add the fields")
	}
}

func TestTheCoverGoldenSnapshotNamesWhyPodsDoNotStartAndOnlyThat(t *testing.T) {
	// An install with automatic coverage (--cover-fields) over the fixture cluster, in which billing-api cannot create a
	// pod and the pods of search-assistant wait for the init container's image. The conditions and the statuses carry
	// messages (a policy's words, a registry's answer, a key): the snapshot has the reasons and none of them.
	out, snap := snapshotOf(t, fixtureAPI(t), "--cover-fields")
	compareGoldenFile(t, "golden-snapshot-cover.json", out)

	stall := func(name string) string { return fmt.Sprint(workloadNamed(t, snap, name)["stall"]) }
	if got := stall("billing-api"); got != "map[replicaFailure:FailedCreate]" {
		t.Errorf("billing-api stall = %s", got)
	}
	if got := stall("search-assistant"); got != "map[initWaiting:[ErrImagePull ImagePullBackOff]]" {
		t.Errorf("search-assistant stall = %s", got)
	}
	for _, name := range []string{"support-assistant", "web", "idle", "vector-db", "log-agent", "nightly-report", "backfill", "debug-shell"} {
		if got := workloadNamed(t, snap, name)["stall"]; got != nil {
			t.Errorf("%s has a stall: %v", name, got)
		}
	}
	for _, leak := range []string{"forbidden", "not from an allowed registry", "Back-off", "pull access denied", "Unauthorized", "mirror.example.com", "sk-live"} {
		if strings.Contains(string(out), leak) {
			t.Errorf("the snapshot contains %q", leak)
		}
	}
}

func TestWithoutTheCoverFieldsFlagThePlainGoldenHasNoStallEvenWhenPodsDoNotStart(t *testing.T) {
	out, snap := snapshotOf(t, fixtureAPI(t))
	if strings.Contains(string(out), "stall") {
		t.Errorf("a server that does not know the field sees it: %s", out)
	}
	for _, name := range []string{"billing-api", "search-assistant"} {
		workloadNamed(t, snap, name) // they are in the plain snapshot, as workloads, with no stall
	}
}
