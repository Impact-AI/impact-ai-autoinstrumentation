// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func item(route string, size int, tag byte) Item {
	return Item{Route: route, Kind: KindOTLP, ContentType: "application/x-protobuf", Body: bytes.Repeat([]byte{tag}, size)}
}

func TestTheQueueKeepsItsBoundByDroppingTheOldestAndCountsWhatItDrops(t *testing.T) {
	stats := NewStats()
	cost := item("a", 40, '1').storageBytes()
	q := NewQueue(2*cost, stats)
	q.Push(item("a", 40, '1'))
	q.Push(item("b", 40, '2'))
	q.Push(item("a", 40, '3')) // only two fit: the oldest goes

	if q.Bytes() != 2*cost || q.Len() != 2 {
		t.Fatalf("queue holds %d bytes in %d items, want two charged items", q.Bytes(), q.Len())
	}
	head, err := q.Peek(context.Background())
	if err != nil || head.Body[0] != '2' {
		t.Fatalf("head %q %v, want the second batch", head.Body[:1], err)
	}
	a := stats.Snapshot()["a"]
	if a.Queued != 2 || a.Dropped != 1 || a.DroppedBytes != 40 {
		t.Errorf("route a: %+v, want 2 queued, 1 dropped of 40 bytes", a)
	}

	q.Push(item("b", int(q.max)+1, '4')) // larger than the whole queue: dropped at once, nothing else goes
	if q.Len() != 2 {
		t.Errorf("an oversized batch pushed others out")
	}
	if b := stats.Snapshot()["b"]; b.Dropped != 1 || b.DroppedBytes != uint64(q.max)+1 {
		t.Errorf("route b: %+v, want the oversized batch dropped and counted", b)
	}
	if got := stats.QueueDropped(); got != 2 {
		t.Errorf("total dropped %d, want 2", got)
	}
}

func TestDoneRemovesTheHeadOnlyIfItIsStillThere(t *testing.T) {
	q := NewQueue(item("a", 60, '1').storageBytes(), NewStats())
	q.Push(item("a", 60, '1'))
	head, _ := q.Peek(context.Background())
	q.Push(item("a", 60, '2')) // pushes the head out while it is being sent
	q.Done(head)
	if q.Len() != 1 {
		t.Fatalf("Done removed the newer batch")
	}
	next, _ := q.Peek(context.Background())
	if next.Body[0] != '2' {
		t.Errorf("head %q", next.Body[:1])
	}
}

func TestPeekWaitsForAnItemOrTheEndOfTheContext(t *testing.T) {
	q := NewQueue(1024, NewStats())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := q.Peek(ctx); err == nil {
		t.Fatal("Peek on an empty queue returned without an item")
	}
	go func() { time.Sleep(10 * time.Millisecond); q.Push(item("a", 1, '1')) }()
	if _, err := q.Peek(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestWhileImpactAIIsDownTheBackendGetsEverythingAndTheCopyResumesByItself(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		http.Error(w, "down", http.StatusServiceUnavailable)
	})
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, copyTo, gatewayOptions{})

	for range 3 {
		resp, _ := do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, protobuf()))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("export failed while impact.ai is down: %d", resp.StatusCode)
		}
	}
	if len(backend.requests()) != 3 {
		t.Fatalf("backend missed spans")
	}
	eventually(t, "retries", func() bool { return len(copyTo.requests()) >= 3 })

	copyTo.setAnswer(nil) // impact.ai is back
	eventually(t, "all three copied", func() bool { return g.stats.Snapshot()["checkout"].Copied == 3 })
	if g.queue.Len() != 0 {
		t.Errorf("queue not drained")
	}
	if !strings.Contains(g.logs.String(), "copy resumed") {
		t.Errorf("the recovery was not logged")
	}
}

func TestACopyImpactAIRefusesIsDroppedAndCounted(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		http.Error(w, "bad", http.StatusBadRequest)
	})
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, copyTo, gatewayOptions{})
	do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, protobuf()))
	eventually(t, "refusal counted", func() bool { return g.stats.Snapshot()["checkout"].CopyRejected == 1 })
	never(t, "a retry of a refused copy", func() bool { return len(copyTo.requests()) > 1 })
}

