// SPDX-License-Identifier: Apache-2.0

package notice

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/inventory"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kube"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kubetest"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/loop"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/sender"
)

const (
	ns        = "impact-system"
	deploy    = "impact-cluster-kit-reporter"
	selector  = "app.kubernetes.io/name=reporter,app.kubernetes.io/instance=impact-cluster-kit"
	scalePath = "/apis/apps/v1/namespaces/impact-system/deployments/impact-cluster-kit-reporter/scale"
	podsPath  = "/api/v1/namespaces/impact-system/pods"
	ingest    = "/v1/inventory"
	selfUID   = "hook-pod-uid"
	fakeKey   = "sk-live-abcdefghijklmnop"
	testKey   = "ik_test_0123456789abcdef"
)

var start = time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)

// fakeClock is a clock that moves only when the code under test sleeps.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	sleeps []time.Duration
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(ctx context.Context, d time.Duration) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sleeps = append(c.sleeps, d)
	c.now = c.now.Add(d)
	return ctx.Err()
}

func (c *fakeClock) slept() time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	var total time.Duration
	for _, d := range c.sleeps {
		total += d
	}
	return total
}

func podList(uids ...string) string {
	items := make([]map[string]any, len(uids))
	for i, uid := range uids {
		items[i] = map[string]any{
			"metadata": map[string]any{"name": "reporter-" + uid, "namespace": ns, "uid": uid, "deletionTimestamp": "2026-11-03T12:00:00Z"},
			"status":   map[string]any{"phase": "Running"},
		}
	}
	b, _ := json.Marshal(map[string]any{"kind": "PodList", "metadata": map[string]any{}, "items": items})
	return string(b)
}

type rig struct {
	t      *testing.T
	srv    *kubetest.Server
	clock  *fakeClock
	logs   *bytes.Buffer
	runner *Runner
	key    string
}

