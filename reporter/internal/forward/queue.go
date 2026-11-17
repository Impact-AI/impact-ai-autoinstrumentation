// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"context"
	"net/http"
	"strings"
	"sync"
)

// DefaultQueueBytes bounds the accounted storage of batches waiting for impact.ai in one gateway replica.
const DefaultQueueBytes = 64 << 20

// Kind says how a queued batch becomes an OTLP copy.
type Kind int

const (
	// KindOTLP is the body of an OTLP/HTTP export: copied as it is, with its content type, and its provenance stamped
	// on every resource.
	KindOTLP Kind = iota
	// KindGRPC is the body of an OTLP/gRPC export: length-prefixed messages, maybe compressed (Encoding).
	KindGRPC
	// KindRelay is a request of a vendor protocol that the route's relay source decodes (Relay).
	KindRelay
)

// Item is one accepted batch waiting to be copied. It holds the body and how to read it. It never holds a credential:
// of the workload's request it keeps only what a relay decoder reads (Relay), never a header or query parameter whose
// name looks like one.
type Item struct {
	Route       string
	Source      string // the tool whose OTLP the route carries (KindOTLP, KindGRPC): the copy's provenance
	Kind        Kind
	Body        []byte
	ContentType string // the media type: KindOTLP application/x-protobuf or application/json
	Encoding    string // Content-Encoding (KindOTLP, KindRelay) or grpc-encoding (KindGRPC)
	Relay       *RelayCall

	seq uint64
}

// RelayCall is what a relay decoder reads of a request besides its body: the source, the method, the path, the query
// without its credential parameters and the headers the decoder declares that are not credentials.
type RelayCall struct {
	Source string
	Method string
	Path   string
	Query  string
	Header http.Header
}

// storageBytes charges retained buffers and metadata, plus a conservative allowance for Item, RelayCall and
// queue/map storage. A nonzero base charge bounds empty batches too; this is not a measurement of process RSS.
func (it Item) storageBytes() int64 {
	size := int64(512 + cap(it.Body) + len(it.Route) + len(it.Source) + len(it.ContentType) + len(it.Encoding))
	if r := it.Relay; r != nil {
		size += int64(len(r.Source) + len(r.Method) + len(r.Path) + len(r.Query))
		for key, values := range r.Header {
			size += int64(64 + len(key) + 16*len(values))
			for _, value := range values {
				size += int64(len(value))
			}
		}
	}
	return size
}

// ownMetadata detaches short parsed strings from possibly much larger HTTP request/header backing strings.
func (it Item) ownMetadata() Item {
	it.ContentType, it.Encoding = strings.Clone(it.ContentType), strings.Clone(it.Encoding)
	if r := it.Relay; r != nil {
		owned := *r
		owned.Method, owned.Path, owned.Query = strings.Clone(r.Method), strings.Clone(r.Path), strings.Clone(r.Query)
		owned.Header = make(http.Header, len(r.Header))
		for key, values := range r.Header {
			cloned := make([]string, len(values))
			for i, value := range values {
				cloned[i] = strings.Clone(value)
			}
			owned.Header[strings.Clone(key)] = cloned
		}
		it.Relay = &owned
	}
	return it
}

// Queue holds the batches waiting for impact.ai, charging bodies, metadata and per-item storage. A batch that does not fit pushes the
// oldest out; each one pushed out is counted on its route.
type Queue struct {
	max   int64
	stats *Stats

	mu    sync.Mutex
	items []Item
	bytes int64
	next  uint64
	ready chan struct{} // has a value while items is not empty
}

// NewQueue returns an empty queue of at most maxBytes.
func NewQueue(maxBytes int64, stats *Stats) *Queue {
	return &Queue{max: maxBytes, stats: stats, ready: make(chan struct{}, 1)}
}

// Push adds a batch, dropping the oldest until it fits. A batch larger than the whole queue is itself dropped.
func (q *Queue) Push(it Item) {
	size := it.storageBytes()
	if size > q.max {
		q.drop(it)
		return
	}
	it = it.ownMetadata()
	q.mu.Lock()
	for q.bytes+size > q.max && len(q.items) > 0 {
		old := q.items[0]
		q.items[0] = Item{}
		q.items = q.items[1:]
		q.bytes -= old.storageBytes()
		q.drop(old)
	}
	q.next++
	it.seq = q.next
	q.items = append(q.items, it)
	q.bytes += size
	q.mu.Unlock()
	q.stats.route(it.Route).queued.Add(1)
	select {
	case q.ready <- struct{}{}:
	default:
	}
}

func (q *Queue) drop(it Item) {
	c := q.stats.route(it.Route)
	c.dropped.Add(1)
	c.droppedBytes.Add(uint64(len(it.Body)))
}

// Peek waits for the oldest batch and returns it without removing it: it stays counted against the bound, and can
// still be pushed out, while it is being sent.
func (q *Queue) Peek(ctx context.Context) (Item, error) {
	for {
		q.mu.Lock()
		if len(q.items) > 0 {
			it := q.items[0]
			q.mu.Unlock()
			return it, nil
		}
		q.mu.Unlock()
		select {
		case <-ctx.Done():
			return Item{}, ctx.Err()
		case <-q.ready:
		}
	}
}

// Done removes a batch Peek returned, if it is still the oldest.
func (q *Queue) Done(it Item) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) > 0 && q.items[0].seq == it.seq {
		q.bytes -= q.items[0].storageBytes()
		q.items[0] = Item{}
		q.items = q.items[1:]
	}
	if len(q.items) > 0 {
		select {
		case q.ready <- struct{}{}:
		default:
		}
	}
}

// Has tells whether a batch Peek returned is still waiting.
func (q *Queue) Has(it Item) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items) > 0 && q.items[0].seq == it.seq
}

// Len is the number of waiting batches.
func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items)
}

// Bytes is the accounted storage of the waiting batches, including metadata and item overhead.
func (q *Queue) Bytes() int64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.bytes
}
