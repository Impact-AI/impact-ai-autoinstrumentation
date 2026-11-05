// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const testKey = "impact_test_abcdefghijklmnopqrstuvwxyz0123456789ABC"

type step struct {
	status     int
	body       string
	etag       string
	retryAfter string
	drop       bool // close the connection without answering
}

type received struct {
	method, path string
	header       http.Header
}

// listServer plays the ingest route that serves the list. The last step repeats.
type listServer struct {
	*httptest.Server
	mu    sync.Mutex
	steps []step
	got   []received
}

func newListServer(t *testing.T, steps ...step) *listServer {
	t.Helper()
	s := &listServer{steps: steps}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.got = append(s.got, received{r.Method, r.URL.Path, r.Header.Clone()})
		var st step
		if len(s.steps) > 1 {
			st, s.steps = s.steps[0], s.steps[1:]
		} else if len(s.steps) == 1 {
			st = s.steps[0]
		} else {
			st = step{status: http.StatusNotModified}
		}
		s.mu.Unlock()
		if st.drop {
			if conn, _, err := w.(http.Hijacker).Hijack(); err == nil {
				conn.Close()
			}
			return
		}
		if st.etag != "" {
			w.Header().Set("ETag", st.etag)
		}
		if st.retryAfter != "" {
			w.Header().Set("Retry-After", st.retryAfter)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(st.status)
		_, _ = w.Write([]byte(st.body))
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *listServer) requests() []received {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]received(nil), s.got...)
}

func (s *listServer) then(steps ...step) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.steps = steps
}

func listBody(version int, automatic bool, entries ...Entry) string {
	b, _ := json.Marshal(List{Schema: SchemaV1, Version: int64(version), Automatic: automatic, Workloads: entries})
	return string(b)
}

func ok(version int, etag string, entries ...Entry) step {
	return step{status: http.StatusOK, body: listBody(version, true, entries...), etag: etag}
}

type harness struct {
	*Poller
	t       *testing.T
	sleeps  []time.Duration
	logs    *bytes.Buffer
	dir     string
	keyFile string
	now     time.Time
}

