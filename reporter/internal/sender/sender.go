// SPDX-License-Identifier: Apache-2.0

// Package sender posts an inventory snapshot to ingest and decides what to do
// with the answer: retry with backoff, honour Retry-After, or give up.
package sender

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Path is the ingest route for inventory snapshots.
const Path = "/v1/inventory"

const (
	defaultMaxAttempts = 5
	firstBackoff       = time.Second
	maxBackoff         = time.Minute
	maxRetryAfter      = time.Hour
	jitterSpread       = 0.2
)

// Outcome says how a send ended.
type Outcome int

const (
	// Accepted means ingest answered with a 2xx status.
	Accepted Outcome = iota
	// Rejected means ingest refused the body (400, 413, other 4xx, redirects).
	// Sending the same body again would not help.
	Rejected
	// KeyRejected means 401 or 403: the key is revoked, expired or wrong.
	KeyRejected
	// GaveUp means every attempt failed or ingest asked to wait longer than one
	// interval. Result.NotBefore may hold the earliest time to try again.
	GaveUp
	// Canceled means the context ended.
	Canceled
)

func (o Outcome) String() string {
	return [...]string{"accepted", "rejected", "key-rejected", "gave-up", "canceled"}[o]
}

// Result describes a finished send.
type Result struct {
	Outcome  Outcome
	Status   int // last HTTP status; 0 when the last attempt got no answer
	Attempts int
	// NotBefore is set when ingest asked to wait longer than one interval
	// (Retry-After): the next snapshot should not be sent before it.
	NotBefore time.Time
	Err       error
}

// Sender posts bodies to <Endpoint>/v1/inventory.
type Sender struct {
	// Endpoint is the base URL of ingest, such as https://ingest.impact.ai.
	Endpoint string
	// Client sends the requests. Nil means a client with a 30 second timeout that
	// follows no redirects (so the key is never sent anywhere else).
	Client    *http.Client
	UserAgent string
	// Interval is the time between snapshots. A Retry-After longer than this is
	// not waited for inside one send; it moves the next send instead.
	Interval time.Duration
	// MaxAttempts per send; zero means 5.
	MaxAttempts int

	// Test seams. Nil means the real clock, sleep and randomness.
	Now    func() time.Time
	Sleep  func(ctx context.Context, d time.Duration) error
	Jitter func() float64

	Log *slog.Logger
}

func (s *Sender) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Sender) sleep(ctx context.Context, d time.Duration) error {
	if s.Sleep != nil {
		return s.Sleep(ctx, d)
	}
	return sleep(ctx, d)
}

func (s *Sender) jitter() float64 {
	if s.Jitter != nil {
		return s.Jitter()
	}
	return rand.Float64()
}

