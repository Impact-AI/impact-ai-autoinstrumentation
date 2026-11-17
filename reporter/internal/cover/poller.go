// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	defaultInterval = time.Minute
	firstBackoff    = 2 * time.Second
	maxBackoff      = 5 * time.Minute
	maxRetryAfter   = time.Hour
	intervalSpread  = 0.1 // the cadence varies by 10% either way, so many clusters do not poll in step
	backoffSpread   = 0.2
	stateFile       = "list.json"
)

// Poller keeps the Store in step with the list impact.ai publishes: it asks for it with the install key and the
// ETag of the list it has, applies a new one, and keeps a copy on disk for a restarted container. It never makes the
// cover pod depend on impact.ai: with no answer it keeps the last list, and it stands down only when the key is refused.
type Poller struct {
	// Endpoint is the ingest origin; the list is GET <Endpoint>/v1/inventory/cover.
	Endpoint string
	// KeyFile holds the install key as bearer. It is read again at every poll, so a new key is picked up.
	KeyFile string
	// StateDir keeps a copy of the last list (an emptyDir). Empty keeps nothing.
	StateDir string
	// Interval is the time between polls when all is well; zero means a minute.
	Interval  time.Duration
	UserAgent string
	// Client sends the requests. Nil means a client with a 10 second timeout that follows no redirects, so the key
	// is never sent anywhere else.
	Client *http.Client
	Store  *Store

	// Test seams. Nil means the real clock, sleep and randomness.
	Now    func() time.Time
	Sleep  func(ctx context.Context, d time.Duration) error
	Jitter func() float64
	Log    *slog.Logger

	etag     string
	failures int
	logged   Info // what was last announced
	isLogged bool
}

func (p *Poller) now() time.Time {
	if p.Now != nil {
		return p.Now()
	}
	return time.Now()
}

func (p *Poller) sleep(ctx context.Context, d time.Duration) error {
	if p.Sleep != nil {
		return p.Sleep(ctx, d)
	}
	return sleep(ctx, d)
}

func (p *Poller) jitter() float64 {
	if p.Jitter != nil {
		return p.Jitter()
	}
	return rand.Float64()
}