// newRig builds a runner against a fake cluster that doubles as ingest. By
// default the old reporter has two pods that disappear on the third look.
func newRig(t *testing.T) *rig {
	t.Helper()
	r := &rig{t: t, srv: kubetest.New(t), clock: &fakeClock{now: start}, logs: &bytes.Buffer{}}
	srv := r.srv
	srv.Handle("/version", map[string]any{"gitVersion": "v1.31.2-eks-abc"})
	srv.Handle("/api/v1/namespaces/kube-system", map[string]any{"metadata": map[string]any{"name": "kube-system", "uid": "4e5622e7-f420-499c-9662-50af7f8a64b9"}})
	srv.HandleList("/api/v1/nodes", []any{map[string]any{
		"metadata": map[string]any{"labels": map[string]any{"eks.amazonaws.com/nodegroup": "ng", "topology.kubernetes.io/region": "us-east-1"}},
		"status":   map[string]any{"nodeInfo": map[string]any{"kubeletVersion": "v1.31.2-eks-abc"}}}}, 0)
	srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Body: `{"kind":"Scale","spec":{"replicas":0}}`})
	srv.HandleSequence(http.MethodGet, podsPath,
		kubetest.Response{Body: podList("old-1", "old-2")},
		kubetest.Response{Body: podList("old-2")},
		kubetest.Response{Body: podList()})
	srv.HandleSequence(http.MethodPost, ingest, kubetest.Response{Status: http.StatusAccepted})

	keyFile := filepath.Join(t.TempDir(), "ingest-key")
	if err := os.WriteFile(keyFile, []byte(testKey+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewJSONHandler(r.logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	client, err := kube.New(kube.Config{Host: srv.URL, TokenFile: ""})
	if err != nil {
		t.Fatal(err)
	}
	collector := inventory.NewCollector(client, inventory.Options{
		ClusterName: "prod-eks", Version: "0.1.0", Instance: selfUID, Now: r.clock.Now, Log: log,
	})
	r.runner = &Runner{
		Kube:    client,
		Builder: collector,
		Ingest:  sender.Sender{Endpoint: srv.URL, UserAgent: "test", Jitter: func() float64 { return 0.5 }, Log: log},
		KeyFile: keyFile,
		Options: Options{
			StopDeployment: deploy, StopNamespace: ns, PodSelector: selector,
			StopTimeout: 60 * time.Second, NoticeTimeout: 60 * time.Second, SelfUID: selfUID,
		},
		Now:   r.clock.Now,
		Sleep: r.clock.Sleep,
		Log:   log,
	}
	return r
}

func (r *rig) run() Result {
	r.t.Helper()
	return r.runner.Run(context.Background())
}

func (r *rig) calls() []string {
	var out []string
	for _, req := range r.srv.Requests() {
		out = append(out, req.Method+" "+req.Path)
	}
	return out
}

func (r *rig) posts() []kubetest.Request {
	var out []kubetest.Request
	for _, req := range r.srv.Requests() {
		if req.Method == http.MethodPost && req.Path == ingest {
			out = append(out, req)
		}
	}
	return out
}

func gunzip(t *testing.T, raw []byte) []byte {
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

func TestStopsTheReporterWaitsForItsPodsAndOnlyThenSendsOneNotice(t *testing.T) {
	r := newRig(t)
	res := r.run()

	want := []string{
		"PATCH " + scalePath,
		"GET " + podsPath, "GET " + podsPath, "GET " + podsPath,
		"GET /version", "GET /api/v1/namespaces/kube-system", "GET /api/v1/nodes",
		"POST " + ingest,
	}
	if got := r.calls(); !reflect.DeepEqual(got, want) {
		t.Fatalf("calls =\n%v\nwant\n%v", got, want)
	}
	if res.Scale != Scaled || res.Wait != PodsGone || res.Polls != 3 || !res.Built || res.Sent.Outcome != sender.Accepted {
		t.Errorf("result = %+v", res)
	}
	if !reflect.DeepEqual(r.clock.sleeps, []time.Duration{time.Second, time.Second}) {
		t.Errorf("sleeps = %v, want a one second pause after each look that found pods", r.clock.sleeps)
	}
	if n := len(r.posts()); n != 1 {
		t.Errorf("%d notices sent, want exactly one", n)
	}
}

func TestTheScaleRequestIsAJSONMergePatchToZeroReplicas(t *testing.T) {
	r := newRig(t)
	r.run()
	var patch kubetest.Request
	for _, req := range r.srv.Requests() {
		if req.Method == http.MethodPatch {
			patch = req
		}
	}
	if patch.Path != scalePath || patch.ContentType != "application/merge-patch+json" || string(patch.Body) != `{"spec":{"replicas":0}}` {
		t.Errorf("patch = %s %q %q", patch.Path, patch.ContentType, patch.Body)
	}
}

func TestPodsAreFoundByLabelAndFinishedOnesAreLeftOut(t *testing.T) {
	r := newRig(t)
	r.run()
	reqs := r.srv.RequestsFor(podsPath)
	if len(reqs) == 0 {
		t.Fatal("no pod list")
	}
	values, _ := url.ParseQuery(reqs[0].Query)
	if values.Get("labelSelector") != selector {
		t.Errorf("labelSelector = %q", values.Get("labelSelector"))
	}
	if fs := values.Get("fieldSelector"); !strings.Contains(fs, "status.phase!=Succeeded") || !strings.Contains(fs, "status.phase!=Failed") {
		t.Errorf("fieldSelector = %q; pods that have already exited must not be waited for", fs)
	}
}

func TestTheNoticeBodyIsUninstalledEmptyAndAboveTheClock(t *testing.T) {
	r := newRig(t)
	res := r.run()
	posts := r.posts()
	if len(posts) != 1 {
		t.Fatalf("%d posts", len(posts))
	}
	p := posts[0]
	if p.AuthHead != "Bearer "+testKey || p.ContentType != "application/json" {
		t.Errorf("headers: auth %q content-type %q", p.AuthHead, p.ContentType)
	}
	var snap struct {
		Schema   string `json:"schema"`
		Sequence int64  `json:"sequence"`
		Observed string `json:"observedAt"`
		Cluster  struct {
			UID      string  `json:"uid"`
			Name     *string `json:"name"`
			Provider string  `json:"provider"`
			Region   *string `json:"region"`
			Version  string  `json:"kubernetesVersion"`
		} `json:"cluster"`
		Reporter struct {
			Mode     string  `json:"mode"`
			Owner    *string `json:"clusterOwner"`
			Version  string  `json:"version"`
			Instance string  `json:"instance"`
		} `json:"reporter"`
		Workloads []json.RawMessage `json:"workloads"`
	}
	body := gunzip(t, p.Body)
	if err := json.Unmarshal(body, &snap); err != nil {
		t.Fatal(err)
	}
	if snap.Schema != "impact.inventory.v1" || snap.Reporter.Mode != "uninstalled" || snap.Reporter.Owner != nil || snap.Reporter.Instance != selfUID {
		t.Errorf("snapshot = %+v", snap)
	}
	if snap.Workloads == nil || len(snap.Workloads) != 0 || !bytes.Contains(body, []byte(`"workloads":[]`)) {
		t.Errorf("workloads must be an empty list: %s", body)
	}
	if snap.Cluster.UID != "4e5622e7-f420-499c-9662-50af7f8a64b9" || snap.Cluster.Provider != "eks" || snap.Cluster.Version != "1.31.2" ||
		snap.Cluster.Name == nil || *snap.Cluster.Name != "prod-eks" || snap.Cluster.Region == nil || *snap.Cluster.Region != "us-east-1" {
		t.Errorf("cluster = %+v", snap.Cluster)
	}
	// The sequence is the clock at send time: two seconds of waiting after the start.
	if want := start.Add(2 * time.Second).Unix(); snap.Sequence != want || res.Sequence != want {
		t.Errorf("sequence = %d (result %d), want the Unix time at send, %d", snap.Sequence, res.Sequence, want)
	}
	if snap.Observed != "2026-11-03T12:00:02Z" {
		t.Errorf("observedAt = %q", snap.Observed)
	}
}

// A running reporter starts at the Unix time and adds one per interval, and the
// interval is at least five seconds, so the clock at the time of the notice is
// always above the last sequence it sent.
func TestTheNoticeSequenceIsAboveEverySequenceARunningReporterSent(t *testing.T) {
	cases := []struct {
		interval time.Duration
		elapsed  []time.Duration
	}{
		{5 * time.Second, []time.Duration{time.Second, 4 * time.Second, 5 * time.Second, 59 * time.Second, 61 * time.Second, 10 * time.Minute}},
		{30 * time.Second, []time.Duration{time.Second, 29 * time.Second, 30 * time.Second, 31 * time.Second, 10 * time.Minute, 2 * time.Hour}},
		{5 * time.Minute, []time.Duration{time.Second, 5 * time.Minute, 10 * time.Minute, 26 * time.Hour}},
	}
	for _, tc := range cases {
		for _, elapsed := range tc.elapsed {
			clock := &fakeClock{now: start}
			snaps := &sequences{}
			rep := newReporter(t, clock, snaps, tc.interval)
			for at := time.Duration(0); at <= elapsed; at += tc.interval {
				clock.mu.Lock()
				clock.now = start.Add(at)
				clock.mu.Unlock()
				rep.Cycle(context.Background())
			}
			noticeSequence := start.Add(elapsed).Unix()
			if noticeSequence <= snaps.last {
				t.Errorf("interval %v, %v after the start: notice %d is not above the reporter's last %d", tc.interval, elapsed, noticeSequence, snaps.last)
			}
		}
	}
}

func TestAMissingScaleTargetIsFine(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Status: http.StatusNotFound, Body: `{"message":"deployments.apps not found"}`})
	res := r.run()
	if res.Scale != AlreadyGone {
		t.Errorf("scale outcome = %v, want already gone", res.Scale)
	}
	if res.Wait != PodsGone || res.Sent.Outcome != sender.Accepted || len(r.posts()) != 1 {
		t.Errorf("the notice must still go out after the wait: %+v", res)
	}
}

func TestOtherScaleErrorsAreLoggedAndTheNoticeStillGoesOut(t *testing.T) {
	for _, status := range []int{http.StatusForbidden, http.StatusConflict, http.StatusInternalServerError} {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Status: status, Body: `{"message":"secret detail"}`})
		res := r.run()
		if res.Scale != ScaleFailed || res.Sent.Outcome != sender.Accepted {
			t.Errorf("status %d: result = %+v", status, res)
		}
		if !strings.Contains(r.logs.String(), `"level":"WARN"`) || !strings.Contains(r.logs.String(), fmt.Sprint(status)) {
			t.Errorf("status %d: the failure was not logged: %s", status, r.logs)
		}
		if strings.Contains(r.logs.String(), "secret detail") {
			t.Errorf("status %d: the response body was logged", status)
		}
	}
}

