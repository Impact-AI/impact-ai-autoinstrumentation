// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"
	"time"
)

// A tool's SDK given a base URL (Traceloop, Langfuse, Braintrust, Phoenix, Logfire) sends traces, metrics, prompts and
// scores to paths under it. Through a base route every one of them reaches the base URL plus its own path and query,
// and only the OTLP trace exports are copied.
func TestABaseURLRouteForwardsEveryPathUnderTheBaseAndCopiesOnlyTraceExports(t *testing.T) {
	backend := newRecorder(t, func(w http.ResponseWriter, r *http.Request, _ []byte) {
		w.Header().Set("X-Vendor-Answer", r.URL.Path)
		if r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"prompt":"vendor answer"}`))
		}
	})
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "llm-app", Protocol: ProtocolOTLPHTTP, Base: true, Source: "vendor",
		Target: backend.srv.URL + "/tenant?token=" + plantedToken}, copyTo, gatewayOptions{})

	calls := []struct {
		method, path, query string
		body                []byte
		header              http.Header
		copied              bool
	}{
		{http.MethodPost, "/v1/traces", "", otlpBatch, protobuf(), true},
		{http.MethodPost, "/v1/metrics", "", []byte("metrics export"), protobuf(), false},
		{http.MethodPost, "/v1/logs", "", []byte("logs export"), protobuf(), false},
		{http.MethodGet, "/v1/traces", "", nil, nil, false},
		{http.MethodGet, "/api/public/v2/prompts/support", "label=production", nil, nil, false},
		{http.MethodPost, "/api/public/scores", "", []byte(`{"name":"helpful","value":1}`), http.Header{"Content-Type": {"application/json"}}, false},
		{http.MethodPost, "/api/public/otel/v1/traces", "", gzipped(t, otlpBatch), http.Header{"Content-Type": {"application/x-protobuf"}, "Content-Encoding": {"gzip"}}, true},
		{http.MethodPost, "/otel/v1/traces", "", otlpBatch, protobuf(), true},
	}
	want := 0
	for i, c := range calls {
		url := g.url + c.path
		if c.query != "" {
			url += "?" + c.query
		}
		req := exportRequest(t, url, c.body, c.header)
		req.Method = c.method
		resp, answer := do(t, http.DefaultClient, req)
		if resp.StatusCode != http.StatusOK || resp.Header.Get("X-Vendor-Answer") != "/tenant"+c.path {
			t.Errorf("%s %s: answered %d by %q", c.method, c.path, resp.StatusCode, resp.Header.Get("X-Vendor-Answer"))
		}
		if c.method == http.MethodGet && string(answer) != `{"prompt":"vendor answer"}` {
			t.Errorf("%s %s: the vendor's answer changed: %q", c.method, c.path, answer)
		}
		got := backend.requests()[i]
		wantQuery := "token=" + plantedToken
		if c.query != "" {
			wantQuery += "&" + c.query
		}
		if got.Method != c.method || got.Path != "/tenant"+c.path || got.Query != wantQuery || !bytes.Equal(got.Body, c.body) {
			t.Errorf("%s %s reached the backend as %s %s?%s", c.method, c.path, got.Method, got.Path, got.Query)
		}
		if c.copied {
			want++
		}
	}
	eventually(t, "the trace copies", func() bool { return len(copyTo.requests()) == want })
	never(t, "another copy", func() bool { return len(copyTo.requests()) > want })
	for _, c := range copyTo.requests() {
		if c.Path != "/v1/traces" || !bytes.Equal(c.Body, otlpBatchCopy) {
			t.Errorf("copy: %s %q", c.Path, c.Body)
		}
	}
	if s := g.stats.Snapshot()["llm-app"]; s.Forwarded != uint64(len(calls)) || s.Partial != 0 {
		t.Errorf("stats %+v", s)
	}
}

// Without base, the target is the traces URL itself: a trace export goes there whatever path the exporter used.
func TestAnExactTargetRouteSendsTheExportToTheTargetURL(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "llm-app", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/ingest/traces"}, copyTo, gatewayOptions{})
	do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, protobuf()))
	if got := backend.requests(); len(got) != 1 || got[0].Path != "/ingest/traces" {
		t.Fatalf("backend saw %+v", got)
	}
	eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
}

// httputil.ReverseProxy re-encodes a query it cannot parse; the gateway passes the workload's query on as it came.
func TestARelayPassesAQueryThatGoDoesNotParseByteForByte(t *testing.T) {
	backend := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "vendor", Protocol: ProtocolRelay, Target: backend.srv.URL + "/?api_key=" + plantedToken}, nil, gatewayOptions{})
	for _, query := range []string{"b=1;a=2", "z=%zz&a=1", "a=1&b=2"} {
		do(t, http.DefaultClient, exportRequest(t, g.url+"/intake?"+query, []byte("vendor bytes"), nil))
		got := backend.requests()
		if q := got[len(got)-1].Query; q != "api_key="+plantedToken+"&"+query {
			t.Errorf("query %q reached the backend as %q", query, q)
		}
	}
}

// A copy says which tool and which route its spans came by, whatever the workload's own resource claimed.
func TestAnOTLPCopyCarriesTheRoutesProvenanceAndNotTheWorkloadsClaim(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "llm-app", Protocol: ProtocolOTLPHTTP, Base: true, Source: "traceloop", Target: backend.srv.URL}, copyTo, gatewayOptions{})

	export := `{"resourceSpans":[` +
		`{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"support"}},` +
		`{"key":"impact.source.route","value":{"stringValue":"relay"}},{"key":"impact.source.decoder","value":{"stringValue":"datadog/1"}}]},` +
		`"scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"chat <gpt-4o>","startTimeUnixNano":"1731150000000000000","attributes":[{"key":"gen_ai.usage.input_tokens","value":{"intValue":"420"}}]}]}]},` +
		`{"scopeSpans":[]}]}`
	do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", []byte(export), http.Header{"Content-Type": {"application/json"}}))
	eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
	c := copyTo.requests()[0]
	if c.Header.Get("Content-Type") != "application/json" {
		t.Errorf("content type %q", c.Header.Get("Content-Type"))
	}
	var doc struct {
		ResourceSpans []struct {
			Resource struct {
				Attributes []struct {
					Key   string `json:"key"`
					Value struct {
						StringValue string `json:"stringValue"`
					} `json:"value"`
				} `json:"attributes"`
			} `json:"resource"`
			ScopeSpans []json.RawMessage `json:"scopeSpans"`
		} `json:"resourceSpans"`
	}
	if err := json.Unmarshal(c.Body, &doc); err != nil || len(doc.ResourceSpans) != 2 {
		t.Fatalf("copy %s: %v", c.Body, err)
	}
	for i, rs := range doc.ResourceSpans {
		var got []string
		for _, a := range rs.Resource.Attributes {
			got = append(got, a.Key+"="+a.Value.StringValue)
		}
		want := []string{"impact.source.id=traceloop", "impact.source.route=copy"}
		if i == 0 {
			want = append([]string{"service.name=support"}, want...)
		}
		if !slices.Equal(got, want) {
			t.Errorf("resource %d: %v, want %v", i, got, want)
		}
	}
	// The spans themselves are copied as they came: the ids, the name and the token count keep their text.
	for _, s := range []string{`"traceId":"5b8efff798038103d269b633813fc60c"`, `"name":"chat <gpt-4o>"`, `"intValue":"420"`, `"startTimeUnixNano":"1731150000000000000"`} {
		if !bytes.Contains(c.Body, []byte(s)) {
			t.Errorf("the copy lost %s: %s", s, c.Body)
		}
	}
}

