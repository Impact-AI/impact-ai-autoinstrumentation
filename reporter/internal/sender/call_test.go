// SPDX-License-Identifier: Apache-2.0

package sender

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// answer is one scripted reply of the server behind PostJSON.
type answer struct {
	status     int
	retryAfter string
	body       string
	drop       bool // close the connection without answering
}

type exchangeServer struct {
	*httptest.Server
	mu      sync.Mutex
	answers []answer
	got     []received
}

func newExchangeServer(t *testing.T, answers ...answer) *exchangeServer {
	t.Helper()
	s := &exchangeServer{answers: answers}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.got = append(s.got, received{r.Method, r.URL.Path, r.Header.Clone(), body})
		a := answer{status: http.StatusOK, body: `{}`}
		if len(s.answers) > 0 {
			a, s.answers = s.answers[0], s.answers[1:]
		}
		s.mu.Unlock()
		if a.drop {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		if a.retryAfter != "" {
			w.Header().Set("Retry-After", a.retryAfter)
		}
		w.WriteHeader(a.status)
		_, _ = io.WriteString(w, a.body)
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *exchangeServer) requests() []received {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]received(nil), s.got...)
}

func newCallHarness(t *testing.T, endpoint string) *harness {
	t.Helper()
	h := newHarness(t, endpoint)
	h.Interval = 5 * time.Minute
	return h
}

func (h *harness) call(t *testing.T, path, token string, body []byte) Reply {
	t.Helper()
	return h.PostJSON(context.Background(), path, token, body)
}

func TestPostJSONSendsPlainJSONWithTheBearerAndReturnsTheAnswer(t *testing.T) {
	for _, suffix := range []string{"", "/", "/base", "/base/"} {
		srv := newExchangeServer(t, answer{status: 200, body: `{"keyId":"pk_1"}`})
		h := newCallHarness(t, srv.URL+suffix)
		body := []byte(`{"schema":"impact.install.v1"}`)
		reply := h.call(t, "/v1/installs/exchange", "tok-abc", body)
		if reply.Outcome != Accepted || reply.Attempts != 1 || reply.Status != 200 || string(reply.Body) != `{"keyId":"pk_1"}` {
			t.Fatalf("%q: reply = %+v (body %q)", suffix, reply.Result, reply.Body)
		}
		r := srv.requests()[0]
		wantPath := "/v1/installs/exchange"
		if strings.HasPrefix(suffix, "/base") {
			wantPath = "/base" + wantPath
		}
		if r.method != http.MethodPost || r.path != wantPath {
			t.Errorf("%q: %s %s, want POST %s", suffix, r.method, r.path, wantPath)
		}
		if got := r.header.Get("Authorization"); got != "Bearer tok-abc" {
			t.Errorf("Authorization = %q", got)
		}
		if r.header.Get("Content-Type") != "application/json" || r.header.Get("Accept") != "application/json" {
			t.Errorf("content headers = %q / %q", r.header.Get("Content-Type"), r.header.Get("Accept"))
		}
		if r.header.Get("Content-Encoding") != "" {
			t.Errorf("Content-Encoding = %q, want none: the exchange is plain JSON", r.header.Get("Content-Encoding"))
		}
		if !bytes.Equal(r.body, body) {
			t.Errorf("body changed on the way: %q", r.body)
		}
	}
}

func TestPostJSONDefinitiveRefusalsAreNotRetriedAndKeepTheirBody(t *testing.T) {
	cases := []struct {
		status  int
		outcome Outcome
	}{
		{400, Rejected}, {401, KeyRejected}, {403, KeyRejected}, {404, Rejected}, {409, Rejected}, {410, Rejected},
	}
	for _, tc := range cases {
		srv := newExchangeServer(t, answer{status: tc.status, body: `{"code":"token_used"}`}, answer{status: 200})
		h := newCallHarness(t, srv.URL)
		reply := h.call(t, "/x", "tok", []byte(`{}`))
		if reply.Outcome != tc.outcome || reply.Attempts != 1 || reply.Status != tc.status || reply.Err == nil {
			t.Errorf("status %d: reply = %+v", tc.status, reply.Result)
		}
		if string(reply.Body) != `{"code":"token_used"}` {
			t.Errorf("status %d: body = %q, want the refusal's own", tc.status, reply.Body)
		}
		if len(h.sleeps) != 0 || len(srv.requests()) != 1 {
			t.Errorf("status %d: retried (sleeps %v, requests %d)", tc.status, h.sleeps, len(srv.requests()))
		}
	}
}

func TestPostJSONRetriesTheSameBodyAndHonoursRetryAfter(t *testing.T) {
	srv := newExchangeServer(t,
		answer{status: 503, retryAfter: "5", body: `{"code":"unavailable"}`},
		answer{status: 429, retryAfter: "7", body: `{"code":"rate_limited"}`},
		answer{status: 500},
		answer{status: 200, body: `{"replayed":true}`},
	)
	h := newCallHarness(t, srv.URL)
	reply := h.call(t, "/x", "tok", []byte("the-same-body"))
	if reply.Outcome != Accepted || reply.Attempts != 4 || string(reply.Body) != `{"replayed":true}` {
		t.Fatalf("reply = %+v, body %q", reply.Result, reply.Body)
	}
	for i, r := range srv.requests() {
		if string(r.body) != "the-same-body" {
			t.Errorf("attempt %d sent a different body %q", i+1, r.body)
		}
	}
	// 5 s and 7 s from the server; the third failure has none, so the backoff for a third failure (4 s).
	if !equalDurations(h.sleeps, secs(5, 7, 4)) {
		t.Errorf("sleeps = %v, want 5s, 7s, 4s", h.sleeps)
	}
}

