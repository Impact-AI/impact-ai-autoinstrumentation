// SPDX-License-Identifier: Apache-2.0

package sender

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type step struct {
	status     int
	retryAfter string
	drop       bool // close the connection without answering
}

type received struct {
	method string
	path   string
	header http.Header
	body   []byte
}

type script struct {
	*httptest.Server
	mu    sync.Mutex
	steps []step
	got   []received
}

func newScript(t *testing.T, steps ...step) *script {
	t.Helper()
	s := &script{steps: steps}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.got = append(s.got, received{r.Method, r.URL.Path, r.Header.Clone(), body})
		var st step
		if len(s.steps) > 0 {
			st, s.steps = s.steps[0], s.steps[1:]
		} else {
			st = step{status: http.StatusAccepted}
		}
		s.mu.Unlock()
		if st.drop {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		if st.retryAfter != "" {
			w.Header().Set("Retry-After", st.retryAfter)
		}
		w.WriteHeader(st.status)
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *script) requests() []received {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]received(nil), s.got...)
}

type harness struct {
	Sender
	sleeps []time.Duration
	logs   *bytes.Buffer
}

func newHarness(t *testing.T, endpoint string) *harness {
	t.Helper()
	h := &harness{logs: &bytes.Buffer{}}
	now := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	h.Sender = Sender{
		Endpoint: endpoint,
		Interval: 5 * time.Minute,
		Now:      func() time.Time { return now },
		Sleep: func(ctx context.Context, d time.Duration) error {
			h.sleeps = append(h.sleeps, d)
			return ctx.Err()
		},
		Jitter: func() float64 { return 0.5 }, // factor 1.0
		Log:    slog.New(slog.NewJSONHandler(h.logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	}
	return h
}

func (h *harness) send(t *testing.T, key string, body []byte) Result {
	t.Helper()
	return h.Send(context.Background(), key, body)
}

func secs(n ...int) []time.Duration {
	var out []time.Duration
	for _, v := range n {
		out = append(out, time.Duration(v)*time.Second)
	}
	return out
}

func equalDurations(a, b []time.Duration) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestSendPostsGzipJSONWithTheBearerKey(t *testing.T) {
	for _, suffix := range []string{"", "/", "/base", "/base/"} {
		srv := newScript(t)
		h := newHarness(t, srv.URL+suffix)
		body := []byte("\x1f\x8bgzip-bytes")
		res := h.send(t, "key-abc", body)
		if res.Outcome != Accepted || res.Attempts != 1 || res.Status != http.StatusAccepted {
			t.Fatalf("%q: result = %+v", suffix, res)
		}
		reqs := srv.requests()
		if len(reqs) != 1 {
			t.Fatalf("%q: %d requests", suffix, len(reqs))
		}
		r := reqs[0]
		wantPath := "/v1/inventory"
		if strings.HasPrefix(suffix, "/base") {
			wantPath = "/base/v1/inventory"
		}
		if r.method != http.MethodPost || r.path != wantPath {
			t.Errorf("%q: %s %s, want POST %s", suffix, r.method, r.path, wantPath)
		}
		if got := r.header.Get("Authorization"); got != "Bearer key-abc" {
			t.Errorf("Authorization = %q", got)
		}
		if r.header.Get("Content-Type") != "application/json" || r.header.Get("Content-Encoding") != "gzip" {
			t.Errorf("content headers = %q / %q", r.header.Get("Content-Type"), r.header.Get("Content-Encoding"))
		}
		if !bytes.Equal(r.body, body) {
			t.Errorf("body changed on the way")
		}
		if len(h.sleeps) != 0 {
			t.Errorf("slept %v on the happy path", h.sleeps)
		}
	}
}

func TestSendAcceptsAnySuccessStatus(t *testing.T) {
	for _, status := range []int{200, 201, 202, 204} {
		srv := newScript(t, step{status: status})
		if res := newHarness(t, srv.URL).send(t, "k", []byte("x")); res.Outcome != Accepted {
			t.Errorf("status %d: %+v", status, res)
		}
	}
}

func TestRetriesTheSameBodyAfter503AndThenSucceeds(t *testing.T) {
	srv := newScript(t, step{status: 503}, step{status: 503}, step{status: 202})
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("the-same-body"))
	if res.Outcome != Accepted || res.Attempts != 3 {
		t.Fatalf("result = %+v", res)
	}
	reqs := srv.requests()
	if len(reqs) != 3 {
		t.Fatalf("%d requests", len(reqs))
	}
	for i, r := range reqs {
		if string(r.body) != "the-same-body" {
			t.Errorf("attempt %d sent a different body %q", i+1, r.body)
		}
	}
	if !equalDurations(h.sleeps, secs(1, 2)) {
		t.Errorf("sleeps = %v, want 1s then 2s", h.sleeps)
	}
}

func TestRetryAfterInSecondsOverridesTheBackoff(t *testing.T) {
	srv := newScript(t, step{status: 503, retryAfter: "7"}, step{status: 503}, step{status: 202})
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != Accepted {
		t.Fatalf("result = %+v", res)
	}
	// The first wait is the server's 7s; the second failure falls back to the
	// backoff for the second failure (2s).
	if !equalDurations(h.sleeps, secs(7, 2)) {
		t.Errorf("sleeps = %v, want 7s then 2s", h.sleeps)
	}
}

func TestRetryAfterAsHTTPDate(t *testing.T) {
	srv := newScript(t)
	h := newHarness(t, srv.URL)
	when := h.Now().Add(9 * time.Second).UTC().Format(http.TimeFormat)
	srv.mu.Lock()
	srv.steps = []step{{status: 429, retryAfter: when}, {status: 202}}
	srv.mu.Unlock()
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != Accepted || !equalDurations(h.sleeps, secs(9)) {
		t.Errorf("result = %+v, sleeps = %v, want a 9s wait", res, h.sleeps)
	}

	// A date in the past means "now": fall back to the backoff, never a negative wait.
	srv2 := newScript(t, step{status: 503, retryAfter: h.Now().Add(-time.Hour).UTC().Format(http.TimeFormat)}, step{status: 202})
	h2 := newHarness(t, srv2.URL)
	if res := h2.send(t, "k", []byte("x")); res.Outcome != Accepted || !equalDurations(h2.sleeps, secs(1)) {
		t.Errorf("past date: result = %+v, sleeps = %v", res, h2.sleeps)
	}
}

func TestRetryAfterLongerThanTheIntervalDefersTheNextCycle(t *testing.T) {
	srv := newScript(t, step{status: 503, retryAfter: "600"})
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != GaveUp || res.Attempts != 1 {
		t.Fatalf("result = %+v, want to give up after one attempt", res)
	}
	if want := h.Now().Add(10 * time.Minute); !res.NotBefore.Equal(want) {
		t.Errorf("NotBefore = %v, want %v", res.NotBefore, want)
	}
	if len(h.sleeps) != 0 {
		t.Errorf("slept %v; a wait longer than the interval is left to the next cycle", h.sleeps)
	}
	if n := len(srv.requests()); n != 1 {
		t.Errorf("%d requests, want 1", n)
	}
}

func TestRetryAfterIsCappedAtOneHour(t *testing.T) {
	srv := newScript(t, step{status: 503, retryAfter: "999999"})
	h := newHarness(t, srv.URL)
	h.Interval = 2 * time.Hour
	if res := h.send(t, "k", []byte("x")); res.Outcome != Accepted || !equalDurations(h.sleeps, []time.Duration{time.Hour}) {
		t.Errorf("result = %+v, sleeps = %v, want one capped 1h wait", res, h.sleeps)
	}

	srv2 := newScript(t, step{status: 503, retryAfter: "999999"})
	h2 := newHarness(t, srv2.URL)
	res := h2.send(t, "k", []byte("x"))
	if want := h2.Now().Add(time.Hour); res.Outcome != GaveUp || !res.NotBefore.Equal(want) {
		t.Errorf("result = %+v, want NotBefore capped at %v", res, want)
	}
}

func TestStopsAfterFiveAttempts(t *testing.T) {
	srv := newScript(t, step{status: 503}, step{status: 503}, step{status: 503}, step{status: 503}, step{status: 503}, step{status: 202})
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != GaveUp || res.Attempts != 5 || res.Status != 503 || res.Err == nil {
		t.Fatalf("result = %+v", res)
	}
	if !equalDurations(h.sleeps, secs(1, 2, 4, 8)) {
		t.Errorf("sleeps = %v, want 1s 2s 4s 8s and no wait after the last attempt", h.sleeps)
	}
	if !res.NotBefore.IsZero() {
		t.Errorf("NotBefore = %v, want none", res.NotBefore)
	}
}

func TestBackoffDoublesAndCapsAtSixtySeconds(t *testing.T) {
	steps := make([]step, 10)
	for i := range steps {
		steps[i] = step{status: 500}
	}
	srv := newScript(t, steps...)
	h := newHarness(t, srv.URL)
	h.MaxAttempts = 10
	h.send(t, "k", []byte("x"))
	if !equalDurations(h.sleeps, secs(1, 2, 4, 8, 16, 32, 60, 60, 60)) {
		t.Errorf("sleeps = %v", h.sleeps)
	}
}

func TestJitterStaysWithinTwentyPercent(t *testing.T) {
	for _, tc := range []struct {
		jitter float64
		want   time.Duration
	}{{0, 800 * time.Millisecond}, {0.5, time.Second}, {0.999999999, 1200 * time.Millisecond}} {
		srv := newScript(t, step{status: 503}, step{status: 202})
		h := newHarness(t, srv.URL)
		h.Jitter = func() float64 { return tc.jitter }
		h.send(t, "k", []byte("x"))
		if len(h.sleeps) != 1 {
			t.Fatalf("sleeps = %v", h.sleeps)
		}
		if diff := h.sleeps[0] - tc.want; diff < -time.Millisecond || diff > time.Millisecond {
			t.Errorf("jitter %v: wait %v, want about %v", tc.jitter, h.sleeps[0], tc.want)
		}
	}
}

func TestClientErrorsAreNotRetried(t *testing.T) {
	for _, status := range []int{400, 404, 413, 415, 422} {
		srv := newScript(t, step{status: status}, step{status: 202})
		h := newHarness(t, srv.URL)
		res := h.send(t, "k", []byte("x"))
		if res.Outcome != Rejected || res.Attempts != 1 || res.Status != status || res.Err == nil {
			t.Errorf("status %d: result = %+v", status, res)
		}
		if len(h.sleeps) != 0 || len(srv.requests()) != 1 {
			t.Errorf("status %d: retried (sleeps %v, requests %d)", status, h.sleeps, len(srv.requests()))
		}
	}
}

func TestRejectedKeysAreNotRetried(t *testing.T) {
	for _, status := range []int{401, 403} {
		srv := newScript(t, step{status: status}, step{status: 202})
		h := newHarness(t, srv.URL)
		res := h.send(t, "super-secret-key", []byte("x"))
		if res.Outcome != KeyRejected || res.Attempts != 1 || res.Status != status {
			t.Errorf("status %d: result = %+v", status, res)
		}
		if len(h.sleeps) != 0 || len(srv.requests()) != 1 {
			t.Errorf("status %d: retried", status)
		}
		if !strings.Contains(h.logs.String(), "key rejected") {
			t.Errorf("status %d: no 'key rejected' log: %s", status, h.logs)
		}
	}
}

func TestTransientStatusesAreRetried(t *testing.T) {
	for _, status := range []int{408, 429, 500, 502, 503, 504} {
		srv := newScript(t, step{status: status}, step{status: 202})
		h := newHarness(t, srv.URL)
		res := h.send(t, "k", []byte("x"))
		if res.Outcome != Accepted || res.Attempts != 2 {
			t.Errorf("status %d: result = %+v", status, res)
		}
	}
}

func TestNetworkErrorsAreRetried(t *testing.T) {
	srv := newScript(t, step{drop: true}, step{status: 202})
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != Accepted || res.Attempts != 2 {
		t.Errorf("result = %+v", res)
	}

	closed := httptest.NewServer(http.NotFoundHandler())
	url := closed.URL
	closed.Close()
	h2 := newHarness(t, url)
	res = h2.send(t, "k", []byte("x"))
	if res.Outcome != GaveUp || res.Attempts != 5 || res.Status != 0 || res.Err == nil {
		t.Errorf("unreachable endpoint: result = %+v", res)
	}
}

func TestRedirectsAreNotFollowed(t *testing.T) {
	other := newScript(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+"/elsewhere", http.StatusTemporaryRedirect)
	}))
	t.Cleanup(srv.Close)
	h := newHarness(t, srv.URL)
	res := h.send(t, "k", []byte("x"))
	if res.Outcome != Rejected || res.Status != http.StatusTemporaryRedirect {
		t.Errorf("result = %+v", res)
	}
	if n := len(other.requests()); n != 0 {
		t.Errorf("the redirect target received %d requests carrying the key", n)
	}
}

