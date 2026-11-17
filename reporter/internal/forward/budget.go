// SPDX-License-Identifier: Apache-2.0

package forward

import "sync/atomic"

// DefaultCaptureBytes bounds the request bodies one gateway replica holds for a copy while it forwards them. With the
// queue's own bound it is what the copies can cost the replica in memory.
const DefaultCaptureBytes = 64 << 20

// CaptureBudget is the memory the handlers sharing it may hold for copies of the requests they are forwarding. A
// request that would go over it is still forwarded whole; only its copy is given up and counted as partial.
type CaptureBudget struct {
	max  int64
	used atomic.Int64
}

// NewCaptureBudget returns a budget of maxBytes.
func NewCaptureBudget(maxBytes int64) *CaptureBudget { return &CaptureBudget{max: maxBytes} }

// InUse is how much of the budget the handlers hold now.
func (b *CaptureBudget) InUse() int64 { return b.used.Load() }

func (b *CaptureBudget) reserve(n int64) bool {
	for {
		used := b.used.Load()
		if used+n > b.max {
			return false
		}
		if b.used.CompareAndSwap(used, used+n) {
			return true
		}
	}
}

func (b *CaptureBudget) release(n int64) {
	if n != 0 {
		b.used.Add(-n)
	}
}