func newHarness(t *testing.T, endpoint string) *harness {
	t.Helper()
	dir := t.TempDir()
	h := &harness{t: t, logs: &bytes.Buffer{}, dir: dir, keyFile: filepath.Join(dir, "ingest-key"), now: fixedNow}
	if err := os.WriteFile(h.keyFile, []byte(" "+testKey+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	h.Poller = &Poller{
		Endpoint:  endpoint,
		KeyFile:   h.keyFile,
		StateDir:  filepath.Join(dir, "state"),
		Interval:  time.Minute,
		UserAgent: "impact-cover/9.9.9",
		Store:     &Store{},
		Now:       func() time.Time { return h.now },
		Sleep: func(ctx context.Context, d time.Duration) error {
			h.sleeps = append(h.sleeps, d)
			return ctx.Err()
		},
		Jitter: func() float64 { return 0.5 }, // no jitter at all
		Log:    slog.New(slog.NewJSONHandler(h.logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	}
	return h
}

func (h *harness) statePath() string { return filepath.Join(h.dir, "state", "list.json") }

func near(got, want time.Duration) bool {
	d := got - want
	return d > -time.Millisecond && d < time.Millisecond
}

func secs(n float64) time.Duration { return time.Duration(n * float64(time.Second)) }

func (h *harness) poll() time.Duration { return h.Poll(context.Background()) }

func (h *harness) wants(delay time.Duration, got time.Duration) {
	h.t.Helper()
	if !near(got, delay) {
		h.t.Errorf("next poll in %v, want %v", got, delay)
	}
}

func TestTheFirstPollSendsTheKeyAndKeepsTheList(t *testing.T) {
	srv := newListServer(t, ok(17, `"v17"`, entry("Deployment", "api", container("app"))))
	h := newHarness(t, srv.URL+"/")
	next := h.poll()
	h.wants(time.Minute, next)

	reqs := srv.requests()
	if len(reqs) != 1 {
		t.Fatalf("%d requests", len(reqs))
	}
	r := reqs[0]
	if r.method != http.MethodGet || r.path != "/v1/inventory/cover" {
		t.Errorf("%s %s", r.method, r.path)
	}
	if got := r.header.Get("Authorization"); got != "Bearer "+testKey {
		t.Errorf("Authorization = %q, want the trimmed key as bearer", got)
	}
	if r.header.Get("If-None-Match") != "" {
		t.Errorf("If-None-Match = %q on the first poll", r.header.Get("If-None-Match"))
	}
	if r.header.Get("Accept") != "application/json" {
		t.Errorf("Accept = %q", r.header.Get("Accept"))
	}
	if r.header.Get("User-Agent") != "impact-cover/9.9.9" {
		t.Errorf("User-Agent = %q", r.header.Get("User-Agent"))
	}
	if e, found := h.Store.Lookup("shop", "Deployment", "api"); !found || e.Container != "app" {
		t.Errorf("the list was not applied: %+v, %v", e, found)
	}
	if info := h.Store.Info(); info.Version != 17 || !info.Automatic {
		t.Errorf("Info = %+v", info)
	}

	// Kept for a restarted container: the answer as it came, with its ETag, readable by this user only.
	stat, err := os.Stat(h.statePath())
	if err != nil {
		t.Fatal(err)
	}
	if stat.Mode().Perm() != 0o600 {
		t.Errorf("mode = %v, want 0600", stat.Mode().Perm())
	}
	var saved struct {
		ETag string          `json:"etag"`
		List json.RawMessage `json:"list"`
	}
	data, _ := os.ReadFile(h.statePath())
	if err := json.Unmarshal(data, &saved); err != nil || saved.ETag != `"v17"` {
		t.Fatalf("saved = %s (%v)", data, err)
	}
	if list, err := ParseList(saved.List); err != nil || list.Version != 17 {
		t.Errorf("the saved list: %+v, %v", list, err)
	}
	leftovers, _ := filepath.Glob(filepath.Join(h.dir, "state", "*"))
	if len(leftovers) != 1 {
		t.Errorf("state dir holds %v, want only list.json", leftovers)
	}
}

func TestTheNextPollIsConditionalAndANotModifiedChangesNothing(t *testing.T) {
	srv := newListServer(t, ok(17, `"v17"`, entry("Deployment", "api")), step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.poll()
	next := h.poll()
	h.wants(time.Minute, next)
	reqs := srv.requests()
	if got := reqs[1].header.Get("If-None-Match"); got != `"v17"` {
		t.Errorf("If-None-Match = %q, want the ETag of the list", got)
	}
	if _, found := h.Store.Lookup("shop", "Deployment", "api"); !found {
		t.Error("a 304 dropped the list")
	}
}

func TestAChangedListReplacesTheOldOneAndItsETagIsSentNext(t *testing.T) {
	srv := newListServer(t, ok(17, `"v17"`, entry("Deployment", "api")), ok(18, `"v18"`, entry("StatefulSet", "db", lang("node"))), step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.poll()
	h.poll()
	h.poll()
	if _, found := h.Store.Lookup("shop", "Deployment", "api"); found {
		t.Error("the old list is still in use")
	}
	if e, found := h.Store.Lookup("shop", "StatefulSet", "db"); !found || e.Lang != "node" {
		t.Errorf("the new list is not in use: %+v, %v", e, found)
	}
	if got := srv.requests()[2].header.Get("If-None-Match"); got != `"v18"` {
		t.Errorf("If-None-Match = %q, want the new ETag", got)
	}
	var saved struct {
		ETag string          `json:"etag"`
		List json.RawMessage `json:"list"`
	}
	data, _ := os.ReadFile(h.statePath())
	if err := json.Unmarshal(data, &saved); err != nil || saved.ETag != `"v18"` || !strings.Contains(string(saved.List), `"version":18`) {
		t.Errorf("the saved copy is not the new list: %s (%v)", data, err)
	}
}

func TestWithoutAUserAgentTheProductIsStillNamed(t *testing.T) {
	srv := newListServer(t, step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.UserAgent = ""
	h.poll()
	if got := srv.requests()[0].header.Get("User-Agent"); got != "impact-cover" {
		t.Errorf("User-Agent = %q", got)
	}
}

func TestAListWithoutAnETagIsFetchedInFullEveryTime(t *testing.T) {
	srv := newListServer(t, ok(1, "", entry("Deployment", "api")))
	h := newHarness(t, srv.URL)
	h.poll()
	h.poll()
	for i, r := range srv.requests() {
		if r.header.Get("If-None-Match") != "" {
			t.Errorf("request %d sent If-None-Match %q", i, r.header.Get("If-None-Match"))
		}
	}
}

func TestAutomaticCoverageOffKeepsTheListButCoversNothing(t *testing.T) {
	srv := newListServer(t, step{status: 200, body: listBody(5, false, entry("Deployment", "api")), etag: `"v5"`})
	h := newHarness(t, srv.URL)
	h.poll()
	if h.Store.Active() {
		t.Error("covering with automatic coverage off")
	}
	if info := h.Store.Info(); !info.Loaded || info.Version != 5 || info.Automatic {
		t.Errorf("Info = %+v", info)
	}
}

func TestTheCadenceHasTenPercentJitter(t *testing.T) {
	for _, tc := range []struct {
		jitter float64
		want   time.Duration
	}{{0, 54 * time.Second}, {0.5, 60 * time.Second}, {0.999999999, 66 * time.Second}} {
		srv := newListServer(t, step{status: http.StatusNotModified})
		h := newHarness(t, srv.URL)
		h.Jitter = func() float64 { return tc.jitter }
		if got := h.poll(); !near(got, tc.want) {
			t.Errorf("jitter %v: next poll in %v, want about %v", tc.jitter, got, tc.want)
		}
	}
}

func TestADefaultIntervalOfAMinute(t *testing.T) {
	srv := newListServer(t, step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.Interval = 0
	h.wants(time.Minute, h.poll())
}

func TestARevokedKeyStopsTheCoverageAtOnce(t *testing.T) {
	for _, status := range []int{401, 403, 404, 410} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			srv := newListServer(t, ok(17, `"v17"`, entry("Deployment", "api")), step{status: status, body: `{"code":"credential_invalid"}`})
			h := newHarness(t, srv.URL)
			h.poll()
			if !h.Store.Active() {
				t.Fatal("no list to begin with")
			}
			next := h.poll()
			h.wants(2*time.Second, next)
			if h.Store.Active() || h.Store.Info().Loaded {
				t.Error("the list is still in use after the key was refused: nothing may be injected without a valid key")
			}
			if _, err := os.Stat(h.statePath()); !errors.Is(err, os.ErrNotExist) {
				t.Errorf("the saved list is still there: %v", err)
			}
			// And the next request starts over: no ETag of a list that is gone.
			h.poll()
			if got := srv.requests()[2].header.Get("If-None-Match"); got != "" {
				t.Errorf("If-None-Match = %q after standing down", got)
			}
		})
	}
}

func TestTransientFailuresKeepTheListAndBackOff(t *testing.T) {
	big := `{"schema":"impact.cover.v1","version":9,"automatic":true,"workloads":[],"padding":"` + strings.Repeat("x", MaxListBytes) + `"}`
	failures := map[string]step{
		"500":              {status: 500},
		"502":              {status: 502},
		"503":              {status: 503},
		"408":              {status: 408},
		"429":              {status: 429},
		"400":              {status: 400},
		"a redirect":       {status: 307},
		"not JSON":         {status: 200, body: "<html>"},
		"another schema":   {status: 200, body: `{"schema":"impact.cover.v2","version":9,"automatic":true,"workloads":[]}`},
		"a bad entry":      {status: 200, body: `{"schema":"impact.cover.v1","version":9,"automatic":true,"workloads":[{"namespace":"X","kind":"Deployment","name":"a","lang":"python"}]}`},
		"too big a body":   {status: 200, body: big},
		"no content":       {status: 204},
		"dropped answer":   {drop: true},
		"an empty success": {status: 200, body: ""},
	}
	for name, failing := range failures {
		t.Run(name, func(t *testing.T) {
			srv := newListServer(t, ok(17, `"v17"`, entry("Deployment", "api")), failing)
			h := newHarness(t, srv.URL)
			h.poll()
			next := h.poll()
			h.wants(2*time.Second, next)
			if e, found := h.Store.Lookup("shop", "Deployment", "api"); !found || e.Name != "api" {
				t.Error("a failure dropped the last good list")
			}
			if info := h.Store.Info(); info.Version != 17 {
				t.Errorf("Info = %+v, want version 17 kept", info)
			}
			if _, err := os.Stat(h.statePath()); err != nil {
				t.Errorf("the saved list was lost: %v", err)
			}
			// The ETag of the good list is still the one that is sent.
			srv.then(step{status: http.StatusNotModified})
			h.poll()
			reqs := srv.requests()
			if got := reqs[len(reqs)-1].header.Get("If-None-Match"); got != `"v17"` {
				t.Errorf("If-None-Match = %q, want the ETag of the list in use", got)
			}
		})
	}
}

func TestBackoffDoublesCapsAndIsResetBySuccess(t *testing.T) {
	srv := newListServer(t, step{status: 503})
	h := newHarness(t, srv.URL)
	var waits []time.Duration
	for i := 0; i < 11; i++ {
		waits = append(waits, h.poll())
	}
	want := []float64{2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300}
	for i, w := range want {
		if !near(waits[i], secs(w)) {
			t.Errorf("wait %d = %v, want %vs", i+1, waits[i], w)
		}
	}
	srv.then(step{status: http.StatusNotModified})
	h.wants(time.Minute, h.poll())
	srv.then(step{status: 503})
	h.wants(2*time.Second, h.poll())
}

func TestBackoffHasTwentyPercentJitter(t *testing.T) {
	for _, tc := range []struct {
		jitter float64
		want   time.Duration
	}{{0, 1600 * time.Millisecond}, {0.5, 2 * time.Second}, {0.999999999, 2400 * time.Millisecond}} {
		srv := newListServer(t, step{status: 503})
		h := newHarness(t, srv.URL)
		h.Jitter = func() float64 { return tc.jitter }
		if got := h.poll(); !near(got, tc.want) {
			t.Errorf("jitter %v: backoff %v, want about %v", tc.jitter, got, tc.want)
		}
	}
}

func TestRetryAfterReplacesTheBackoff(t *testing.T) {
	date := fixedNow.Add(90 * time.Second).UTC().Format(http.TimeFormat)
	past := fixedNow.Add(-time.Hour).UTC().Format(http.TimeFormat)
	for _, tc := range []struct {
		name  string
		after string
		want  time.Duration
	}{
		{"seconds", "7", 7 * time.Second},
		{"a date", date, 90 * time.Second},
		{"capped at an hour", "999999", time.Hour},
		{"zero falls back to the backoff", "0", 2 * time.Second},
		{"nonsense falls back to the backoff", "soon", 2 * time.Second},
		{"a date in the past falls back to the backoff", past, 2 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := newListServer(t, step{status: 503, retryAfter: tc.after})
			h := newHarness(t, srv.URL)
			h.wants(tc.want, h.poll())
		})
	}
	// 429 says the same.
	srv := newListServer(t, step{status: 429, retryAfter: "30"})
	h := newHarness(t, srv.URL)
	h.wants(30*time.Second, h.poll())
}

func TestTheKeyIsReadAgainAtEveryPollAndNeverLogged(t *testing.T) {
	srv := newListServer(t, ok(1, `"v1"`, entry("Deployment", "api")), step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.poll()
	const rotated = "impact_test_ROTATEDROTATEDROTATEDROTATEDROTATEDROTATED12"
	if err := os.WriteFile(h.keyFile, []byte(rotated), 0o600); err != nil {
		t.Fatal(err)
	}
	h.poll()
	reqs := srv.requests()
	if reqs[0].header.Get("Authorization") != "Bearer "+testKey || reqs[1].header.Get("Authorization") != "Bearer "+rotated {
		t.Errorf("Authorization = %q then %q", reqs[0].header.Get("Authorization"), reqs[1].header.Get("Authorization"))
	}
	// Every kind of poll leaves the key out of the log: success, refusal, failure, no key.
	srv.then(step{status: 401})
	h.poll()
	srv.then(step{status: 503})
	h.poll()
	srv.then(step{drop: true})
	h.poll()
	os.Remove(h.keyFile)
	h.poll()
	for _, secret := range []string{testKey, rotated} {
		if strings.Contains(h.logs.String(), secret) {
			t.Errorf("the key was logged:\n%s", h.logs)
		}
	}
}

func TestWithoutAKeyNothingIsSentAndTheListIsKept(t *testing.T) {
	srv := newListServer(t, ok(1, `"v1"`, entry("Deployment", "api")))
	h := newHarness(t, srv.URL)
	h.poll()
	before := len(srv.requests())
	// An empty key file, one of blanks, and no file at all: each is a failure in a row (2s, 4s, 8s), none sends anything.
	for i, content := range []string{"", " \n", "<missing>"} {
		if content == "<missing>" {
			os.Remove(h.keyFile)
		} else if err := os.WriteFile(h.keyFile, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		h.wants(secs(float64(int(2)<<i)), h.poll())
		if len(srv.requests()) != before {
			t.Errorf("a request was sent without a key (key file %q)", content)
		}
		if !h.Store.Active() {
			t.Errorf("the list was dropped because the key file is %q", content)
		}
	}
}

func TestTheSavedListIsLoadedAtStartAndItsETagIsSent(t *testing.T) {
	srv := newListServer(t, step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	if err := os.MkdirAll(filepath.Dir(h.statePath()), 0o700); err != nil {
		t.Fatal(err)
	}
	saved, _ := json.Marshal(map[string]any{"etag": `"v21"`, "list": json.RawMessage(listBody(21, true, entry("DaemonSet", "agent")))})
	if err := os.WriteFile(h.statePath(), saved, 0o600); err != nil {
		t.Fatal(err)
	}
	h.Load()
	if _, found := h.Store.Lookup("shop", "DaemonSet", "agent"); !found {
		t.Fatal("the saved list was not loaded")
	}
	h.poll()
	if got := srv.requests()[0].header.Get("If-None-Match"); got != `"v21"` {
		t.Errorf("If-None-Match = %q, want the saved ETag", got)
	}
}

func TestASavedListThatIsNotValidIsIgnored(t *testing.T) {
	for name, content := range map[string]string{
		"not JSON":       "{",
		"empty":          "",
		"no list":        `{"etag":"x"}`,
		"another schema": `{"etag":"x","list":{"schema":"nope","version":1,"automatic":true,"workloads":[]}}`,
		"a bad entry":    `{"etag":"x","list":{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[{"namespace":"","kind":"Job","name":"a","lang":"python"}]}}`,
	} {
		t.Run(name, func(t *testing.T) {
			srv := newListServer(t, step{status: http.StatusNotModified})
			h := newHarness(t, srv.URL)
			_ = os.MkdirAll(filepath.Dir(h.statePath()), 0o700)
			if err := os.WriteFile(h.statePath(), []byte(content), 0o600); err != nil {
				t.Fatal(err)
			}
			h.Load()
			if h.Store.Info().Loaded {
				t.Error("an invalid saved list was loaded")
			}
			h.poll()
			if got := srv.requests()[0].header.Get("If-None-Match"); got != "" {
				t.Errorf("If-None-Match = %q from an invalid saved list", got)
			}
		})
	}
	// No file at all is the ordinary first start.
	srv := newListServer(t, step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	h.Load()
	if h.Store.Info().Loaded || strings.Contains(h.logs.String(), `"level":"WARN"`) || strings.Contains(h.logs.String(), `"level":"ERROR"`) {
		t.Errorf("a first start is not a problem:\n%s", h.logs)
	}
}

func TestRedirectsAreNotFollowedSoTheKeyGoesNowhereElse(t *testing.T) {
	elsewhere := newListServer(t, ok(1, `"x"`, entry("Deployment", "evil")))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/v1/inventory/cover", http.StatusTemporaryRedirect)
	}))
	t.Cleanup(srv.Close)
	h := newHarness(t, srv.URL)
	h.wants(2*time.Second, h.poll())
	if n := len(elsewhere.requests()); n != 0 {
		t.Errorf("the redirect target received %d requests, carrying the key", n)
	}
	if h.Store.Info().Loaded {
		t.Error("a list was taken from a redirect")
	}
}

func TestTheVersionIsLoggedOnlyWhenItChanges(t *testing.T) {
	srv := newListServer(t,
		ok(1, `"a"`, entry("Deployment", "api")),
		ok(1, `"b"`, entry("Deployment", "api")), // same version, new ETag: nothing new to say
		ok(2, `"c"`, entry("Deployment", "api"), entry("Deployment", "web")),
		step{status: http.StatusNotModified},
	)
	h := newHarness(t, srv.URL)
	for i := 0; i < 4; i++ {
		h.poll()
	}
	lines := 0
	for _, line := range strings.Split(strings.TrimSpace(h.logs.String()), "\n") {
		var rec map[string]any
		if json.Unmarshal([]byte(line), &rec) != nil || rec["level"] != "INFO" {
			continue
		}
		lines++
		if lines == 2 && (rec["version"] != float64(2) || rec["workloads"] != float64(2)) {
			t.Errorf("the second line should name version 2 and 2 workloads: %v", rec)
		}
	}
	if lines != 2 {
		t.Errorf("%d INFO lines, want 2 (version 1, then version 2):\n%s", lines, h.logs)
	}
	for _, leak := range []string{`"api"`, `"web"`, "shop"} {
		if strings.Contains(h.logs.String(), leak) {
			t.Errorf("the list content is in the log (%s):\n%s", leak, h.logs)
		}
	}
}

func TestStateThatCannotBeWrittenDoesNotStopTheCoverage(t *testing.T) {
	srv := newListServer(t, ok(1, `"v1"`, entry("Deployment", "api")))
	h := newHarness(t, srv.URL)
	// The state "directory" is a file.
	h.StateDir = filepath.Join(h.dir, "file")
	if err := os.WriteFile(h.StateDir, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	h.wants(time.Minute, h.poll())
	if !h.Store.Active() {
		t.Error("the list was not applied because it could not be saved")
	}
	// And with no state directory at all, nothing is written.
	h2 := newHarness(t, srv.URL)
	h2.StateDir = ""
	h2.poll()
	if _, err := os.Stat(filepath.Join(h2.dir, "state")); err == nil {
		t.Error("a state directory was created without being asked")
	}
}

func TestRunLoadsPollsAndSleepsUntilCancelled(t *testing.T) {
	srv := newListServer(t, ok(1, `"v1"`, entry("Deployment", "api")), step{status: 503}, step{status: http.StatusNotModified})
	h := newHarness(t, srv.URL)
	ctx, cancel := context.WithCancel(context.Background())
	h.Sleep = func(ctx context.Context, d time.Duration) error {
		h.sleeps = append(h.sleeps, d)
		if len(h.sleeps) == 3 {
			cancel()
		}
		return ctx.Err()
	}
	err := h.Run(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Run = %v, want context.Canceled", err)
	}
	if len(h.sleeps) != 3 || !near(h.sleeps[0], time.Minute) || !near(h.sleeps[1], 2*time.Second) || !near(h.sleeps[2], time.Minute) {
		t.Errorf("sleeps = %v, want 1m, 2s, 1m", h.sleeps)
	}
	if n := len(srv.requests()); n != 3 {
		t.Errorf("%d requests, want 3", n)
	}
}

func TestRunSurvivesAPanicInAPoll(t *testing.T) {
	srv := newListServer(t, ok(1, `"v1"`, entry("Deployment", "api")))
	h := newHarness(t, srv.URL)
	calls := 0
	h.Jitter = func() float64 {
		calls++
		if calls == 1 {
			panic("boom")
		}
		return 0.5
	}
	ctx, cancel := context.WithCancel(context.Background())
	h.Sleep = func(ctx context.Context, d time.Duration) error {
		h.sleeps = append(h.sleeps, d)
		if len(h.sleeps) == 2 {
			cancel()
		}
		return ctx.Err()
	}
	if err := h.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Errorf("Run = %v", err)
	}
	if len(h.sleeps) != 2 {
		t.Errorf("sleeps = %v: the loop stopped after a panic", h.sleeps)
	}
	if !strings.Contains(h.logs.String(), "boom") {
		t.Errorf("the panic was not logged:\n%s", h.logs)
	}
}

func TestACancelledPollIsNotAFailure(t *testing.T) {
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-block }))
	t.Cleanup(func() { close(block); srv.Close() })
	h := newHarness(t, srv.URL)
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(50 * time.Millisecond); cancel() }()
	next := h.Poll(ctx)
	if h.failures != 0 {
		t.Errorf("failures = %d after a cancelled poll", h.failures)
	}
	if !near(next, time.Minute) {
		t.Errorf("next = %v", next)
	}
}

func TestParseRetryAfter(t *testing.T) {
	now := fixedNow
	for _, tc := range []struct {
		in   string
		want time.Duration
	}{
		{"", 0}, {"0", 0}, {"-5", 0}, {"abc", 0}, {"3", 3 * time.Second}, {" 12 ", 12 * time.Second},
		{strconv.Itoa(7200), time.Hour}, {now.Add(30 * time.Second).Format(http.TimeFormat), 30 * time.Second},
		{now.Add(-time.Minute).Format(http.TimeFormat), 0},
	} {
		if got := parseRetryAfter(tc.in, now); got != tc.want {
			t.Errorf("parseRetryAfter(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

func TestRealSleepHonoursCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	if err := sleep(ctx, time.Hour); !errors.Is(err, context.Canceled) {
		t.Errorf("err = %v", err)
	}
	if time.Since(start) > time.Second {
		t.Error("sleep ignored the cancelled context")
	}
	if err := sleep(context.Background(), time.Millisecond); err != nil {
		t.Errorf("err = %v", err)
	}
	_ = fmt.Sprint()
}
