// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	copyTimeout       = 10 * time.Second
	copyFirstBackoff  = time.Second
	copyMaxBackoff    = 30 * time.Second
	copyMaxRetryAfter = 5 * time.Minute
)

// TracesPath is the OTLP/HTTP route a copy is posted to.
const TracesPath = "/v1/traces"

// Copier posts the queued batches to impact.ai, oldest first, one at a time. While impact.ai does not take them they
// stay in the queue, which drops the oldest when it is full; the forwarding never waits for the copier.
type Copier struct {
	// Endpoint is the base URL of an OTLP/HTTP receiver of impact.ai: the kit's own gateway in the cluster, or ingest.
	Endpoint string
	// KeyFile holds the install key, sent as bearer and read again at every attempt. Empty sends no key (the kit's
	// gateway adds its own).
	KeyFile string
	// Client posts the copies. Nil means a client with a 10 second timeout that follows no redirects and uses no proxy.
	Client    *http.Client
	Queue     *Queue
	Stats     *Stats
	UserAgent string
	// Sleep waits between attempts; nil means the real clock.
	Sleep func(ctx context.Context, d time.Duration) error
	Log   *slog.Logger

	failing bool
}

func (c *Copier) log() *slog.Logger {
	if c.Log != nil {
		return c.Log
	}
	return slog.New(slog.DiscardHandler)
}

func (c *Copier) client() *http.Client {
	if c.Client != nil {
		return c.Client
	}
	return &http.Client{
		Timeout: copyTimeout,
		Transport: &http.Transport{
			DialContext:         (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			ForceAttemptHTTP2:   true,
			MaxIdleConnsPerHost: 4,
			IdleConnTimeout:     90 * time.Second,
			TLSHandshakeTimeout: 5 * time.Second,
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (c *Copier) sleep(ctx context.Context, d time.Duration) error {
	if c.Sleep != nil {
		return c.Sleep(ctx, d)
	}
	return sleepFor(ctx, d)
}

func sleepFor(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Run copies until the context ends.
func (c *Copier) Run(ctx context.Context) error {
	client := c.client()
	for {
		it, err := c.Queue.Peek(ctx)
		if err != nil {
			return err
		}
		counts := c.Stats.route(it.Route)
		p, err := decode(it)
		switch {
		case err != nil:
			counts.partial.Add(1)
			c.log().Debug("a forwarded batch cannot be copied", "route", it.Route, "reason", err.Error())
			c.Queue.Done(it)
			continue
		case p == nil:
			c.Queue.Done(it)
			continue
		}
		if err := c.deliver(ctx, client, it, p, counts); err != nil {
			return err
		}
	}
}

// deliver posts one copy until impact.ai accepts or refuses it, or the queue pushed it out.
func (c *Copier) deliver(ctx context.Context, client *http.Client, it Item, p *payload, counts *counters) error {
	for attempt := 1; ; attempt++ {
		if !c.Queue.Has(it) {
			return nil // pushed out while impact.ai did not take it; counted as dropped
		}
		status, wait, reason := c.post(ctx, client, p)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		switch {
		case status >= 200 && status <= 299:
			counts.copied.Add(1)
			c.Queue.Done(it)
			if c.failing {
				c.failing = false
				c.log().Info("copy resumed", "endpoint", hostOf(c.Endpoint))
			}
			return nil
		case status == http.StatusUnauthorized || status == http.StatusForbidden:
			c.fail("impact.ai refused the install key; copies wait in the queue for a new key", status, reason)
		case status == 0 || status == http.StatusRequestTimeout || status == http.StatusTooManyRequests || status >= 500:
			c.fail("impact.ai does not take copies now; they wait in the queue", status, reason)
		default:
			counts.copyRejected.Add(1)
			c.Queue.Done(it)
			c.log().Warn("impact.ai refused a copy; it is dropped", "route", it.Route, "status", status)
			return nil
		}
		d := copyFirstBackoff << min(attempt-1, 5)
		d = max(min(d, copyMaxBackoff), wait)
		if err := c.sleep(ctx, d); err != nil {
			return err
		}
	}
}

func (c *Copier) fail(msg string, status int, reason string) {
	if c.failing {
		return
	}
	c.failing = true
	attrs := []any{"endpoint", hostOf(c.Endpoint), "status", status}
	if reason != "" {
		attrs = append(attrs, "error", reason)
	}
	c.log().Warn(msg, attrs...)
}

// post sends one copy. It returns the status (0 without an answer), the Retry-After wait and why there was no answer.
func (c *Copier) post(ctx context.Context, client *http.Client, p *payload) (int, time.Duration, string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.Endpoint, "/")+TracesPath, bytes.NewReader(p.body))
	if err != nil {
		return 0, 0, "the copy endpoint is not a URL"
	}
	req.Header.Set("Content-Type", p.contentType)
	if p.encoding != "" {
		req.Header.Set("Content-Encoding", p.encoding)
	}
	if c.UserAgent != "" {
		req.Header.Set("User-Agent", c.UserAgent)
	}
	if c.KeyFile != "" {
		key, err := os.ReadFile(c.KeyFile)
		if err != nil || len(bytes.TrimSpace(key)) == 0 {
			return 0, 0, "the install key file cannot be read"
		}
		req.Header.Set("Authorization", "Bearer "+string(bytes.TrimSpace(key)))
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, 0, redact(err)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	_ = resp.Body.Close()
	return resp.StatusCode, retryAfter(resp.Header.Get("Retry-After")), ""
}

func retryAfter(v string) time.Duration {
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil || n <= 0 {
		return 0
	}
	return min(time.Duration(n)*time.Second, copyMaxRetryAfter)
}

func hostOf(endpoint string) string {
	if u, err := parseTarget(endpoint); err == nil {
		return u.Host
	}
	return ""
}
