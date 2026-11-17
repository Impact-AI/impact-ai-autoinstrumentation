// SPDX-License-Identifier: Apache-2.0

package loop

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/inventory"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/sender"
)

var start = time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)

type fakeSnapshots struct {
	sequences []int64
	err       error
	workloads int
	block     bool
}

func (f *fakeSnapshots) Snapshot(ctx context.Context, sequence int64) (*inventory.Snapshot, error) {
	f.sequences = append(f.sequences, sequence)
	if f.block {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if f.err != nil {
		return nil, f.err
	}
	s := &inventory.Snapshot{
		Schema:   inventory.SchemaV1,
		Cluster:  inventory.Cluster{UID: "uid", Provider: "other", KubernetesVersion: "1.31.2"},
		Reporter: inventory.ReporterInfo{Version: "t", Instance: "i", Mode: "inject"},
		Sequence: sequence, ObservedAt: "2026-11-03T12:00:00Z",
	}
	for i := 0; i < f.workloads; i++ {
		s.Workloads = append(s.Workloads, inventory.Workload{
			Namespace: "ns", Kind: "Deployment", Name: fmt.Sprintf("w-%05d", i), Pods: inventory.Pods{Desired: 1, Ready: 1},
			ServiceAccount: "default", Annotations: map[string]string{},
			Containers: []inventory.Container{{Name: "app", Image: "x", Executables: []string{}, EnvNames: []string{}, SecretRefs: []inventory.SecretRef{}, EnvFrom: []inventory.EnvFromRef{}}},
		})
	}
	return s, nil
}

type fakePoster struct {
	keys    []string
	bodies  [][]byte
	results []sender.Result
}

func (f *fakePoster) Send(_ context.Context, key string, body []byte) sender.Result {
	f.keys = append(f.keys, key)
	f.bodies = append(f.bodies, body)
	if len(f.results) == 0 {
		return sender.Result{Outcome: sender.Accepted, Status: 202, Attempts: 1}
	}
	r := f.results[0]
	f.results = f.results[1:]
	return r
}

type rig struct {
	*Reporter
	snaps   *fakeSnapshots
	poster  *fakePoster
	keyFile string
	logs    *bytes.Buffer
	sleeps  []time.Duration
	now     time.Time
}

func newRig(t *testing.T) *rig {
	t.Helper()
	r := &rig{snaps: &fakeSnapshots{}, poster: &fakePoster{}, logs: &bytes.Buffer{}, now: start}
	r.keyFile = filepath.Join(t.TempDir(), "ingest-key")
	r.writeKey(t, "key-one\n")
	r.Reporter = New(Reporter{
		Snapshots: r.snaps,
		Sender:    r.poster,
		KeyFile:   r.keyFile,
		Interval:  5 * time.Minute,
		Now:       func() time.Time { return r.now },
		Sleep: func(ctx context.Context, d time.Duration) error {
			r.sleeps = append(r.sleeps, d)
			r.now = r.now.Add(d)
			return ctx.Err()
		},
		Log: slog.New(slog.NewJSONHandler(r.logs, nil)),
	})
	return r
}

func (r *rig) writeKey(t *testing.T, key string) {
	t.Helper()
	if err := os.WriteFile(r.keyFile, []byte(key), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestSequenceIsSeededFromTheClockAndRisesByOne(t *testing.T) {
	r := newRig(t)
	for range 3 {
		r.Cycle(context.Background())
	}
	want := []int64{start.Unix(), start.Unix() + 1, start.Unix() + 2}
	if fmt.Sprint(r.snaps.sequences) != fmt.Sprint(want) {
		t.Errorf("sequences = %v, want %v", r.snaps.sequences, want)
	}

	// A restarted reporter (a new instance, later in time) starts above where the
	// old one stopped, because the seed is the clock.
	later := newRig(t)
	later.now = start.Add(15 * time.Minute)
	later.Reporter = New(Reporter{Snapshots: later.snaps, Sender: later.poster, KeyFile: later.keyFile, Interval: time.Minute, Now: func() time.Time { return later.now }})
	later.Cycle(context.Background())
	if got := later.snaps.sequences[0]; got <= want[2] {
		t.Errorf("restart sequence %d does not rise above the previous %d", got, want[2])
	}
}

func TestEachCycleSendsTheEncodedSnapshotWithTheKey(t *testing.T) {
	r := newRig(t)
	r.snaps.workloads = 3
	res := r.Cycle(context.Background())
	if !res.Built || res.Sent.Outcome != sender.Accepted || res.Sequence != start.Unix() {
		t.Fatalf("result = %+v", res)
	}
	if len(r.poster.keys) != 1 || r.poster.keys[0] != "key-one" {
		t.Errorf("keys = %q, want the trimmed key", r.poster.keys)
	}
	zr, err := gzip.NewReader(bytes.NewReader(r.poster.bodies[0]))
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := io.ReadAll(zr)
	var got inventory.Snapshot
	if err := json.Unmarshal(raw, &got); err != nil || len(got.Workloads) != 3 || got.Sequence != start.Unix() {
		t.Errorf("body = %s (%v)", raw, err)
	}
}

func TestTheKeyIsReadAgainForEveryCycle(t *testing.T) {
	r := newRig(t)
	r.Cycle(context.Background())
	r.writeKey(t, "  key-two \n\n")
	r.Cycle(context.Background())
	if fmt.Sprint(r.poster.keys) != "[key-one key-two]" {
		t.Errorf("keys = %q, want the rotated key on the second cycle", r.poster.keys)
	}
	if strings.Contains(r.logs.String(), "key-one") || strings.Contains(r.logs.String(), "key-two") {
		t.Errorf("a key was logged: %s", r.logs)
	}
}

func TestNoKeySkipsTheCycleWithoutUsingASequence(t *testing.T) {
	for name, setup := range map[string]func(*testing.T, *rig){
		"empty":   func(t *testing.T, r *rig) { r.writeKey(t, " \n") },
		"missing": func(t *testing.T, r *rig) { _ = os.Remove(r.keyFile) },
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t)
			setup(t, r)
			res := r.Cycle(context.Background())
			if res.Built || len(r.snaps.sequences) != 0 || len(r.poster.keys) != 0 {
				t.Errorf("nothing may be built or sent without a key: %+v", res)
			}
			if !strings.Contains(r.logs.String(), `"level":"ERROR"`) {
				t.Errorf("a missing key must be logged as an error: %s", r.logs)
			}
			r.writeKey(t, "key-three")
			r.Cycle(context.Background())
			if r.snaps.sequences[0] != start.Unix() {
				t.Errorf("the skipped cycle used a sequence: %v", r.snaps.sequences)
			}
		})
	}
}

