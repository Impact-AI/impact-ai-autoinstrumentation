// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"sync"
	"sync/atomic"
)

// Counts are one route's numbers since the gateway started. They hold names and numbers, never a header or a target.
type Counts struct {
	// Forwarded requests got an answer from the backend (any status); NotAccepted of them were not a 2xx (or, over
	// gRPC, not grpc-status 0), so nothing of them was copied.
	Forwarded   uint64 `json:"forwarded"`
	NotAccepted uint64 `json:"notAccepted"`
	// Unreachable requests got no answer from the backend: the exporter got 502 Bad Gateway.
	Unreachable uint64 `json:"unreachable"`
	// Queued batches waited for impact.ai; Copied of them were accepted by it and CopyRejected refused (4xx).
	Queued       uint64 `json:"queued"`
	Copied       uint64 `json:"copied"`
	CopyRejected uint64 `json:"copyRejected"`
	// Dropped batches were pushed out of the full queue, oldest first, or were larger than the whole queue.
	Dropped      uint64 `json:"dropped"`
	DroppedBytes uint64 `json:"droppedBytes"`
	// Partial batches were forwarded but could not be copied, or only in part: a payload the gateway cannot read, a
	// compression it does not know, a batch larger than the copy limit, or spans it had to leave out.
	Partial uint64 `json:"partial"`
}

type counters struct {
	forwarded, notAccepted, unreachable, queued, copied, copyRejected, dropped, droppedBytes, partial atomic.Uint64
}

// Stats counts per route. The zero value is not usable; call NewStats.
type Stats struct {
	mu     sync.Mutex
	routes map[string]*counters
}

// NewStats returns empty counts.
func NewStats() *Stats { return &Stats{routes: map[string]*counters{}} }

func (s *Stats) route(name string) *counters {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.routes[name]
	if c == nil {
		c = &counters{}
		s.routes[name] = c
	}
	return c
}

// Snapshot returns every route's counts.
func (s *Stats) Snapshot() map[string]Counts {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]Counts, len(s.routes))
	for name, c := range s.routes {
		out[name] = Counts{
			Forwarded: c.forwarded.Load(), NotAccepted: c.notAccepted.Load(), Unreachable: c.unreachable.Load(),
			Queued: c.queued.Load(), Copied: c.copied.Load(), CopyRejected: c.copyRejected.Load(),
			Dropped: c.dropped.Load(), DroppedBytes: c.droppedBytes.Load(), Partial: c.partial.Load(),
		}
	}
	return out
}

// QueueDropped is the number of batches dropped from the queue over all routes.
func (s *Stats) QueueDropped() uint64 {
	var n uint64
	for _, c := range s.Snapshot() {
		n += c.Dropped
	}
	return n
}
