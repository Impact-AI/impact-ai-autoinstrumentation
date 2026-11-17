// SPDX-License-Identifier: Apache-2.0

// Package loop runs the reporter: every interval it builds a snapshot and sends
// it, one cycle at a time.
package loop

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/inventory"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/sender"
)

// DefaultBuildTimeout bounds how long building one snapshot may take.
const DefaultBuildTimeout = 2 * time.Minute

// Snapshotter builds a snapshot with the given sequence number.
type Snapshotter interface {
	Snapshot(ctx context.Context, sequence int64) (*inventory.Snapshot, error)
}

// Poster sends an encoded snapshot to ingest.
type Poster interface {
	Send(ctx context.Context, key string, body []byte) sender.Result
}

// Reporter builds and sends snapshots.
type Reporter struct {
	Snapshots Snapshotter
	Sender    Poster
	// KeyFile holds the ingest key. It is read again in every cycle, so a rotated
	// Secret is picked up without a restart.
	KeyFile  string
	Interval time.Duration
	// BuildTimeout bounds one snapshot; zero means DefaultBuildTimeout.
	BuildTimeout time.Duration
	// OnBuilt is called after each snapshot was built (readiness: it only has
	// to be true once).
	OnBuilt func()

	// Test seams.
	Now   func() time.Time
	Sleep func(ctx context.Context, d time.Duration) error
	Log   *slog.Logger

	sequence int64
}

// New returns a reporter whose sequence numbers start at the current Unix time
// in seconds and rise by one for every snapshot. Seeding from the clock keeps
// the numbers rising across restarts of the pod.
func New(r Reporter) *Reporter {
	r.sequence = r.now().Unix()
	return &r
}

func (r *Reporter) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Reporter) log() *slog.Logger {
	if r.Log != nil {
		return r.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (r *Reporter) sleep(ctx context.Context, d time.Duration) error {
	if r.Sleep != nil {
		return r.Sleep(ctx, d)
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

// CycleResult describes one cycle.
type CycleResult struct {
	// Built is true when a snapshot was built.
	Built    bool
	Sequence int64
	// Sent is the outcome of sending; zero when nothing was sent.
	Sent sender.Result
	// NotBefore is the earliest time the next snapshot may be sent, when ingest
	// asked to wait longer than one interval.
	NotBefore time.Time
}

func readKey(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// Cycle reads the key, builds one snapshot and sends it. It never panics on
// failures: they are logged and the next cycle tries again.
func (r *Reporter) Cycle(ctx context.Context) CycleResult {
	log := r.log()
	var res CycleResult

	key, err := readKey(r.KeyFile)
	if err != nil || key == "" {
		log.Error("no ingest key; skipping this cycle", "file", r.KeyFile, "readError", err != nil)
		return res
	}

	timeout := r.BuildTimeout
	if timeout <= 0 {
		timeout = DefaultBuildTimeout
	}
	buildCtx, cancel := context.WithTimeout(ctx, timeout)
	snap, err := r.Snapshots.Snapshot(buildCtx, r.sequence)
	cancel()
	if err != nil {
		log.Error("could not build the inventory snapshot", "error", err)
		return res
	}
	r.sequence++
	res.Built, res.Sequence = true, snap.Sequence
	if r.OnBuilt != nil {
		r.OnBuilt()
	}

	enc, err := inventory.Encode(snap)
	if err != nil {
		log.Error("could not encode the inventory snapshot", "error", err)
		return res
	}
	if enc.Dropped > 0 {
		log.Warn("the snapshot was cut to the ingest limits (5,000 workloads, 2 MiB gzip)",
			"workloads", enc.Workloads, "dropped", enc.Dropped, "gzipBytes", len(enc.Body))
	}

	res.Sent = r.Sender.Send(ctx, key, enc.Body)
	res.NotBefore = res.Sent.NotBefore
	attrs := []any{
		"sequence", snap.Sequence, "workloads", enc.Workloads, "gzipBytes", len(enc.Body),
		"mode", snap.Reporter.Mode, "status", res.Sent.Status, "attempts", res.Sent.Attempts,
	}
	if res.Sent.Outcome == sender.Accepted {
		log.Info("inventory sent", attrs...)
	} else {
		log.Warn("inventory not delivered", append(attrs, "outcome", res.Sent.Outcome.String())...)
	}
	return res
}

// safeCycle runs a cycle and turns a panic into a log line, so one bad
// response cannot stop the reporter.
func (r *Reporter) safeCycle(ctx context.Context) (res CycleResult) {
	defer func() {
		if p := recover(); p != nil {
			r.log().Error("a cycle panicked; the next one will try again", "panic", fmt.Sprint(p))
		}
	}()
	return r.Cycle(ctx)
}

// Run cycles until the context ends: the first cycle at once, then one every
// interval, or later when ingest asked to wait longer.
func (r *Reporter) Run(ctx context.Context) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		res := r.safeCycle(ctx)
		wait := r.Interval
		if !res.NotBefore.IsZero() {
			wait = max(wait, res.NotBefore.Sub(r.now()))
		}
		if err := r.sleep(ctx, wait); err != nil {
			return err
		}
	}
}