func TestARevokedKeyHoldsTheCopiesAndANewKeyResumesThem(t *testing.T) {
	keyFile := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(keyFile, []byte("old-install-key\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, func(w http.ResponseWriter, r *http.Request, _ []byte) {
		if r.Header.Get("Authorization") != "Bearer new-install-key" {
			http.Error(w, "revoked", http.StatusUnauthorized)
		}
	})
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, copyTo, gatewayOptions{keyFile: keyFile})
	do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, protobuf()))
	eventually(t, "a refused attempt", func() bool { return len(copyTo.requests()) >= 1 })
	if g.stats.Snapshot()["checkout"].Copied != 0 || g.queue.Len() != 1 {
		t.Fatalf("the batch was not held")
	}
	eventually(t, "the refused key logged", func() bool { return strings.Contains(g.logs.String(), "refused the install key") })
	if err := os.WriteFile(keyFile, []byte("new-install-key\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	eventually(t, "copied with the new key", func() bool { return g.stats.Snapshot()["checkout"].Copied == 1 })
	assertNoSecret(t, "the logs", g.logs.String())
	if strings.Contains(g.logs.String(), "install-key") {
		t.Errorf("the install key reached the logs")
	}
}

func protobuf() http.Header { return http.Header{"Content-Type": {"application/x-protobuf"}} }

// The byte budget must also bound empty/tiny batches and protocol metadata while the copier cannot drain it.
func TestTheQueueBoundsSmallBatchesAndMetadata(t *testing.T) {
	stats := NewStats()
	q := NewQueue(4096, stats)
	for range 1000 {
		q.Push(item("a", 0, 0))
	}
	if q.Len() > 8 || q.Bytes() == 0 || q.Bytes() > 4096 {
		t.Fatalf("empty batches escaped the bound: %d items, %d charged bytes", q.Len(), q.Bytes())
	}
	if s := stats.Snapshot()["a"]; s.Dropped != 1000-uint64(q.Len()) || s.DroppedBytes != 0 {
		t.Fatalf("wrong empty-batch loss counters: %+v", s)
	}
	for _, it := range []Item{
		{Route: "large", ContentType: strings.Repeat("x", 4096)},
		{Route: "large", Encoding: strings.Repeat("x", 4096)},
		{Route: "large", Relay: &RelayCall{Query: strings.Repeat("x", 4096)}},
		{Route: "large", Relay: &RelayCall{Header: http.Header{"User-Agent": {strings.Repeat("x", 4096)}}}},
		{Route: "large", Relay: &RelayCall{Header: http.Header{"User-Agent": make([]string, 4096)}}},
	} {
		before := q.Len()
		q.Push(it)
		if q.Len() != before || q.Bytes() > 4096 {
			t.Fatal("oversized metadata displaced queued copies or escaped the bound")
		}
	}
	if s := stats.Snapshot()["large"]; s.Dropped != 5 || s.DroppedBytes != 0 {
		t.Fatalf("metadata was not dropped, or counted as payload: %+v", s)
	}
	for q.Len() > 0 {
		head, _ := q.Peek(context.Background())
		q.Done(head)
	}
	if q.Bytes() != 0 {
		t.Fatalf("draining left %d charged bytes", q.Bytes())
	}
}

func TestTinyChunkedExportsStillReachTheVendorWhenTheCopyQueueFills(t *testing.T) {
	backend := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, nil, gatewayOptions{queueBytes: 4096, noCopier: true})
	for i := range 32 {
		body := ""
		if i%2 == 1 {
			body = "{}"
		}
		req := exportRequest(t, g.url+"/v1/traces", []byte(body), http.Header{"Content-Type": {"application/json"}})
		// Force chunked encoding, including for an empty body, just as a streaming exporter can send.
		req.Body = io.NopCloser(strings.NewReader(body))
		req.ContentLength = -1
		resp, _ := do(t, http.DefaultClient, req)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("vendor status %d", resp.StatusCode)
		}
	}
	eventually(t, "all copies considered", func() bool { return g.stats.Snapshot()["checkout"].Queued == 32 })
	if len(backend.requests()) != 32 || g.queue.Len() > 8 || g.queue.Bytes() > 4096 {
		t.Fatalf("forwarded %d; queue has %d items, %d charged bytes", len(backend.requests()), g.queue.Len(), g.queue.Bytes())
	}
	if g.stats.Snapshot()["checkout"].Dropped == 0 {
		t.Fatal("copy loss was not counted")
	}
}