func TestTerminatingPodsStillCountAsPods(t *testing.T) {
	r := newRig(t)
	// The same pod, already deleted but not yet gone, on every look until the third.
	r.srv.HandleSequence(http.MethodGet, podsPath,
		kubetest.Response{Body: podList("old-1")}, kubetest.Response{Body: podList("old-1")}, kubetest.Response{Body: podList("old-1")}, kubetest.Response{Body: podList()})
	res := r.run()
	if res.Polls != 4 || res.Wait != PodsGone {
		t.Errorf("polls = %d, outcome %v; a pod with a deletion timestamp is still there", res.Polls, res.Wait)
	}
	first, last := r.srv.RequestsFor(podsPath), r.posts()
	if len(last) != 1 || !last[0].At.After(first[len(first)-1].At) {
		t.Error("the notice was sent before the last look at the pods")
	}
}

func TestOurOwnPodIsNeverWaitedFor(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Body: podList(selfUID)})
	res := r.run()
	if res.Polls != 1 || res.Wait != PodsGone {
		t.Errorf("polls = %d, outcome %v; the hook's own pod must not hold it up", res.Polls, res.Wait)
	}
}

func TestTheStopTimeoutIsHonouredAndTheNoticeStillGoesOut(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Body: podList("old-1")})
	r.runner.Options.StopTimeout = 5 * time.Second
	res := r.run()
	if res.Wait != WaitTimedOut || res.Polls != 6 {
		t.Errorf("wait %v after %d polls, want a timeout after looking at t=0..5s", res.Wait, res.Polls)
	}
	if got := r.clock.slept(); got != 5*time.Second {
		t.Errorf("waited %v, want exactly the stop timeout", got)
	}
	if res.Sent.Outcome != sender.Accepted || len(r.posts()) != 1 {
		t.Errorf("after the timeout the notice is sent anyway: %+v", res)
	}
	if !strings.Contains(r.logs.String(), "timed out") {
		t.Errorf("the timeout was not logged: %s", r.logs)
	}
}