func TestCancellationDuringBackoffStopsSending(t *testing.T) {
	srv := newScript(t, step{status: 503}, step{status: 202})
	h := newHarness(t, srv.URL)
	ctx, cancel := context.WithCancel(context.Background())
	h.Sleep = func(ctx context.Context, d time.Duration) error {
		cancel()
		return ctx.Err()
	}
	res := h.Send(ctx, "k", []byte("x"))
	if res.Outcome != Canceled {
		t.Errorf("result = %+v, want Canceled", res)
	}
	if n := len(srv.requests()); n != 1 {
		t.Errorf("%d requests, want 1", n)
	}
}

func TestTheKeyIsNeverLogged(t *testing.T) {
	const key = "ik_live_do_not_log_this_key"
	for _, steps := range [][]step{{{status: 202}}, {{status: 503}, {status: 202}}, {{status: 401}}, {{status: 400}}, {{drop: true}, {status: 202}}} {
		srv := newScript(t, steps...)
		h := newHarness(t, srv.URL)
		h.send(t, key, []byte("x"))
		if strings.Contains(h.logs.String(), key) {
			t.Errorf("the key was logged: %s", h.logs)
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
		t.Error("sleep ignored the canceled context")
	}
	if err := sleep(context.Background(), time.Millisecond); err != nil {
		t.Errorf("err = %v", err)
	}
}

func TestParseRetryAfter(t *testing.T) {
	now := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		in   string
		want time.Duration
	}{
		{"", 0}, {"0", 0}, {"-5", 0}, {"abc", 0}, {"3", 3 * time.Second}, {" 12 ", 12 * time.Second},
		{strconv.Itoa(7200), time.Hour}, {now.Add(30 * time.Second).Format(http.TimeFormat), 30 * time.Second},
		{now.Add(-time.Minute).Format(http.TimeFormat), 0},
	}
	for _, tc := range cases {
		if got := parseRetryAfter(tc.in, now); got != tc.want {
			t.Errorf("parseRetryAfter(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}
