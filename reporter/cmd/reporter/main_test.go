// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"flag"
	"io"
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

var update = flag.Bool("update", false, "rewrite testdata/golden-snapshot.json")

var fixedNow = time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)

const (
	testKey = "ik_test_0123456789abcdef"
	fakeKey = "sk-live-abcdefghijklmnop"
)

// apiPaths maps API paths to fixture files in testdata/api.
var apiPaths = map[string]string{
	"/apis/apps/v1/deployments":  "deployments.json",
	"/apis/apps/v1/statefulsets": "statefulsets.json",
	"/apis/apps/v1/daemonsets":   "daemonsets.json",
	"/apis/batch/v1/cronjobs":    "cronjobs.json",
	"/apis/batch/v1/jobs":        "jobs.json",
	"/api/v1/pods":               "pods.json",
	"/api/v1/nodes":              "nodes.json",
	"/api/v1/namespaces":         "namespaces.json",
	"/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations": "mutatingwebhookconfigurations.json",
}

func readFixture(t testing.TB, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "api", name))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// fixtureAPI serves the fixtures like an API server: lists in pages of two.
func fixtureAPI(t testing.TB) *kubetest.Server {
	t.Helper()
	srv := kubetest.New(t)
	srv.Handle("/version", readFixture(t, "version.json"))
	srv.Handle("/api/v1/namespaces/kube-system", readFixture(t, "namespace-kube-system.json"))
	for path, file := range apiPaths {
		var list struct {
			Items []json.RawMessage `json:"items"`
		}
		if err := json.Unmarshal(readFixture(t, file), &list); err != nil {
			t.Fatal(err)
		}
		items := make([]any, len(list.Items))
		for i, it := range list.Items {
			items[i] = it
		}
		srv.HandleList(path, items, 2)
	}
	return srv
}

// ingest is a fake ingest that records what it receives.
type ingest struct {
	*httptest.Server
	mu       sync.Mutex
	requests []ingestRequest
	status   int
}

type ingestRequest struct {
	Method, Path string
	Header       http.Header
	Raw          []byte // gzip body
}

func newIngest(t testing.TB) *ingest {
	t.Helper()
	in := &ingest{status: http.StatusAccepted}
	in.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		in.mu.Lock()
		in.requests = append(in.requests, ingestRequest{r.Method, r.URL.Path, r.Header.Clone(), body})
		status := in.status
		in.mu.Unlock()
		w.WriteHeader(status)
	}))
	t.Cleanup(in.Close)
	return in
}

