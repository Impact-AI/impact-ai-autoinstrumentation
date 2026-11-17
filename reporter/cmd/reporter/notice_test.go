// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubetest"
)

const (
	hookDeployment = "impact-ai-reporter"
	hookSelector   = "app.kubernetes.io/name=reporter,app.kubernetes.io/instance=impact-ai"
	hookScalePath  = "/apis/apps/v1/namespaces/impact-system/deployments/impact-ai-reporter/scale"
	hookPodsPath   = "/api/v1/namespaces/impact-system/pods"
)

// clock moves only when the code under test sleeps, so waits cost nothing.
type clock struct {
	mu    sync.Mutex
	now   time.Time
	slept time.Duration
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) Sleep(ctx context.Context, d time.Duration) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	c.slept += d
	return ctx.Err()
}

// clockDeps makes time, sleeping and backoff jitter deterministic.
func clockDeps(c *clock) deps {
	return deps{now: c.Now, sleep: c.Sleep, jitter: func() float64 { return 0.5 }}
}

func podListJSON(uids ...string) string {
	items := make([]map[string]any, len(uids))
	for i, uid := range uids {
		items[i] = map[string]any{
			"metadata": map[string]any{"name": "reporter-" + uid, "namespace": "impact-system", "uid": uid},
			"status":   map[string]any{"phase": "Running"},
		}
	}
	b, _ := json.Marshal(map[string]any{"kind": "PodList", "items": items})
	return string(b)
}

// noticeAPI is the fixture cluster, which also plays ingest, with a reporter
// whose two pods are gone on the third look.
func noticeAPI(t *testing.T) *kubetest.Server {
	t.Helper()
	api := fixtureAPI(t)
	api.HandleSequence(http.MethodPatch, hookScalePath, kubetest.Response{Body: `{"kind":"Scale"}`})
	api.HandleSequence(http.MethodGet, hookPodsPath,
		kubetest.Response{Body: podListJSON("old-1", "old-2")}, kubetest.Response{Body: podListJSON("old-2")}, kubetest.Response{Body: podListJSON()})
	api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: http.StatusAccepted})
	return api
}

func noticeArgs(api *kubetest.Server, keyFile string, extra ...string) []string {
	args := append(baseArgs(api), "--uninstall-notice", "--ingest-endpoint="+api.URL, "--ingest-key-file="+keyFile,
		"--stop-deployment="+hookDeployment, "--stop-pod-selector="+hookSelector)
	return append(args, extra...)
}

func calls(api *kubetest.Server) []string {
	var out []string
	for _, r := range api.Requests() {
		out = append(out, r.Method+" "+r.Path)
	}
	return out
}

func postedNotices(t *testing.T, api *kubetest.Server) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, r := range api.Requests() {
		if r.Method == http.MethodPost && r.Path == "/v1/inventory" {
			var snap map[string]any
			if err := json.Unmarshal(gunzip(t, r.Body), &snap); err != nil {
				t.Fatal(err)
			}
			out = append(out, snap)
		}
	}
	return out
}