func TestAPartialSecondIsNotOverslept(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Body: podList("old-1")})
	r.runner.Options.StopTimeout = 2500 * time.Millisecond
	res := r.run()
	if res.Wait != WaitTimedOut || r.clock.slept() != 2500*time.Millisecond {
		t.Errorf("wait %v, slept %v; the last pause must end at the deadline", res.Wait, r.clock.slept())
	}
}

func TestTheNoticeTimeoutBoundsBuildingAndSendingWithRetries(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodPost, ingest, kubetest.Response{Status: http.StatusServiceUnavailable})
	r.runner.Options.NoticeTimeout = 10 * time.Second
	before := r.clock.slept()
	res := r.run()
	// Attempts at 0s, 1s, 3s and 7s; the next pause (8s) would pass the 10s budget.
	if n := len(r.posts()); n != 4 {
		t.Errorf("%d attempts, want 4", n)
	}
	if res.Sent.Outcome != sender.Canceled {
		t.Errorf("outcome = %v, want the send to stop at the budget", res.Sent.Outcome)
	}
	// The wait for the pods took 2s; the notice used exactly its 10s.
	if used := r.clock.slept() - before; used != 12*time.Second {
		t.Errorf("slept %v in total, want 2s of waiting plus the 10s budget", used)
	}
}