func (s *Sender) log() *slog.Logger {
	if s.Log != nil {
		return s.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (s *Sender) client() *http.Client {
	if s.Client != nil {
		return s.Client
	}
	return &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (s *Sender) urlFor(path string) string {
	return strings.TrimRight(s.Endpoint, "/") + path
}

// sleep waits for d or until the context ends.
func sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// parseRetryAfter reads a Retry-After header: whole seconds or an HTTP date.
// A missing, invalid, zero or past value gives 0. The result is capped at one hour.
func parseRetryAfter(header string, now time.Time) time.Duration {
	header = strings.TrimSpace(header)
	if header == "" {
		return 0
	}
	var d time.Duration
	if n, err := strconv.ParseInt(header, 10, 64); err == nil {
		if n <= 0 {
			return 0
		}
		if n > int64(maxRetryAfter/time.Second) {
			return maxRetryAfter
		}
		d = time.Duration(n) * time.Second
	} else if when, err := http.ParseTime(header); err == nil {
		d = when.Sub(now)
	}
	return min(max(d, 0), maxRetryAfter)
}

// retryable tells whether an HTTP status is worth sending the same body again.
func retryable(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests || status >= 500
}

// backoff is the wait after the n-th failed attempt (n starts at 1): 1s, 2s,
// 4s ... capped at 60s, with up to 20% jitter either way.
func (s *Sender) backoff(n int) time.Duration {
	d := firstBackoff
	for i := 1; i < n && d < maxBackoff; i++ {
		d *= 2
	}
	d = min(d, maxBackoff)
	factor := 1 - jitterSpread + 2*jitterSpread*s.jitter()
	return time.Duration(float64(d) * factor)
}

// maxAnswerBytes bounds how much of an answer is read, and how much of it a caller of PostJSON gets back.
const maxAnswerBytes = 64 << 10

type attempt struct {
	status     int
	retryAfter time.Duration
	err        error
	body       []byte // the answer, at most maxAnswerBytes
}

// request is one POST: where, with which bearer credential, and whether the body is gzip (the inventory) or plain JSON.
type request struct {
	path   string
	bearer string
	body   []byte
	gzip   bool
}

func (s *Sender) post(ctx context.Context, r request) attempt {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.urlFor(r.path), bytes.NewReader(r.body))
	if err != nil {
		return attempt{err: err}
	}
	req.Header.Set("Content-Type", "application/json")
	if r.gzip {
		req.Header.Set("Content-Encoding", "gzip")
	} else {
		req.Header.Set("Accept", "application/json")
	}
	req.Header.Set("Authorization", "Bearer "+r.bearer)
	ua := s.UserAgent
	if ua == "" {
		ua = "impact-reporter"
	}
	req.Header.Set("User-Agent", ua)
	resp, err := s.client().Do(req)
	if err != nil {
		// The error text names the URL and the failure, never the headers.
		return attempt{err: err}
	}
	defer resp.Body.Close()
	answer, _ := io.ReadAll(io.LimitReader(resp.Body, maxAnswerBytes))
	return attempt{status: resp.StatusCode, retryAfter: parseRetryAfter(resp.Header.Get("Retry-After"), s.now()), body: answer}
}

// words are the log lines of one kind of send.
type words struct {
	credentialRefused string // 401 or 403
	rejected          string // any other answer that sending again would not change
	retry             string
	giveUp            string
}

var inventoryWords = words{
	credentialRefused: "key rejected: ingest refused the ingest key",
	rejected:          "ingest rejected the inventory and it will not be sent again",
	retry:             "sending the inventory failed, will retry",
	giveUp:            "giving up on this snapshot",
}

var callWords = words{
	credentialRefused: "ingest refused the credential and it will not be sent again",
	rejected:          "ingest refused the request and it will not be sent again",
	retry:             "the request failed, will retry",
	giveUp:            "giving up on this request",
}

// Send posts body. The same body is retried on transient failures: network
// errors, 408, 429 and 5xx. Retry-After replaces the backoff; one longer than
// the interval defers the next send instead of blocking this one.
func (s *Sender) Send(ctx context.Context, key string, body []byte) Result {
	req := request{path: Path, bearer: key, body: body, gzip: true}
	res, _ := s.run(ctx, inventoryWords, func() attempt { return s.post(ctx, req) })
	return res
}

// Reply is the end of a PostJSON call: how it ended, and the last answer ingest gave.
type Reply struct {
	Result
	// Body is the last answer, at most 64 KiB; empty when no attempt got one.
	Body []byte
}

// PostJSON posts a JSON document to <Endpoint><path> with a bearer credential and retries exactly as Send does: the
// same body again after a network error, 408, 429 or 5xx, Retry-After in place of the backoff, and a Retry-After longer
// than Interval ends the call (Interval is the budget of the whole call here). A 401 or 403 is KeyRejected and any
// other 4xx Rejected: the answer's body is in Reply.Body either way, so the caller can read ingest's reason. Nothing
// of the body or the credential is logged.
func (s *Sender) PostJSON(ctx context.Context, path, bearer string, body []byte) Reply {
	req := request{path: path, bearer: bearer, body: body}
	var last []byte
	res, _ := s.run(ctx, callWords, func() attempt {
		a := s.post(ctx, req)
		last = a.body
		return a
	})
	return Reply{Result: res, Body: last}
}

// run is the retry policy shared by Send and PostJSON.
func (s *Sender) run(ctx context.Context, w words, do func() attempt) (Result, []byte) {
	maxAttempts := s.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = defaultMaxAttempts
	}
	log := s.log()
	var res Result
	for n := 1; ; n++ {
		a := do()
		res.Attempts, res.Status, res.Err = n, a.status, a.err
		switch {
		case ctx.Err() != nil:
			res.Outcome, res.Err = Canceled, ctx.Err()
			return res, a.body
		case a.err == nil && a.status >= 200 && a.status < 300:
			res.Outcome, res.Err = Accepted, nil
			return res, a.body
		case a.err == nil && (a.status == http.StatusUnauthorized || a.status == http.StatusForbidden):
			res.Outcome, res.Err = KeyRejected, fmt.Errorf("ingest answered %d", a.status)
			log.Error(w.credentialRefused, "status", a.status)
			return res, a.body
		case a.err == nil && !retryable(a.status):
			res.Outcome, res.Err = Rejected, fmt.Errorf("ingest answered %d", a.status)
			log.Error(w.rejected, "status", a.status)
			return res, a.body
		}
		if a.err == nil {
			res.Err = fmt.Errorf("ingest answered %d", a.status)
		}

		// A transient failure. Decide whether and how long to wait.
		if a.retryAfter > s.Interval && s.Interval > 0 {
			res.Outcome = GaveUp
			res.NotBefore = s.now().Add(a.retryAfter)
			log.Warn("ingest asked to wait longer than one interval", "status", a.status, "retryAfter", a.retryAfter.String())
			return res, a.body
		}
		if n >= maxAttempts {
			res.Outcome = GaveUp
			log.Warn(w.giveUp, "status", a.status, "attempts", n)
			return res, a.body
		}
		wait := a.retryAfter
		if wait <= 0 {
			wait = s.backoff(n)
		}
		log.Warn(w.retry, "status", a.status, "attempt", n, "wait", wait.String())
		if err := s.sleep(ctx, wait); err != nil {
			res.Outcome, res.Err = Canceled, err
			return res, a.body
		}
	}
}