func (p *Poller) log() *slog.Logger {
	if p.Log != nil {
		return p.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (p *Poller) client() *http.Client {
	if p.Client != nil {
		return p.Client
	}
	return &http.Client{
		Timeout:       10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (p *Poller) url() string { return strings.TrimRight(p.Endpoint, "/") + Path }

func (p *Poller) statePath() string { return filepath.Join(p.StateDir, stateFile) }

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

// parseRetryAfter reads a Retry-After header: whole seconds or an HTTP date. A missing, invalid, zero or past value
// gives 0. The result is capped at one hour.
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

func readKey(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// regular is the wait after a poll that went well.
func (p *Poller) regular() time.Duration {
	interval := p.Interval
	if interval <= 0 {
		interval = defaultInterval
	}
	return time.Duration(float64(interval) * (1 - intervalSpread + 2*intervalSpread*p.jitter()))
}

// failed counts a failure and returns the wait before the next poll: the server's Retry-After when it gave one,
// else 2s, 4s, 8s ... up to 5 minutes, with up to 20% jitter either way.
func (p *Poller) failed(retryAfter time.Duration) time.Duration {
	p.failures++
	if retryAfter > 0 {
		return retryAfter
	}
	d := firstBackoff
	for i := 1; i < p.failures && d < maxBackoff; i++ {
		d *= 2
	}
	d = min(d, maxBackoff)
	return time.Duration(float64(d) * (1 - backoffSpread + 2*backoffSpread*p.jitter()))
}

// announce says at Info what list is in use, once for each version.
func (p *Poller) announce(list List) {
	info := Info{Loaded: true, Version: list.Version, Automatic: list.Automatic}
	if p.isLogged && p.logged.Version == info.Version && p.logged.Automatic == info.Automatic {
		return
	}
	p.logged, p.isLogged = info, true
	args := []any{"version", list.Version, "automatic", list.Automatic, "workloads", p.Store.Info().Workloads}
	if list.Skipped > 0 {
		// Languages only, never the workloads: this kit does not inject them, a newer one may.
		args = append(args, "skipped", list.Skipped, "skippedLanguages", list.SkippedLanguages)
	}
	p.log().Info("cover list in use", args...)
}

// standDown forgets the list: the key was refused, so nothing is injected until a list can be fetched again.
func (p *Poller) standDown(status int) {
	if p.Store.Info().Loaded {
		p.log().Warn("ingest refused the install key; no workload is covered until it accepts one", "status", status)
	}
	p.Store.Clear()
	p.etag, p.isLogged = "", false
	if p.StateDir != "" {
		if err := os.Remove(p.statePath()); err != nil && !errors.Is(err, os.ErrNotExist) {
			p.log().Warn("could not remove the saved cover list", "error", err)
		}
	}
}

type saved struct {
	ETag string          `json:"etag"`
	List json.RawMessage `json:"list"`
}

// save keeps the list as it came, atomically, readable by this user only. A failure is logged and changes nothing.
func (p *Poller) save(body []byte) {
	if p.StateDir == "" {
		return
	}
	data, err := json.Marshal(saved{ETag: p.etag, List: body})
	if err == nil {
		err = os.MkdirAll(p.StateDir, 0o700)
	}
	var tmp *os.File
	if err == nil {
		tmp, err = os.CreateTemp(p.StateDir, "list-*.tmp") // 0600
	}
	if err == nil {
		_, err = tmp.Write(data)
		if cerr := tmp.Close(); err == nil {
			err = cerr
		}
		if err == nil {
			err = os.Rename(tmp.Name(), p.statePath())
		}
		if err != nil {
			_ = os.Remove(tmp.Name())
		}
	}
	if err != nil {
		p.log().Warn("could not save the cover list; it is used all the same", "error", err)
	}
}

// Load reads the copy a previous container saved, if there is a valid one. Run does it first.
func (p *Poller) Load() {
	if p.StateDir == "" {
		return
	}
	data, err := os.ReadFile(p.statePath())
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			p.log().Warn("could not read the saved cover list", "error", err)
		}
		return
	}
	var s saved
	if len(data) > MaxListBytes+4096 || json.Unmarshal(data, &s) != nil {
		p.log().Warn("the saved cover list is not valid; it is ignored")
		return
	}
	list, err := ParseList(s.List)
	if err != nil {
		p.log().Warn("the saved cover list is not valid; it is ignored", "error", err)
		return
	}
	p.Store.Set(list)
	p.etag = s.ETag
	p.announce(list)
}

// Poll fetches the list once and returns how long to wait before the next poll.
func (p *Poller) Poll(ctx context.Context) time.Duration {
	log := p.log()
	key, err := readKey(p.KeyFile)
	if err != nil || key == "" {
		log.Error("no install key; the cover list cannot be fetched", "file", p.KeyFile, "readError", err != nil)
		return p.failed(0)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.url(), nil)
	if err != nil {
		log.Error("cannot build the cover list request", "error", err)
		return p.failed(0)
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Accept", "application/json")
	ua := p.UserAgent
	if ua == "" {
		ua = "impact-cover"
	}
	req.Header.Set("User-Agent", ua)
	if p.etag != "" {
		req.Header.Set("If-None-Match", p.etag)
	}
	resp, err := p.client().Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return p.regular() // shutting down
		}
		// The error text names the URL and the failure, never the headers.
		log.Warn("could not fetch the cover list; keeping the last one", "error", err)
		return p.failed(0)
	}
	defer resp.Body.Close()
	retryAfter := parseRetryAfter(resp.Header.Get("Retry-After"), p.now())

	switch status := resp.StatusCode; {
	case status == http.StatusOK:
		body, err := io.ReadAll(io.LimitReader(resp.Body, MaxListBytes+1))
		if err != nil {
			log.Warn("could not read the cover list; keeping the last one", "error", err)
			return p.failed(retryAfter)
		}
		list, err := ParseList(body)
		if err != nil {
			log.Warn("the cover list is not valid; keeping the last one", "error", err)
			return p.failed(retryAfter)
		}
		p.Store.Set(list)
		p.etag = resp.Header.Get("ETag")
		p.save(body)
		p.failures = 0
		p.announce(list)
		return p.regular()
	case status == http.StatusNotModified:
		p.failures = 0
		return p.regular()
	case status == http.StatusUnauthorized || status == http.StatusForbidden || status == http.StatusNotFound || status == http.StatusGone:
		p.standDown(status)
		return p.failed(retryAfter)
	default:
		log.Warn("impact.ai did not serve the cover list; keeping the last one", "status", status)
		return p.failed(retryAfter)
	}
}

// safePoll turns a panic into a log line, so one bad answer cannot stop the cover pod.
func (p *Poller) safePoll(ctx context.Context) (wait time.Duration) {
	defer func() {
		if r := recover(); r != nil {
			p.log().Error("a poll panicked; the next one will try again", "panic", fmt.Sprint(r))
			wait = p.failed(0)
		}
	}()
	return p.Poll(ctx)
}

// Run loads the saved list, then polls at once and every interval until the context ends.
func (p *Poller) Run(ctx context.Context) error {
	p.Load()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		wait := p.safePoll(ctx)
		if err := p.sleep(ctx, wait); err != nil {
			return err
		}
	}
}