func TestAProtobufCopyReplacesTheWorkloadsProvenanceClaimAndKeepsEverythingElse(t *testing.T) {
	// The workload's resource claims impact.source.route=relay and repeats the Resource field, as a protobuf encoder
	// may; dropped_attributes_count (field 2) and an unknown field (99) pass through.
	claim := "\x0a\x1e\x0a\x13impact.source.route\x12\x07\x0a\x05relay"
	resource1 := "\x0a\x1a\x0a\x0cservice.name\x12\x0a\x0a\x08checkout" + claim
	resource2 := "\x10\x03"
	unknown := "\x9a\x06\x02hi"
	rs := "\x0a" + string(rune(len(resource1))) + resource1 + "\x0a\x02" + resource2 + "\x12\x04\x12\x02\x2a\x00" + unknown
	export := "\x0a" + string(rune(len(rs))) + rs

	out, spans, err := stampProtobuf([]byte(export), copyProvenance(Item{Source: "vendor"}))
	if err != nil || !spans {
		t.Fatalf("stamp: %v %v", spans, err)
	}
	wantResource := "\x0a\x1a\x0a\x0cservice.name\x12\x0a\x0a\x08checkout" + "\x10\x03" +
		"\x0a\x1c\x0a\x10impact.source.id\x12\x08\x0a\x06vendor" + "\x0a\x1d\x0a\x13impact.source.route\x12\x06\x0a\x04copy"
	wantRS := "\x0a" + string(rune(len(wantResource))) + wantResource + "\x12\x04\x12\x02\x2a\x00" + unknown
	want := "\x0a" + string(rune(len(wantRS))) + wantRS
	if string(out) != want {
		t.Errorf("stamped\n%q\nwant\n%q", out, want)
	}
	for _, bad := range []string{"\x0a\x05\x0a\x03", "\x0b\x0c", "\x0a\x02\x08"} {
		if _, _, err := stampProtobuf([]byte(bad), copyProvenance(Item{Source: "vendor"})); err == nil {
			t.Errorf("%q: no error", bad)
		}
	}
}