func TestARetryAfterBeyondTheBudgetEndsTheSendAtOnce(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodPost, ingest, kubetest.Response{Status: http.StatusServiceUnavailable, Header: map[string]string{"Retry-After": "600"}})
	res := r.run()
	if res.Sent.Outcome != sender.GaveUp || res.Sent.Attempts != 1 || len(r.posts()) != 1 {
		t.Errorf("result = %+v", res.Sent)
	}
	if r.clock.slept() != 2*time.Second {
		t.Errorf("slept %v; only the wait for the pods may sleep", r.clock.slept())
	}
}

func TestTransientIngestFailuresAreRetriedWithTheSameNotice(t *testing.T) {
	r := newRig(t)
	r.srv.HandleSequence(http.MethodPost, ingest,
		kubetest.Response{Status: http.StatusServiceUnavailable, Header: map[string]string{"Retry-After": "3"}},
		kubetest.Response{Status: http.StatusBadGateway},
		kubetest.Response{Status: http.StatusAccepted})
	res := r.run()
	if res.Sent.Outcome != sender.Accepted || res.Sent.Attempts != 3 {
		t.Fatalf("result = %+v", res.Sent)
	}
	posts := r.posts()
	for i, p := range posts {
		if !bytes.Equal(p.Body, posts[0].Body) {
			t.Errorf("attempt %d sent a different body", i+1)
		}
	}
}

func TestRejectionsAreNotRetriedAndNeverStopTheRun(t *testing.T) {
	for status, want := range map[int]sender.Outcome{400: sender.Rejected, 413: sender.Rejected, 401: sender.KeyRejected, 403: sender.KeyRejected} {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodPost, ingest, kubetest.Response{Status: status})
		res := r.run()
		if res.Sent.Outcome != want || len(r.posts()) != 1 {
			t.Errorf("status %d: outcome %v after %d attempts", status, res.Sent.Outcome, len(r.posts()))
		}
	}
}

func TestPodListFailures(t *testing.T) {
	t.Run("a forbidden list ends the wait at once", func(t *testing.T) {
		for _, status := range []int{http.StatusForbidden, http.StatusUnauthorized, http.StatusBadRequest} {
			r := newRig(t)
			r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Status: status})
			res := r.run()
			if res.Wait != WaitFailed || res.Polls != 1 || res.Sent.Outcome != sender.Accepted {
				t.Errorf("status %d: result = %+v", status, res)
			}
		}
	})
	t.Run("a missing namespace means no pods", func(t *testing.T) {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Status: http.StatusNotFound})
		if res := r.run(); res.Wait != PodsGone || res.Polls != 1 {
			t.Errorf("result = %+v", res)
		}
	})
	t.Run("server errors are retried until the pods are gone", func(t *testing.T) {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodGet, podsPath,
			kubetest.Response{Status: http.StatusInternalServerError}, kubetest.Response{Status: http.StatusServiceUnavailable}, kubetest.Response{Body: podList()})
		if res := r.run(); res.Wait != PodsGone || res.Polls != 3 {
			t.Errorf("result = %+v", res)
		}
	})
	t.Run("server errors that never stop end at the stop timeout", func(t *testing.T) {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Status: http.StatusInternalServerError})
		r.runner.Options.StopTimeout = 3 * time.Second
		if res := r.run(); res.Wait != WaitTimedOut || res.Sent.Outcome != sender.Accepted {
			t.Errorf("result = %+v", res)
		}
	})
}