func TestUninstallNoticeStopsTheReporterThenSendsOneNotice(t *testing.T) {
	api := noticeAPI(t)
	clk := &clock{now: fixedNow}
	var stderr bytes.Buffer
	code := run(context.Background(), noticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, &stderr, clockDeps(clk))
	if code != 0 {
		t.Fatalf("exit %d, stderr:\n%s", code, stderr.String())
	}

	want := []string{
		"PATCH " + hookScalePath,
		"GET " + hookPodsPath, "GET " + hookPodsPath, "GET " + hookPodsPath,
		"GET /version", "GET /api/v1/namespaces/kube-system", "GET /api/v1/nodes",
		"POST /v1/inventory",
	}
	if got := calls(api); !reflect.DeepEqual(got, want) {
		t.Fatalf("calls =\n%v\nwant\n%v", got, want)
	}

	notices := postedNotices(t, api)
	if len(notices) != 1 {
		t.Fatalf("%d notices", len(notices))
	}
	n := notices[0]
	reporter := n["reporter"].(map[string]any)
	if reporter["mode"] != "uninstalled" || reporter["clusterOwner"] != nil || reporter["instance"] != "5b1d0f4c-0000-4000-8000-000000000001" {
		t.Errorf("reporter = %v", reporter)
	}
	if w, ok := n["workloads"].([]any); !ok || len(w) != 0 {
		t.Errorf("workloads = %#v", n["workloads"])
	}
	if n["cluster"].(map[string]any)["uid"] != "4e5622e7-f420-499c-9662-50af7f8a64b9" || n["cluster"].(map[string]any)["name"] != "prod-eks" {
		t.Errorf("cluster = %v", n["cluster"])
	}

	// A reporter that started ten minutes before, with an interval of five
	// seconds, has sent 120 snapshots by now. The notice is above the last one.
	lastReporterSequence := fixedNow.Add(-10*time.Minute).Unix() + 120
	if seq := int64(n["sequence"].(float64)); seq <= lastReporterSequence || seq != fixedNow.Add(2*time.Second).Unix() {
		t.Errorf("sequence = %v, want the Unix time at send, above %d", n["sequence"], lastReporterSequence)
	}
	if strings.Contains(stderr.String(), testKey) {
		t.Errorf("the key was logged: %s", stderr.String())
	}
}

func TestUninstallNoticeUsageErrorsExitTwo(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no endpoint", []string{"--uninstall-notice"}, "ingest endpoint"},
		{"endpoint that is no URL", []string{"--uninstall-notice", "--ingest-endpoint=ingest"}, "ingest endpoint"},
		{"stop timeout of zero", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-timeout=0s"}, "stop-timeout"},
		{"notice timeout that is no duration", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--notice-timeout=soon"}, "notice-timeout"},
		{"a deployment but no namespace", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-deployment=reporter"}, "stop namespace"},
		{"an unknown flag", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-everything"}, "stop-everything"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var stderr bytes.Buffer
			if code := run(context.Background(), tc.args, env(), io.Discard, &stderr, testDeps()); code != 2 || !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("exit %d, stderr %q; want 2 mentioning %q", code, stderr.String(), tc.want)
			}
		})
	}
}