// slowBackend reads each request whole, then holds its answer until release is closed.
func slowBackend(t *testing.T) (*recorder, chan struct{}, chan struct{}) {
	t.Helper()
	arrived := make(chan struct{}, 16)
	release := make(chan struct{})
	backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		arrived <- struct{}{}
		<-release
	})
	return backend, arrived, release
}

// send makes a request from another goroutine than the test's.
func send(t *testing.T, req *http.Request) {
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Error(err)
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
}

// The bodies held for copies are bounded across the gateway: a request over the budget is still forwarded whole and
// its copy counted as partial, a request that cannot be copied is never held, and the budget comes back.
func TestTheBodiesHeldForCopiesStayWithinTheCaptureBudget(t *testing.T) {
	backend, arrived, release := slowBackend(t)
	copyTo := newRecorder(t, nil)
	stats := NewStats()
	queue := NewQueue(DefaultQueueBytes, stats)
	budget := NewCaptureBudget(int64(len(otlpBatch)) + 10)
	h, err := NewHandler(Route{Name: "llm-app", Protocol: ProtocolOTLPHTTP, Base: true, Source: "vendor", Target: backend.srv.URL},
		Options{Queue: queue, Stats: stats, Captures: budget})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})

	// Metrics are only forwarded: nothing of them is held while the backend takes its time.
	go send(t, exportRequest(t, srv.URL+"/v1/metrics", []byte("metrics export"), protobuf()))
	<-arrived
	if n := budget.InUse(); n != 0 {
		t.Errorf("a metrics export holds %d bytes for a copy", n)
	}
	// A trace export is held for its copy...
	first := make(chan struct{})
	req := exportRequest(t, srv.URL+"/v1/traces", otlpBatch, protobuf())
	go func() { defer close(first); send(t, req) }()
	<-arrived
	if n := budget.InUse(); n != int64(len(otlpBatch)) {
		t.Errorf("the trace export holds %d bytes, want %d", n, len(otlpBatch))
	}
	// ...and a second one, which the budget cannot also hold, is forwarded whole and its copy given up.
	second := make(chan struct{})
	req = exportRequest(t, srv.URL+"/v1/traces", otlpBatch, protobuf())
	go func() { defer close(second); send(t, req) }()
	<-arrived
	if n := budget.InUse(); n != int64(len(otlpBatch)) {
		t.Errorf("the budget holds %d bytes, want %d", n, len(otlpBatch))
	}
	close(release)
	<-first
	<-second
	if got := backend.requests(); len(got) != 3 || !bytes.Equal(got[1].Body, otlpBatch) || !bytes.Equal(got[2].Body, otlpBatch) {
		t.Errorf("the exports did not reach the backend whole")
	}
	eventually(t, "one copy queued and one partial", func() bool {
		s := stats.Snapshot()["llm-app"]
		return s.Queued == 1 && s.Partial == 1
	})
	eventually(t, "the budget back", func() bool { return budget.InUse() == 0 })
	_ = copyTo
}

// A copy given up because the backend did not accept the batch gives its bytes back to the budget.
func TestABatchTheBackendRefusesGivesItsCaptureBack(t *testing.T) {
	backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) { w.WriteHeader(http.StatusBadRequest) })
	stats := NewStats()
	budget := NewCaptureBudget(DefaultCaptureBytes)
	h, err := NewHandler(Route{Name: "a", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"},
		Options{Queue: NewQueue(DefaultQueueBytes, stats), Stats: stats, Captures: budget})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	for range 3 {
		do(t, http.DefaultClient, exportRequest(t, srv.URL+"/v1/traces", otlpBatch, protobuf()))
	}
	deadline := time.Now().Add(time.Second)
	for budget.InUse() != 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if n := budget.InUse(); n != 0 {
		t.Errorf("%d bytes still held", n)
	}
}