func TestWithoutADeploymentNameNothingIsScaled(t *testing.T) {
	r := newRig(t)
	r.runner.Options.StopDeployment = ""
	res := r.run()
	for _, c := range r.calls() {
		if strings.HasPrefix(c, "PATCH") {
			t.Errorf("unexpected write: %s", c)
		}
	}
	if res.Scale != ScaleSkipped || res.Wait != PodsGone {
		t.Errorf("result = %+v; with a selector the pods are still waited for", res)
	}
}

func TestWithoutASelectorThereIsNothingToWaitFor(t *testing.T) {
	r := newRig(t)
	r.runner.Options.PodSelector = ""
	res := r.run()
	if res.Wait != WaitSkipped || len(r.srv.RequestsFor(podsPath)) != 0 {
		t.Errorf("result = %+v", res)
	}
	if res.Scale != Scaled || !strings.Contains(r.logs.String(), "selector") {
		t.Errorf("scale %v; a missing selector should be logged: %s", res.Scale, r.logs)
	}

	r = newRig(t)
	r.runner.Options.PodSelector, r.runner.Options.StopDeployment = "", ""
	res = r.run()
	if res.Scale != ScaleSkipped || res.Wait != WaitSkipped || res.Sent.Outcome != sender.Accepted {
		t.Errorf("a notice alone must work: %+v", res)
	}
	if got := r.calls(); got[0] != "GET /version" {
		t.Errorf("calls = %v; nothing is written or listed before the notice", got)
	}
}

func TestNoKeyMeansNoNoticeAndNoCrash(t *testing.T) {
	for name, setup := range map[string]func(*rig){
		"missing": func(r *rig) { _ = os.Remove(r.runner.KeyFile) },
		"empty":   func(r *rig) { _ = os.WriteFile(r.runner.KeyFile, []byte(" \n"), 0o600) },
	} {
		r := newRig(t)
		setup(r)
		res := r.run()
		if res.Built || len(r.posts()) != 0 {
			t.Errorf("%s key: %+v, %d posts", name, res, len(r.posts()))
		}
		if !strings.Contains(r.logs.String(), `"level":"ERROR"`) {
			t.Errorf("%s key: not logged as an error: %s", name, r.logs)
		}
	}
}

func TestBuildingTheNoticeIsRetriedAndThenGivenUp(t *testing.T) {
	t.Run("a transient failure", func(t *testing.T) {
		r := newRig(t)
		r.srv.HandleSequence(http.MethodGet, "/version", kubetest.Response{Status: http.StatusInternalServerError}, kubetest.Response{Body: `{"gitVersion":"v1.31.2"}`})
		res := r.run()
		if !res.Built || res.Sent.Outcome != sender.Accepted {
			t.Errorf("result = %+v", res)
		}
		// The sequence is taken at the successful attempt, after the pause.
		if res.Sequence != start.Add(3*time.Second).Unix() {
			t.Errorf("sequence = %d", res.Sequence)
		}
	})
	t.Run("a permanent failure", func(t *testing.T) {
		r := newRig(t)
		r.srv.HandleStatus("/api/v1/namespaces/kube-system", http.StatusForbidden, `{}`)
		res := r.run()
		if res.Built || len(r.posts()) != 0 {
			t.Errorf("result = %+v", res)
		}
		if n := len(r.srv.RequestsFor("/api/v1/namespaces/kube-system")); n != 3 {
			t.Errorf("%d attempts to read the cluster, want 3", n)
		}
		if !strings.Contains(r.logs.String(), "uninstall notice") {
			t.Errorf("not logged: %s", r.logs)
		}
	})
}