func TestSnapshotFailureSendsNothingAndKeepsTheSequence(t *testing.T) {
	r := newRig(t)
	r.snaps.err = errors.New("list deployments: 500")
	res := r.Cycle(context.Background())
	if res.Built || len(r.poster.keys) != 0 {
		t.Errorf("result = %+v, sent %d", res, len(r.poster.keys))
	}
	r.snaps.err = nil
	r.Cycle(context.Background())
	if r.snaps.sequences[0] != r.snaps.sequences[1] {
		t.Errorf("a failed build must not use up a sequence: %v", r.snaps.sequences)
	}
}

func TestBuildTimeoutEndsAHangingSnapshot(t *testing.T) {
	r := newRig(t)
	r.snaps.block = true
	r.BuildTimeout = 20 * time.Millisecond
	done := make(chan CycleResult, 1)
	go func() { done <- r.Cycle(context.Background()) }()
	select {
	case res := <-done:
		if res.Built {
			t.Errorf("result = %+v", res)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the build timeout did not apply")
	}
}

func TestReadyCallbackRunsAfterASnapshotIsBuiltEvenIfSendingFails(t *testing.T) {
	r := newRig(t)
	calls := 0
	r.OnBuilt = func() { calls++ }
	r.poster.results = []sender.Result{{Outcome: sender.GaveUp, Status: 503, Attempts: 5, Err: errors.New("x")}, {Outcome: sender.Accepted}}
	r.Cycle(context.Background())
	if calls != 1 {
		t.Errorf("OnBuilt ran %d times after a built snapshot whose sending failed, want 1", calls)
	}
	r.Cycle(context.Background())
	if calls != 2 {
		t.Errorf("OnBuilt ran %d times after two built snapshots, want 2 (it must be idempotent for the caller)", calls)
	}

	failing := newRig(t)
	failing.snaps.err = errors.New("boom")
	failing.OnBuilt = func() { t.Error("ready before any snapshot was built") }
	failing.Cycle(context.Background())
}

func TestWarnsOnceWhenTheLimitsCutWorkloads(t *testing.T) {
	r := newRig(t)
	r.snaps.workloads = 6000
	r.Cycle(context.Background())
	if n := strings.Count(r.logs.String(), "ingest limits"); n != 1 {
		t.Errorf("%d limit warnings, want exactly one:\n%s", n, r.logs)
	}
	var line map[string]any
	for _, l := range strings.Split(r.logs.String(), "\n") {
		if strings.Contains(l, "ingest limits") {
			if err := json.Unmarshal([]byte(l), &line); err != nil {
				t.Fatal(err)
			}
		}
	}
	if line["level"] != "WARN" || line["workloads"] != float64(5000) || line["dropped"] != float64(1000) {
		t.Errorf("limit warning = %v, want level WARN with workloads 5000 and dropped 1000", line)
	}
}

func TestRunWaitsOneIntervalBetweenCycles(t *testing.T) {
	r := newRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	r.Sleep = func(ctx context.Context, d time.Duration) error {
		r.sleeps = append(r.sleeps, d)
		if len(r.sleeps) == 3 {
			cancel()
		}
		return ctx.Err()
	}
	err := r.Run(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Run returned %v, want context.Canceled", err)
	}
	if len(r.snaps.sequences) != 3 {
		t.Errorf("%d cycles ran, want 3 (the first one immediately)", len(r.snaps.sequences))
	}
	for _, d := range r.sleeps {
		if d != 5*time.Minute {
			t.Errorf("waited %v, want 5m", d)
		}
	}
}

func TestRetryAfterLongerThanTheIntervalDelaysTheNextCycle(t *testing.T) {
	r := newRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	r.poster.results = []sender.Result{
		{Outcome: sender.GaveUp, Status: 503, Attempts: 1, NotBefore: start.Add(12 * time.Minute), Err: errors.New("x")},
		{Outcome: sender.Accepted},
	}
	r.Sleep = func(ctx context.Context, d time.Duration) error {
		r.sleeps = append(r.sleeps, d)
		r.now = r.now.Add(d)
		if len(r.sleeps) == 2 {
			cancel()
		}
		return ctx.Err()
	}
	_ = r.Run(ctx)
	if len(r.sleeps) != 2 || r.sleeps[0] != 12*time.Minute || r.sleeps[1] != 5*time.Minute {
		t.Errorf("waits = %v, want 12m (Retry-After) then 5m", r.sleeps)
	}
}

func TestRejectedAndKeyRejectedKeepTheNormalInterval(t *testing.T) {
	for _, outcome := range []sender.Outcome{sender.Rejected, sender.KeyRejected} {
		r := newRig(t)
		ctx, cancel := context.WithCancel(context.Background())
		r.poster.results = []sender.Result{{Outcome: outcome, Status: 413, Attempts: 1, Err: errors.New("x")}}
		r.Sleep = func(ctx context.Context, d time.Duration) error {
			r.sleeps = append(r.sleeps, d)
			cancel()
			return ctx.Err()
		}
		_ = r.Run(ctx)
		if len(r.poster.bodies) != 1 || len(r.sleeps) != 1 || r.sleeps[0] != 5*time.Minute {
			t.Errorf("%v: sent %d bodies, waits %v", outcome, len(r.poster.bodies), r.sleeps)
		}
	}
}

type panickingSnapshots struct{ calls int }

func (p *panickingSnapshots) Snapshot(context.Context, int64) (*inventory.Snapshot, error) {
	p.calls++
	if p.calls == 1 {
		panic("unexpected response shape")
	}
	return &inventory.Snapshot{Schema: inventory.SchemaV1}, nil
}

func TestAPanicInOneCycleDoesNotStopTheReporter(t *testing.T) {
	r := newRig(t)
	snaps := &panickingSnapshots{}
	r.Snapshots = snaps
	ctx, cancel := context.WithCancel(context.Background())
	r.Sleep = func(ctx context.Context, d time.Duration) error {
		r.sleeps = append(r.sleeps, d)
		if len(r.sleeps) == 2 {
			cancel()
		}
		return ctx.Err()
	}
	_ = r.Run(ctx)
	if snaps.calls != 2 || len(r.poster.keys) != 1 {
		t.Errorf("snapshots built %d times, %d sent; want a retry after the panic", snaps.calls, len(r.poster.keys))
	}
	if !strings.Contains(r.logs.String(), "panicked") {
		t.Errorf("the panic was not logged: %s", r.logs)
	}
}

func TestRunStopsPromptlyWhenTheContextEnds(t *testing.T) {
	r := newRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Errorf("Run = %v", err)
	}
}