// Whatever goes wrong, the hook ends with exit code 0 so that helm uninstall
// carries on, and the failure is in the log.
func TestUninstallNoticeNeverBlocksTheUninstall(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL
	closed.Close()

	cases := []struct {
		name  string
		setup func(api *kubetest.Server) (extra []string, args func([]string) []string)
	}{
		{"ingest keeps answering 500", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 500})
			return nil, nil
		}},
		{"ingest refuses the key", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 401})
			return nil, nil
		}},
		{"ingest refuses the body", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 400})
			return nil, nil
		}},
		{"ingest asks to wait a long time", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 503, Header: map[string]string{"Retry-After": "3600"}})
			return nil, nil
		}},
		{"ingest cannot be reached", func(api *kubetest.Server) ([]string, func([]string) []string) {
			return nil, func(a []string) []string { return append(a, "--ingest-endpoint="+closedURL) }
		}},
		{"the scale is forbidden", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPatch, hookScalePath, kubetest.Response{Status: 403})
			return nil, nil
		}},
		{"the pods cannot be listed", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodGet, hookPodsPath, kubetest.Response{Status: 403})
			return nil, nil
		}},
		{"the old pods never go away", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodGet, hookPodsPath, kubetest.Response{Body: podListJSON("old-1")})
			return []string{"--stop-timeout=3s"}, nil
		}},
		{"the cluster cannot be identified", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleStatus("/api/v1/namespaces/kube-system", 500, `{}`)
			return nil, nil
		}},
		{"the API server is down", func(api *kubetest.Server) ([]string, func([]string) []string) {
			return []string{"--stop-timeout=3s"}, func(a []string) []string { return append(a, "--api-server="+closedURL) }
		}},
		{"the notice budget runs out", func(api *kubetest.Server) ([]string, func([]string) []string) {
			api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 503})
			return []string{"--notice-timeout=5s"}, nil
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := noticeAPI(t)
			extra, mod := tc.setup(api)
			args := noticeArgs(api, writeKeyFile(t), extra...)
			if mod != nil {
				args = mod(args)
			}
			var stderr bytes.Buffer
			code := run(context.Background(), args, testEnv(), io.Discard, &stderr, clockDeps(&clock{now: fixedNow}))
			if code != 0 {
				t.Errorf("exit %d; the uninstall must never be blocked. stderr:\n%s", code, stderr.String())
			}
			if !strings.Contains(stderr.String(), `"level":"WARN"`) && !strings.Contains(stderr.String(), `"level":"ERROR"`) {
				t.Errorf("the failure was not logged:\n%s", stderr.String())
			}
			if strings.Contains(stderr.String(), testKey) {
				t.Errorf("the key was logged")
			}
		})
	}

	t.Run("no key file", func(t *testing.T) {
		api := noticeAPI(t)
		var stderr bytes.Buffer
		code := run(context.Background(), noticeArgs(api, filepath.Join(t.TempDir(), "missing")), testEnv(), io.Discard, &stderr, clockDeps(&clock{now: fixedNow}))
		if code != 0 || !strings.Contains(stderr.String(), `"level":"ERROR"`) || len(postedNotices(t, api)) != 0 {
			t.Errorf("exit %d, %d notices, stderr:\n%s", code, len(postedNotices(t, api)), stderr.String())
		}
	})

	t.Run("not running in a cluster", func(t *testing.T) {
		var stderr bytes.Buffer
		args := []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--ingest-key-file=" + writeKeyFile(t)}
		if code := run(context.Background(), args, env(), io.Discard, &stderr, clockDeps(&clock{now: fixedNow})); code != 0 {
			t.Errorf("exit %d", code)
		}
		if !strings.Contains(stderr.String(), "not running in a cluster") {
			t.Errorf("not logged: %s", stderr.String())
		}
	})

	t.Run("the context ends", func(t *testing.T) {
		api := noticeAPI(t)
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if code := run(ctx, noticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, io.Discard, clockDeps(&clock{now: fixedNow})); code != 0 {
			t.Errorf("exit %d", code)
		}
	})
}

func TestUninstallNoticeHonoursTheStopTimeout(t *testing.T) {
	api := noticeAPI(t)
	api.HandleSequence(http.MethodGet, hookPodsPath, kubetest.Response{Body: podListJSON("old-1")})
	clk := &clock{now: fixedNow}
	code := run(context.Background(), noticeArgs(api, writeKeyFile(t), "--stop-timeout=4s"), testEnv(), io.Discard, io.Discard, clockDeps(clk))
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if clk.slept != 4*time.Second {
		t.Errorf("waited %v, want the 4s stop timeout", clk.slept)
	}
	if n := len(postedNotices(t, api)); n != 1 {
		t.Errorf("%d notices; after the timeout the notice still goes out", n)
	}
}

func TestUninstallNoticeHonoursTheNoticeTimeout(t *testing.T) {
	api := noticeAPI(t)
	api.HandleSequence(http.MethodPost, "/v1/inventory", kubetest.Response{Status: 503})
	clk := &clock{now: fixedNow}
	code := run(context.Background(), noticeArgs(api, writeKeyFile(t), "--notice-timeout=6s"), testEnv(), io.Discard, io.Discard, clockDeps(clk))
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	// 2s waiting for the pods, then retries that stop at the 6s budget:
	// attempts at 0s, 1s and 3s; the next pause (4s) is cut at 6s.
	if got := len(postedNotices(t, api)); got != 3 || clk.slept != 8*time.Second {
		t.Errorf("%d attempts, %v slept; want 3 attempts and 2s+6s", got, clk.slept)
	}
}