func TestPostJSONGivesUpAfterTheAttemptsAndKeepsTheLastAnswer(t *testing.T) {
	var answers []answer
	for range 6 {
		answers = append(answers, answer{status: 503, body: `{"code":"unavailable"}`})
	}
	srv := newExchangeServer(t, answers...)
	h := newCallHarness(t, srv.URL)
	reply := h.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != GaveUp || reply.Attempts != 5 || reply.Status != 503 || string(reply.Body) != `{"code":"unavailable"}` {
		t.Fatalf("reply = %+v, body %q", reply.Result, reply.Body)
	}
}

func TestPostJSONRetryAfterBeyondTheBudgetEndsTheCallAtOnce(t *testing.T) {
	srv := newExchangeServer(t, answer{status: 429, retryAfter: "600", body: `{"code":"rate_limited"}`})
	h := newCallHarness(t, srv.URL)
	h.Interval = 2 * time.Minute // the budget of the whole call
	reply := h.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != GaveUp || reply.Attempts != 1 || reply.Status != 429 {
		t.Fatalf("reply = %+v", reply.Result)
	}
	if want := h.Now().Add(10 * time.Minute); !reply.NotBefore.Equal(want) {
		t.Errorf("NotBefore = %v, want %v", reply.NotBefore, want)
	}
	if len(h.sleeps) != 0 {
		t.Errorf("slept %v; a wait beyond the budget is not waited for", h.sleeps)
	}
}

func TestPostJSONRetriesNetworkErrors(t *testing.T) {
	srv := newExchangeServer(t, answer{drop: true}, answer{status: 200, body: `{"ok":true}`})
	h := newCallHarness(t, srv.URL)
	reply := h.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != Accepted || reply.Attempts != 2 || string(reply.Body) != `{"ok":true}` {
		t.Errorf("reply = %+v", reply.Result)
	}

	closed := httptest.NewServer(http.NotFoundHandler())
	url := closed.URL
	closed.Close()
	h2 := newCallHarness(t, url)
	reply = h2.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != GaveUp || reply.Attempts != 5 || reply.Status != 0 || reply.Err == nil || len(reply.Body) != 0 {
		t.Errorf("unreachable endpoint: reply = %+v", reply.Result)
	}
}

func TestPostJSONDoesNotFollowRedirectsWithItsCredential(t *testing.T) {
	other := newExchangeServer(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+"/elsewhere", http.StatusTemporaryRedirect)
	}))
	t.Cleanup(srv.Close)
	h := newCallHarness(t, srv.URL)
	reply := h.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != Rejected || reply.Status != http.StatusTemporaryRedirect {
		t.Errorf("reply = %+v", reply.Result)
	}
	if n := len(other.requests()); n != 0 {
		t.Errorf("the redirect target received %d requests carrying the credential", n)
	}
}

func TestPostJSONStopsWhenTheContextEnds(t *testing.T) {
	srv := newExchangeServer(t, answer{status: 503}, answer{status: 200})
	h := newCallHarness(t, srv.URL)
	ctx, cancel := context.WithCancel(context.Background())
	h.Sleep = func(ctx context.Context, d time.Duration) error {
		cancel()
		return ctx.Err()
	}
	reply := h.PostJSON(ctx, "/x", "tok", []byte("x"))
	if reply.Outcome != Canceled {
		t.Errorf("reply = %+v, want Canceled", reply.Result)
	}
	if n := len(srv.requests()); n != 1 {
		t.Errorf("%d requests, want 1", n)
	}
}

func TestPostJSONNeverLogsTheCredentialOrTheBodies(t *testing.T) {
	const token = "impactinstall_e2e_do_not_log_this_token_at_all_0123456789"
	for _, answers := range [][]answer{
		{{status: 200, body: `{"keyId":"pk_secretish"}`}},
		{{status: 503}, {status: 200}},
		{{status: 401, body: `{"code":"token_unknown"}`}},
		{{status: 409, body: `{"code":"token_used"}`}},
		{{drop: true}, {status: 200}},
	} {
		srv := newExchangeServer(t, answers...)
		h := newCallHarness(t, srv.URL)
		h.Log = slog.New(slog.NewJSONHandler(h.logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
		h.call(t, "/x", token, []byte(`{"lookupHash":"do-not-log-the-request-either"}`))
		out := h.logs.String()
		for _, secret := range []string{token, "do-not-log-the-request-either", "pk_secretish"} {
			if strings.Contains(out, secret) {
				t.Errorf("%q was logged: %s", secret, out)
			}
		}
	}
}

func TestPostJSONCapsTheAnswerItKeeps(t *testing.T) {
	srv := newExchangeServer(t, answer{status: 200, body: strings.Repeat("x", 200<<10)})
	h := newCallHarness(t, srv.URL)
	reply := h.call(t, "/x", "tok", []byte("x"))
	if reply.Outcome != Accepted || len(reply.Body) != 64<<10 {
		t.Errorf("kept %d bytes, want exactly the 64 KiB cap", len(reply.Body))
	}
}