// AE-3 for the notice: no workload names, no env names, no values.
func TestTheNoticeCarriesNoNamesOfWorkloadsAndNoValues(t *testing.T) {
	r := newRig(t)
	app := map[string]any{
		"metadata": map[string]any{"annotations": map[string]any{"kubectl.kubernetes.io/last-applied-configuration": "{" + fakeKey + "}"}},
		"spec": map[string]any{"containers": []any{map[string]any{
			"name": "app", "image": "x", "command": []any{"/bin/sh", "-c", "--api-key=" + fakeKey},
			"env": []any{map[string]any{"name": "OPENAI_API_KEY", "value": fakeKey}},
		}}},
	}
	deployment := map[string]any{"metadata": map[string]any{"name": "support-assistant", "namespace": "shop"}, "spec": map[string]any{"replicas": 2, "template": app}}
	r.srv.HandleList("/apis/apps/v1/deployments", []any{deployment}, 0)
	r.srv.HandleList("/api/v1/pods", []any{map[string]any{"metadata": map[string]any{"name": "debug", "namespace": "shop"}, "spec": app["spec"]}}, 0)

	r.run()
	posts := r.posts()
	if len(posts) != 1 {
		t.Fatalf("%d posts", len(posts))
	}
	body := string(gunzip(t, posts[0].Body))
	for _, leak := range []string{"support-assistant", "shop", "debug", "OPENAI_API_KEY", fakeKey, "sk-live", "--api-key", "last-applied", testKey, "/bin/sh"} {
		if strings.Contains(body, leak) {
			t.Errorf("the notice contains %q: %s", leak, body)
		}
	}
	for _, path := range []string{"/apis/apps/v1/deployments", "/apis/apps/v1/statefulsets", "/apis/batch/v1/jobs"} {
		if n := len(r.srv.RequestsFor(path)); n != 0 {
			t.Errorf("%s was read %d times; a notice needs no workloads", path, n)
		}
	}
	// The pod list that the wait uses is namespaced and only asks about the reporter.
	for _, req := range r.srv.RequestsFor("/api/v1/pods") {
		t.Errorf("cluster-wide pod list: %+v", req)
	}
	if strings.Contains(r.logs.String(), fakeKey) || strings.Contains(r.logs.String(), testKey) {
		t.Errorf("a key was logged: %s", r.logs)
	}
}

func TestACanceledContextStopsTheRunWithoutSendingAnything(t *testing.T) {
	r := newRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	res := r.runner.Run(ctx)
	if len(r.posts()) != 0 || res.Attempted {
		t.Errorf("result = %+v, %d posts", res, len(r.posts()))
	}
}

func TestDefaultsApplyToZeroTimeouts(t *testing.T) {
	r := newRig(t)
	r.runner.Options.StopTimeout, r.runner.Options.NoticeTimeout = 0, 0
	r.srv.HandleSequence(http.MethodGet, podsPath, kubetest.Response{Body: podList("old-1")})
	res := r.run()
	if res.Wait != WaitTimedOut || r.clock.slept() != 60*time.Second {
		t.Errorf("wait %v after %v, want the 60s default", res.Wait, r.clock.slept())
	}
}

// sequences is a snapshotter that records the sequences a reporter uses.
type sequences struct{ last int64 }

func (s *sequences) Snapshot(_ context.Context, sequence int64) (*inventory.Snapshot, error) {
	s.last = sequence
	return &inventory.Snapshot{Schema: inventory.SchemaV1, Sequence: sequence}, nil
}

type okPoster struct{}

func (okPoster) Send(context.Context, string, []byte) sender.Result {
	return sender.Result{Outcome: sender.Accepted, Status: 202, Attempts: 1}
}

func newReporter(t *testing.T, clock *fakeClock, snaps loop.Snapshotter, interval time.Duration) *loop.Reporter {
	t.Helper()
	keyFile := filepath.Join(t.TempDir(), "ingest-key")
	if err := os.WriteFile(keyFile, []byte(testKey), 0o600); err != nil {
		t.Fatal(err)
	}
	return loop.New(loop.Reporter{Snapshots: snaps, Sender: okPoster{}, KeyFile: keyFile, Interval: interval, Now: clock.Now})
}