func TestUninstallNoticeDryRunPrintsTheNoticeAndChangesNothing(t *testing.T) {
	api := noticeAPI(t)
	var stdout, stderr bytes.Buffer
	args := append(baseArgs(api), "--dry-run", "--uninstall-notice", "--stop-deployment="+hookDeployment, "--stop-pod-selector="+hookSelector)
	code := run(context.Background(), args, testEnv(), &stdout, &stderr, clockDeps(&clock{now: fixedNow}))
	if code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "{\n  \"") {
		t.Errorf("want indented JSON, got %.60q", stdout.String())
	}
	var n map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &n); err != nil {
		t.Fatal(err)
	}
	if n["reporter"].(map[string]any)["mode"] != "uninstalled" || len(n["workloads"].([]any)) != 0 || n["sequence"] != float64(fixedNow.Unix()) {
		t.Errorf("notice = %v", n)
	}
	for _, r := range api.Requests() {
		if r.Method != http.MethodGet {
			t.Errorf("dry run made a %s request to %s", r.Method, r.Path)
		}
		if strings.HasSuffix(r.Path, "/pods") || strings.HasSuffix(r.Path, "/deployments") {
			t.Errorf("dry run read %s", r.Path)
		}
	}
}

func TestUninstallNoticeCommandCarriesTheCloudIdentity(t *testing.T) {
	api := noticeAPI(t)
	var stdout, stderr bytes.Buffer
	args := append(baseArgs(api), "--dry-run", "--uninstall-notice", "--cloud-provider=gcp", "--cloud-account-id=my-project", "--cloud-region=europe-west1")
	if code := run(context.Background(), args, testEnv(), &stdout, &stderr, clockDeps(&clock{now: fixedNow})); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	var n struct {
		Cluster map[string]any `json:"cluster"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &n); err != nil {
		t.Fatal(err)
	}
	if n.Cluster["cloudProvider"] != "gcp" || n.Cluster["accountId"] != "my-project" || n.Cluster["region"] != "europe-west1" {
		t.Errorf("cluster in the notice = %v", n.Cluster)
	}
	if _, ok := n.Cluster["arn"]; ok {
		t.Errorf("arn was not given but the notice has one: %v", n.Cluster)
	}
}

func TestUninstallNoticeDryRunStillFailsLoudly(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL
	closed.Close()
	var stderr bytes.Buffer
	code := run(context.Background(), []string{"--dry-run", "--uninstall-notice", "--api-server=" + closedURL}, env(), io.Discard, &stderr, clockDeps(&clock{now: fixedNow}))
	if code != 1 {
		t.Errorf("exit %d, want 1: a dry run is a tool for people, only the real hook always exits 0", code)
	}
}

func TestUninstallNoticeDoesNotOpenTheHealthPort(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	api := noticeAPI(t)
	code := run(context.Background(), noticeArgs(api, writeKeyFile(t), "--listen="+l.Addr().String()), testEnv(), io.Discard, io.Discard, clockDeps(&clock{now: fixedNow}))
	if code != 0 || len(postedNotices(t, api)) != 1 {
		t.Errorf("exit %d with %d notices; the hook must not try to listen", code, len(postedNotices(t, api)))
	}
}

func TestUninstallNoticeDoesNotWaitForItsOwnPod(t *testing.T) {
	api := noticeAPI(t)
	// The selector also matches the hook's own pod, whose UID is POD_UID.
	api.HandleSequence(http.MethodGet, hookPodsPath, kubetest.Response{Body: podListJSON("5b1d0f4c-0000-4000-8000-000000000001")})
	clk := &clock{now: fixedNow}
	if code := run(context.Background(), noticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, io.Discard, clockDeps(clk)); code != 0 {
		t.Fatal(code)
	}
	if clk.slept != 0 {
		t.Errorf("waited %v for itself", clk.slept)
	}
}
