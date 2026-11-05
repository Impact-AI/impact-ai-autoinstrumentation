// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kubetest"
)

const (
	installToken   = "impactinstall_e2e_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"
	secretsPath    = "/api/v1/namespaces/impact-ai/secrets"
	keySecretPath  = secretsPath + "/impact-ai-install-key"
	exchangeAnswer = `{"installId":"ins_1","keyId":"pk_1","productId":"prod_e2e","autoCover":true,"replayed":false}`
	// What Helm makes before the exchange runs.
	emptySecret = `{"metadata":{"name":"impact-ai-install-key","labels":{"app.kubernetes.io/part-of":"impact-ai"}}}`
)

type exchangeEnv struct {
	api     *kubetest.Server
	ingest  *httptest.Server
	mu      sync.Mutex
	posts   [][]byte
	answers []func(http.ResponseWriter)
	args    []string
	sleeps  []time.Duration
}

func newExchangeEnv(t *testing.T, answers ...func(http.ResponseWriter)) *exchangeEnv {
	t.Helper()
	e := &exchangeEnv{api: kubetest.New(t), answers: answers}
	// The Secret is Helm's: empty, there before the hook runs. The hook reads it and patches it, and deletes it when a
	// first install is refused; it has no route to make one, so a POST would meet the fake server's 404 and show in the
	// requests below.
	e.api.HandleSequence(http.MethodGet, keySecretPath, kubetest.Response{Body: emptySecret})
	e.api.HandleSequence(http.MethodPatch, keySecretPath, kubetest.Response{Body: `{}`})
	e.api.HandleSequence(http.MethodDelete, keySecretPath, kubetest.Response{Body: `{}`})
	e.ingest = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		e.mu.Lock()
		e.posts = append(e.posts, append([]byte(r.Header.Get("Authorization")+"\n"), body...))
		var answer func(http.ResponseWriter)
		if len(e.answers) > 0 {
			answer, e.answers = e.answers[0], e.answers[1:]
		}
		e.mu.Unlock()
		if answer == nil {
			answer = answered(200, exchangeAnswer)
		}
		answer(w)
	}))
	t.Cleanup(e.ingest.Close)

	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "install-token")
	if err := os.WriteFile(tokenFile, []byte(installToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	saToken := filepath.Join(dir, "sa-token")
	if err := os.WriteFile(saToken, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	e.args = []string{
		"exchange",
		"--api-server=" + e.api.URL, "--token-file=" + saToken,
		"--ingest-endpoint=" + e.ingest.URL, "--product-id=prod_e2e", "--install-token-file=" + tokenFile,
		"--secret-name=impact-ai-install-key", "--namespace=impact-ai",
		"--cluster-uid=4e5622e7-f420-499c-9662-50af7f8a64b9", "--cluster-name=kind-e2e", "--chart-version=0.3.0",
	}
	return e
}

func answered(status int, body string, headers ...string) func(http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		for i := 0; i+1 < len(headers); i += 2 {
			w.Header().Set(headers[i], headers[i+1])
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func (e *exchangeEnv) run(extra ...string) (int, string, string) {
	var stdout, stderr bytes.Buffer
	d := deps{now: func() time.Time { return fixedNow }, jitter: func() float64 { return 0.5 }, sleep: func(ctx context.Context, d time.Duration) error {
		e.sleeps = append(e.sleeps, d)
		return ctx.Err()
	}}
	code := run(context.Background(), append(append([]string{}, e.args...), extra...), func(string) string { return "" }, &stdout, &stderr, d)
	return code, stdout.String(), stderr.String()
}

func TestExchangeStoresTheKeyThroughTheKubernetesAPIAndSendsOnlyTheHash(t *testing.T) {
	e := newExchangeEnv(t)
	code, stdout, stderr := e.run()
	if code != 0 {
		t.Fatalf("exit %d\n%s", code, stderr)
	}
	if stdout != "" {
		t.Errorf("stdout = %q", stdout)
	}

	// One read of the Secret Helm made, and two patches of it: the key stored ahead, then promoted. Nothing else.
	var patches [][]byte
	for _, r := range e.api.Requests() {
		if r.AuthHead != "Bearer sa-token" {
			t.Errorf("%s %s sent Authorization %q", r.Method, r.Path, r.AuthHead)
		}
		if r.Path != keySecretPath || (r.Method != http.MethodGet && r.Method != http.MethodPatch) {
			t.Errorf("request %s %s: the exchange reads and patches the one Secret and does nothing else", r.Method, r.Path)
		}
		if r.Method == http.MethodPatch {
			patches = append(patches, r.Body)
		}
	}
	if len(patches) != 2 {
		t.Fatalf("requests = %+v: want a GET, the write-ahead PATCH and the promoting PATCH", e.api.Requests())
	}
	var ahead struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
		Data map[string]string `json:"data"`
	}
	if err := json.Unmarshal(patches[0], &ahead); err != nil {
		t.Fatal(err)
	}
	if ahead.Metadata.Annotations["impact.ai/exchange"] != "pending" || ahead.Data["pending-key"] == "" {
		t.Errorf("write-ahead = %s", patches[0])
	}
	if !strings.Contains(string(patches[1]), `"ingest-key"`) || !strings.Contains(string(patches[1]), `"pending-key":null`) {
		t.Errorf("promotion = %s", patches[1])
	}

	if len(e.posts) != 1 || !strings.HasPrefix(string(e.posts[0]), "Bearer "+installToken+"\n") {
		t.Fatalf("ingest requests = %q", e.posts)
	}
	if strings.Contains(string(e.posts[0]), "pending-key") {
		t.Errorf("ingest saw the Secret's contents")
	}
	if strings.Contains(stderr, installToken) {
		t.Errorf("the token was logged: %s", stderr)
	}
}

func TestExchangeWithoutTheSecretHelmMakesFailsAndCreatesNothing(t *testing.T) {
	e := newExchangeEnv(t)
	e.api.HandleSequence(http.MethodGet, keySecretPath, kubetest.Response{Status: http.StatusNotFound, Body: `{"kind":"Status","code":404}`})
	code, _, stderr := e.run()
	if code != 1 {
		t.Fatalf("exit %d, want 1\n%s", code, stderr)
	}
	if !strings.Contains(stderr, "Helm") || len(e.posts) != 0 {
		t.Errorf("stderr = %q, ingest requests = %d", stderr, len(e.posts))
	}
	for _, r := range e.api.Requests() {
		if r.Method != http.MethodGet {
			t.Errorf("%s %s: with no Secret to write to the exchange wrote", r.Method, r.Path)
		}
	}
}

func TestExchangeRefusedPrintsThePlainSentenceTakesTheStoredKeyBackAndFails(t *testing.T) {
	e := newExchangeEnv(t, answered(409, `{"code":"token_used"}`))
	code, _, stderr := e.run()
	if code != 1 {
		t.Fatalf("exit %d, want 1\n%s", code, stderr)
	}
	if !strings.Contains(stderr, "already used") || !strings.Contains(stderr, "Cover your AI") {
		t.Errorf("stderr = %q", stderr)
	}
	// A refused first install leaves nothing: the key was stored ahead (one patch), and the Secret Helm made goes with it.
	// Nothing makes a Secret.
	var deleted, patched int
	for _, r := range e.api.Requests() {
		switch r.Method {
		case http.MethodPost:
			t.Errorf("%s %s: the hook has no right to make a Secret", r.Method, r.Path)
		case http.MethodDelete:
			deleted++
		case http.MethodPatch:
			patched++
		}
	}
	if deleted != 1 || patched != 1 {
		t.Errorf("the hook patched the Secret %d times and deleted it %d times, want the write-ahead and the delete", patched, deleted)
	}
	if strings.Contains(stderr, installToken) {
		t.Errorf("the token was printed: %s", stderr)
	}
}

func TestExchangeWhenImpactAiIsBusyExitsWithTheRetryableCode(t *testing.T) {
	var busy []func(http.ResponseWriter)
	for range 5 {
		busy = append(busy, answered(503, `{"code":"unavailable"}`, "Retry-After", "5"))
	}
	e := newExchangeEnv(t, busy...)
	code, _, stderr := e.run()
	if code != 3 {
		t.Fatalf("exit %d, want 3\n%s", code, stderr)
	}
	if !strings.Contains(stderr, "Nothing was changed") {
		t.Errorf("stderr = %q", stderr)
	}
	for _, r := range e.api.Requests() {
		if r.Method == http.MethodPatch && strings.Contains(string(r.Body), `"pending-key":null`) {
			t.Errorf("a busy impact.ai must not take the stored key back")
		}
	}
	if len(e.sleeps) != 4 {
		t.Errorf("sleeps = %v", e.sleeps)
	}
}

func TestExchangeUsageErrors(t *testing.T) {
	cases := map[string][]string{
		"no endpoint":      {"--ingest-endpoint="},
		"endpoint not url": {"--ingest-endpoint=ingest.example.test"},
		"no product":       {"--product-id="},
		"no secret name":   {"--secret-name="},
		"bad secret name":  {"--secret-name=../x"},
		"no namespace":     {"--namespace="},
		"no cluster uid":   {"--cluster-uid="},
		"bad timeout":      {"--timeout=soon"},
		"no token file":    {"--install-token-file=" + filepath.Join(t.TempDir(), "missing")},
	}
	for name, extra := range cases {
		t.Run(name, func(t *testing.T) {
			e := newExchangeEnv(t)
			code, _, stderr := e.run(extra...)
			if code != 2 {
				t.Errorf("exit %d, want 2\n%s", code, stderr)
			}
			if len(e.posts) != 0 || len(e.api.Requests()) != 0 {
				t.Errorf("a usage error reached the network")
			}
		})
	}
}

func TestExchangeVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), []string{"exchange", "--version"}, func(string) string { return "" }, &stdout, &stderr, deps{}); code != 0 || !strings.HasPrefix(stdout.String(), "impact-reporter ") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestTheReporterItselfIsStillWhatNoSubcommandStarts(t *testing.T) {
	// A usage error of the reporter proper, not of exchange or cover.
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"--interval=1s"}, func(string) string { return "" }, &stdout, &stderr, deps{})
	if code != 2 || strings.Contains(stderr.String(), "exchange") || strings.Contains(stderr.String(), "cover") {
		t.Errorf("exit %d, stderr %q", code, stderr.String())
	}
}