func (in *ingest) wait(t testing.TB, n int) []ingestRequest {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		in.mu.Lock()
		got := append([]ingestRequest(nil), in.requests...)
		in.mu.Unlock()
		if len(got) >= n {
			return got
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("ingest received fewer than %d requests", n)
	return nil
}

func gunzip(t testing.TB, raw []byte) []byte {
	t.Helper()
	zr, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	out, err := io.ReadAll(zr)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func testDeps() deps { return deps{now: func() time.Time { return fixedNow }} }

func baseArgs(api *kubetest.Server) []string {
	return []string{
		"--api-server=" + api.URL, "--cluster-name=prod-eks", "--own-namespace=impact-system",
		"--release-name=impact-cluster-kit", "--release-namespace=impact-system",
	}
}

func testEnv(extra ...string) func(string) string {
	return env(append([]string{"POD_UID", "5b1d0f4c-0000-4000-8000-000000000001"}, extra...)...)
}

func writeKeyFile(t testing.TB) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "ingest-key")
	if err := os.WriteFile(p, []byte(testKey+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// canonical decodes JSON and encodes it again with sorted keys and indentation.
func canonical(t testing.TB, data []byte) string {
	t.Helper()
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, data)
	}
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return string(out) + "\n"
}

func compareGolden(t *testing.T, got []byte) {
	t.Helper()
	compareGoldenFile(t, "golden-snapshot.json", got)
}

// compareGoldenFile compares a snapshot with a file of testdata: the plain snapshot of the fixture cluster, or the one
// of an install with automatic coverage (golden-snapshot-cover.json), which adds the facts of that.
func compareGoldenFile(t *testing.T, name string, got []byte) {
	t.Helper()
	path := filepath.Join("..", "..", "testdata", name)
	if *update {
		if err := os.WriteFile(path, []byte(canonical(t, got)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run the test with -update to create the golden file)", err)
	}
	if g := canonical(t, got); g != string(want) {
		t.Errorf("snapshot differs from testdata/%s (run with -update after checking the change):\n--- got\n%s\n--- want\n%s", name, g, want)
	}
}

func TestDryRunPrintsTheGoldenSnapshotAndExits(t *testing.T) {
	api := fixtureAPI(t)
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), append(baseArgs(api), "--dry-run"), testEnv(), &stdout, &stderr, testDeps())
	if code != 0 {
		t.Fatalf("exit code %d, stderr:\n%s", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "{\n  \"schema\"") {
		t.Errorf("dry run should print indented JSON, got %.80q", stdout.String())
	}
	compareGolden(t, stdout.Bytes())
}

func TestDryRunReportsTheCloudIdentityTheChartGave(t *testing.T) {
	api := fixtureAPI(t)
	var stdout, stderr bytes.Buffer
	args := append(baseArgs(api), "--dry-run", "--cloud-provider=aws", "--cloud-account-id=012345678901", "--cloud-region=eu-west-1",
		"--cluster-arn=arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks")
	if code := run(context.Background(), args, testEnv(), &stdout, &stderr, testDeps()); code != 0 {
		t.Fatalf("exit code %d, stderr:\n%s", code, stderr.String())
	}
	var snap struct {
		Cluster map[string]any `json:"cluster"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &snap); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"cloudProvider": "aws", "accountId": "012345678901", "region": "eu-west-1",
		"arn": "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks",
	}
	for key, value := range want {
		if snap.Cluster[key] != value {
			t.Errorf("cluster[%q] = %v, want %v", key, snap.Cluster[key], value)
		}
	}
}

func TestSendsTheGoldenSnapshotToIngest(t *testing.T) {
	api := fixtureAPI(t)
	in := newIngest(t)
	keyFile := writeKeyFile(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan int, 1)
	var stderr bytes.Buffer
	go func() {
		done <- run(ctx, append(baseArgs(api), "--ingest-endpoint="+in.URL, "--ingest-key-file="+keyFile, "--listen=127.0.0.1:0"),
			testEnv(), io.Discard, &stderr, testDeps())
	}()
	reqs := in.wait(t, 1)
	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Errorf("exit code %d after cancellation, stderr:\n%s", code, stderr.String())
		}
	case <-time.After(10 * time.Second):
		t.Fatal("run did not stop after the context was canceled")
	}

	r := reqs[0]
	if r.Method != http.MethodPost || r.Path != "/v1/inventory" {
		t.Errorf("%s %s, want POST /v1/inventory", r.Method, r.Path)
	}
	if got := r.Header.Get("Authorization"); got != "Bearer "+testKey {
		t.Errorf("Authorization = %q", got)
	}
	if r.Header.Get("Content-Type") != "application/json" || r.Header.Get("Content-Encoding") != "gzip" {
		t.Errorf("content headers = %q, %q", r.Header.Get("Content-Type"), r.Header.Get("Content-Encoding"))
	}
	body := gunzip(t, r.Raw)
	compareGolden(t, body)

	// Names only, end to end: nothing secret reaches ingest.
	for _, forbidden := range []string{
		fakeKey, "sk-live", "--api-key", "support_assistant", "last-applied", "kubectl.kubernetes.io",
		"example.com/team", "TOP-SECRET-CA-BUNDLE", "cert-manager.io/inject-ca-from-secret", testKey, "86400",
	} {
		if bytes.Contains(body, []byte(forbidden)) || strings.Contains(stderr.String(), forbidden) {
			t.Errorf("%q travelled to ingest or the logs", forbidden)
		}
	}
}

func TestAForeignInjectorMakesTheSnapshotReportOnly(t *testing.T) {
	api := fixtureAPI(t)
	list := map[string]any{"items": []any{
		map[string]any{"metadata": map[string]any{"name": "datadog-webhook"}, "webhooks": []any{
			map[string]any{"name": "datadog.webhook.auto.instrumentation", "clientConfig": map[string]any{"service": map[string]any{"name": "dd", "namespace": "datadog", "path": "/injectlib"}}}}},
	}}
	api.Handle("/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations", list)

	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), append(baseArgs(api), "--dry-run"), testEnv(), &stdout, &stderr, testDeps()); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	var snap struct {
		Reporter struct {
			Mode         string  `json:"mode"`
			ClusterOwner *string `json:"clusterOwner"`
		} `json:"reporter"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &snap); err != nil {
		t.Fatal(err)
	}
	if snap.Reporter.Mode != "report-only" || snap.Reporter.ClusterOwner == nil || *snap.Reporter.ClusterOwner != "datadog" {
		t.Errorf("reporter = %+v, want report-only owned by datadog", snap.Reporter)
	}
}

func TestInjectionDisabledByTheChartIsReportOnlyWithoutAnOwner(t *testing.T) {
	api := fixtureAPI(t)
	var stdout bytes.Buffer
	if code := run(context.Background(), append(baseArgs(api), "--dry-run", "--injection=disabled"), testEnv(), &stdout, io.Discard, testDeps()); code != 0 {
		t.Fatal(code)
	}
	var snap struct {
		Reporter struct {
			Mode         string  `json:"mode"`
			ClusterOwner *string `json:"clusterOwner"`
		} `json:"reporter"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &snap); err != nil {
		t.Fatal(err)
	}
	if snap.Reporter.Mode != "report-only" || snap.Reporter.ClusterOwner != nil {
		t.Errorf("reporter = %+v", snap.Reporter)
	}
}

func freeAddr(t testing.TB) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().String()
}

func status(url string) int {
	resp, err := http.Get(url)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

func TestHealthEndpoints(t *testing.T) {
	api := fixtureAPI(t)
	in := newIngest(t)
	keyFile := writeKeyFile(t)
	addr := freeAddr(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan int, 1)
	go func() {
		done <- run(ctx, append(baseArgs(api), "--ingest-endpoint="+in.URL, "--ingest-key-file="+keyFile, "--listen="+addr),
			testEnv(), io.Discard, io.Discard, testDeps())
	}()

	deadline := time.Now().Add(10 * time.Second)
	for status("http://"+addr+"/healthz") != http.StatusOK {
		if time.Now().After(deadline) {
			t.Fatal("/healthz never answered 200")
		}
		time.Sleep(10 * time.Millisecond)
	}
	for status("http://"+addr+"/readyz") != http.StatusOK {
		if time.Now().After(deadline) {
			t.Fatal("/readyz never answered 200 after the first snapshot was built")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Errorf("exit code %d", code)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("run did not stop")
	}
}

func TestReadyzIsNotReadyBeforeTheFirstSnapshot(t *testing.T) {
	var ready readiness
	h := healthHandler(&ready)
	for path, want := range map[string]int{"/healthz": 200, "/readyz": 503} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != want {
			t.Errorf("%s before: %d, want %d", path, rec.Code, want)
		}
	}
	ready.set()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != 200 {
		t.Errorf("/readyz after: %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/other", nil))
	if rec.Code != 404 {
		t.Errorf("/other: %d, want 404", rec.Code)
	}
}

func TestVersionFlag(t *testing.T) {
	var stdout, stderr bytes.Buffer
	old := version
	version = "1.2.3"
	defer func() { version = old }()
	if code := run(context.Background(), []string{"--version"}, env(), &stdout, &stderr, testDeps()); code != 0 {
		t.Fatalf("exit %d", code)
	}
	if got := strings.TrimSpace(stdout.String()); got != "impact-reporter 1.2.3" {
		t.Errorf("version output = %q", got)
	}
}

func TestBadConfigExitsWithTwoAndSaysWhy(t *testing.T) {
	var stderr bytes.Buffer
	code := run(context.Background(), []string{"--interval=1s"}, env(), io.Discard, &stderr, testDeps())
	if code != 2 || !strings.Contains(stderr.String(), "ingest endpoint") {
		t.Errorf("exit %d, stderr %q", code, stderr.String())
	}
}

func TestOutsideAClusterWithoutAnAPIServerFailsClearly(t *testing.T) {
	var stderr bytes.Buffer
	code := run(context.Background(), []string{"--dry-run"}, env(), io.Discard, &stderr, testDeps())
	if code != 1 || !strings.Contains(stderr.String(), "not running in a cluster") {
		t.Errorf("exit %d, stderr %q", code, stderr.String())
	}
}

func TestDryRunFailsWhenTheClusterCannotBeRead(t *testing.T) {
	api := fixtureAPI(t)
	api.HandleStatus("/api/v1/pods", http.StatusForbidden, `{}`)
	var stderr bytes.Buffer
	code := run(context.Background(), append(baseArgs(api), "--dry-run"), testEnv(), io.Discard, &stderr, testDeps())
	if code != 1 {
		t.Errorf("exit %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "pods") {
		t.Errorf("stderr should name what failed: %s", stderr.String())
	}
}

func TestInstanceFallsBackToARandomID(t *testing.T) {
	api := fixtureAPI(t)
	var out1, out2 bytes.Buffer
	args := append(baseArgs(api), "--dry-run")
	run(context.Background(), args, env(), &out1, io.Discard, testDeps())
	run(context.Background(), args, env(), &out2, io.Discard, testDeps())
	get := func(b []byte) string {
		var s struct {
			Reporter struct {
				Instance string `json:"instance"`
			} `json:"reporter"`
		}
		_ = json.Unmarshal(b, &s)
		return s.Reporter.Instance
	}
	a, b := get(out1.Bytes()), get(out2.Bytes())
	if len(a) < 16 || a == b {
		t.Errorf("instances = %q, %q; want two different random IDs", a, b)
	}
}

func TestGoldenSnapshotHasTheDocumentedShape(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "golden-snapshot.json"))
	if err != nil {
		t.Skip("golden file not created yet")
	}
	var snap map[string]any
	if err := json.Unmarshal(data, &snap); err != nil {
		t.Fatal(err)
	}
	want := []string{"cluster", "observedAt", "reporter", "schema", "sequence", "workloads"}
	if len(snap) != len(want) {
		t.Fatalf("golden file has %d top-level fields, want %v", len(snap), want)
	}
	for _, key := range want {
		if _, ok := snap[key]; !ok {
			t.Errorf("golden file lacks %q", key)
		}
	}
	if snap["schema"] != "impact.inventory.v1" || snap["sequence"] != float64(fixedNow.Unix()) {
		t.Errorf("schema/sequence = %v / %v", snap["schema"], snap["sequence"])
	}
}
