// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// The planted vendor credentials of the tests: a key in a header and a token in the target URL. They must reach the
// vendor's backend and nothing else.
const (
	plantedKey   = "planted-vendor-key-7f3a90"
	plantedToken = "planted-url-token-91c2e4"
)

// syncBuffer collects log output written from several goroutines.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// seen is one request as a server received it.
type seen struct {
	Method string
	Path   string
	Query  string
	Host   string
	Header http.Header
	Body   []byte
}

// recorder is a vendor backend or a copy endpoint: it records every request and answers with answer().
type recorder struct {
	mu     sync.Mutex
	seen   []seen
	answer func(w http.ResponseWriter, r *http.Request, body []byte)
	srv    *httptest.Server
}

func h2cProtocols() *http.Protocols {
	p := new(http.Protocols)
	p.SetHTTP1(true)
	p.SetUnencryptedHTTP2(true)
	return p
}

func newRecorder(t *testing.T, answer func(w http.ResponseWriter, r *http.Request, body []byte)) *recorder {
	t.Helper()
	rec := &recorder{answer: answer}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		rec.mu.Lock()
		rec.seen = append(rec.seen, seen{Method: r.Method, Path: r.URL.Path, Query: r.URL.RawQuery, Host: r.Host, Header: r.Header.Clone(), Body: body})
		answer := rec.answer
		rec.mu.Unlock()
		if answer != nil {
			answer(w, r, body)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	srv.Config.Protocols = h2cProtocols()
	srv.Start()
	t.Cleanup(srv.Close)
	rec.srv = srv
	return rec
}

func (r *recorder) setAnswer(answer func(w http.ResponseWriter, r *http.Request, body []byte)) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.answer = answer
}

func (r *recorder) requests() []seen {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]seen(nil), r.seen...)
}

// gateway is one route of the forwarder in front of a backend, with its queue and copier towards a copy endpoint.
type gateway struct {
	url    string
	queue  *Queue
	stats  *Stats
	logs   *syncBuffer
	copier *Copier
}

type gatewayOptions struct {
	queueBytes int64
	noCopier   bool
	keyFile    string
}

func startGateway(t *testing.T, rt Route, copyTo *recorder, o gatewayOptions) *gateway {
	t.Helper()
	if o.queueBytes == 0 {
		o.queueBytes = DefaultQueueBytes
	}
	logs := &syncBuffer{}
	log := slog.New(slog.NewJSONHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	stats := NewStats()
	queue := NewQueue(o.queueBytes, stats)
	h, err := NewHandler(rt, Options{Queue: queue, Stats: stats, Log: log})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(h, log)
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	g := &gateway{url: "http://" + ln.Addr().String(), queue: queue, stats: stats, logs: logs}
	if copyTo != nil && !o.noCopier {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		g.copier = &Copier{
			Endpoint:  copyTo.srv.URL,
			KeyFile:   o.keyFile,
			Queue:     queue,
			Stats:     stats,
			UserAgent: "impact-forward/test",
			Log:       log,
			Sleep: func(ctx context.Context, d time.Duration) error {
				return sleepFor(ctx, min(d, 20*time.Millisecond))
			},
		}
		go func() { defer close(done); _ = g.copier.Run(ctx) }()
		t.Cleanup(func() { cancel(); <-done })
	}
	return g
}

// eventually polls cond for up to five seconds.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// never checks that cond stays false for a while.
func never(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(300 * time.Millisecond)
	for time.Now().Before(deadline) {
		if cond() {
			t.Fatalf("unexpected: %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func gzipped(t *testing.T, b []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(b); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// otlpBatch is an OTLP export request with one resource (service.name checkout) and one span. The forwarder passes
// its bytes on unchanged; only the copy reads them, to stamp its provenance.
var otlpBatch = []byte("\x0aD\x0a\x1c\x0a\x1a\x0a\x0cservice.name\x12\x0a\x0a\x08checkout\x12$\x12\x22\x0a\x10trace-id-sixteen\x12\x08span-id!*\x04chat")

// otlpBatchCopy is what impact.ai gets of otlpBatch from a route whose source is "vendor": the same request, its
// resource stamped with impact.source.id and impact.source.route.
var otlpBatchCopy = []byte("\x0a\x81\x01\x0aY\x0a\x1a\x0a\x0cservice.name\x12\x0a\x0a\x08checkout\x0a\x1c\x0a\x10impact.source.id\x12\x08\x0a\x06vendor\x0a\x1d\x0a\x13impact.source.route\x12\x06\x0a\x04copy\x12$\x12\x22\x0a\x10trace-id-sixteen\x12\x08span-id!*\x04chat")

func exportRequest(t *testing.T, url string, body []byte, header http.Header) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range header {
		req.Header[k] = v
	}
	return req
}

func vendorHeaders() http.Header {
	return http.Header{
		"Content-Type":     {"application/x-protobuf"},
		"Content-Encoding": {"gzip"},
		"Authorization":    {"Api-Key " + plantedKey},
		"X-Vendor-Key":     {plantedKey},
		"User-Agent":       {"vendor-exporter/1.2.3"},
		"X-Forwarded-For":  {"203.0.113.7"},
	}
}

func do(t *testing.T, client *http.Client, req *http.Request) (*http.Response, []byte) {
	t.Helper()
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read answer: %v", err)
	}
	return resp, body
}

func TestOTLPHTTPReachesTheBackendAsADirectRunWouldWithTheWorkloadsOwnHeaders(t *testing.T) {
	backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
	})
	copyTo := newRecorder(t, nil)
	target := backend.srv.URL + "/otlp/v1/traces?token=" + plantedToken
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: target}, copyTo, gatewayOptions{})
	body := gzipped(t, otlpBatch)

	// A direct run, then the same export through the gateway, which the switch points at the gateway's own address.
	do(t, http.DefaultClient, exportRequest(t, target, body, vendorHeaders()))
	do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", body, vendorHeaders()))

	got := backend.requests()
	if len(got) != 2 {
		t.Fatalf("backend saw %d requests, want 2", len(got))
	}
	direct, gateway := got[0], got[1]
	if !bytes.Equal(direct.Body, gateway.Body) || !bytes.Equal(gateway.Body, body) {
		t.Errorf("the backend got other bytes through the gateway")
	}
	if direct.Path != gateway.Path || direct.Query != gateway.Query || direct.Method != gateway.Method || direct.Host != gateway.Host {
		t.Errorf("request line differs: direct %s %s?%s host %s, gateway %s %s?%s host %s",
			direct.Method, direct.Path, direct.Query, direct.Host, gateway.Method, gateway.Path, gateway.Query, gateway.Host)
	}
	if !equalHeaders(direct.Header, gateway.Header) {
		t.Errorf("headers differ:\ndirect  %v\ngateway %v", direct.Header, gateway.Header)
	}
}

func equalHeaders(a, b http.Header) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if strings.Join(v, "\x00") != strings.Join(b[k], "\x00") {
			return false
		}
	}
	return true
}

func TestTheExporterGetsTheBackendsOwnAnswerAndNothingIsCopiedUnlessItAccepts(t *testing.T) {
	cases := []struct {
		name   string
		status int
		header http.Header
		body   string
		copied bool
	}{
		{"accepted", http.StatusOK, http.Header{"Content-Type": {"application/json"}}, `{"partialSuccess":{}}`, true},
		{"accepted without content", http.StatusAccepted, nil, "", true},
		{"bad request", http.StatusBadRequest, http.Header{"Content-Type": {"text/plain"}, "X-Vendor-Error": {"bad-batch"}}, "batch refused", false},
		{"key refused", http.StatusForbidden, http.Header{"Www-Authenticate": {`Bearer realm="vendor"`}}, "no", false},
		{"too many", http.StatusTooManyRequests, http.Header{"Retry-After": {"7"}}, "slow down", false},
		{"down", http.StatusServiceUnavailable, http.Header{"Retry-After": {"3"}}, "maintenance", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
				for k, v := range tc.header {
					w.Header()[k] = v
				}
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			})
			copyTo := newRecorder(t, nil)
			g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, copyTo, gatewayOptions{})

			resp, answer := do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, http.Header{"Content-Type": {"application/x-protobuf"}}))
			if resp.StatusCode != tc.status || string(answer) != tc.body {
				t.Errorf("exporter got %d %q, want %d %q", resp.StatusCode, answer, tc.status, tc.body)
			}
			for k, v := range tc.header {
				if got := resp.Header[k]; strings.Join(got, ",") != strings.Join(v, ",") {
					t.Errorf("answer header %s = %v, want %v", k, got, v)
				}
			}
			if tc.copied {
				eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
				never(t, "a second copy", func() bool { return len(copyTo.requests()) > 1 })
				c := copyTo.requests()[0]
				if c.Path != "/v1/traces" || !bytes.Equal(c.Body, otlpBatchCopy) || c.Header.Get("Content-Type") != "application/x-protobuf" {
					t.Errorf("copy: %s %q %q", c.Path, c.Body, c.Header.Get("Content-Type"))
				}
				eventually(t, "the copy counted", func() bool { return g.stats.Snapshot()["checkout"].Copied == 1 })
			} else {
				never(t, "a copy of a batch the backend did not accept", func() bool { return len(copyTo.requests()) > 0 })
				if s := g.stats.Snapshot()["checkout"]; s.NotAccepted != 1 || s.Queued != 0 {
					t.Errorf("stats %+v, want one not accepted and nothing queued", s)
				}
			}
		})
	}
}

func TestWhenTheBackendCannotBeReachedTheExporterGetsBadGatewayAndNothingIsCopied(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed := "http://" + ln.Addr().String()
	_ = ln.Close()
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: closed + "/v1/traces?token=" + plantedToken}, copyTo, gatewayOptions{})

	resp, _ := do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, vendorHeaders()))
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status %d, want 502", resp.StatusCode)
	}
	never(t, "a copy", func() bool { return len(copyTo.requests()) > 0 })
	logs := g.logs.String()
	if !strings.Contains(logs, "backend") {
		t.Errorf("the failure was not logged: %s", logs)
	}
	assertNoSecret(t, "the gateway's logs", logs)
}

func assertNoSecret(t *testing.T, where, s string) {
	t.Helper()
	for _, secret := range []string{plantedKey, plantedToken} {
		if strings.Contains(s, secret) {
			t.Errorf("%s contain the planted %q", where, secret)
		}
	}
}

// grpcFrame wraps a message in the gRPC length prefix.
func grpcFrame(msg []byte, compressed bool) []byte {
	b := make([]byte, 5+len(msg))
	if compressed {
		b[0] = 1
	}
	binary.BigEndian.PutUint32(b[1:5], uint32(len(msg)))
	copy(b[5:], msg)
	return b
}

// grpcBackend answers a unary call with an empty message and the given status in the trailers.
func grpcBackend(t *testing.T, status string) *recorder {
	return newRecorder(t, func(w http.ResponseWriter, r *http.Request, _ []byte) {
		if r.ProtoMajor != 2 {
			http.Error(w, "gRPC needs HTTP/2", http.StatusHTTPVersionNotSupported)
			return
		}
		w.Header().Set("Content-Type", "application/grpc")
		w.Header().Set("Trailer", "Grpc-Status, Grpc-Message")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(grpcFrame(nil, false))
		w.Header().Set("Grpc-Status", status)
		if status != "0" {
			w.Header().Set("Grpc-Message", "vendor refused")
		}
	})
}

func h2cClient() *http.Client {
	p := new(http.Protocols)
	p.SetUnencryptedHTTP2(true)
	return &http.Client{Transport: &http.Transport{Protocols: p}}
}

func grpcExport(t *testing.T, url string, frames []byte, extra http.Header) *http.Request {
	req := exportRequest(t, url+"/opentelemetry.proto.collector.trace.v1.TraceService/Export", frames, http.Header{
		"Content-Type": {"application/grpc"},
		"Te":           {"trailers"},
		"X-Vendor-Key": {plantedKey},
	})
	for k, v := range extra {
		req.Header[k] = v
	}
	return req
}

func TestOTLPGRPCIsForwardedWithItsHeadersAndTrailersAndCopiedOnlyWhenTheStatusIsOK(t *testing.T) {
	for _, tc := range []struct {
		status string
		copied bool
	}{{"0", true}, {"8", false}, {"16", false}} {
		t.Run("grpc-status "+tc.status, func(t *testing.T) {
			backend := grpcBackend(t, tc.status)
			copyTo := newRecorder(t, nil)
			g := startGateway(t, Route{Name: "agent", Protocol: ProtocolOTLPGRPC, Source: "vendor", Target: backend.srv.URL}, copyTo, gatewayOptions{})
			frames := grpcFrame(otlpBatch, false)

			resp, _ := do(t, h2cClient(), grpcExport(t, g.url, frames, nil))
			if resp.StatusCode != http.StatusOK || resp.Trailer.Get("Grpc-Status") != tc.status {
				t.Fatalf("exporter got %d grpc-status %q, want 200 %q", resp.StatusCode, resp.Trailer.Get("Grpc-Status"), tc.status)
			}
			got := backend.requests()
			if len(got) != 1 || !bytes.Equal(got[0].Body, frames) || got[0].Header.Get("X-Vendor-Key") != plantedKey ||
				got[0].Header.Get("Te") != "trailers" || got[0].Path != "/opentelemetry.proto.collector.trace.v1.TraceService/Export" {
				t.Fatalf("backend saw %+v", got)
			}
			if tc.copied {
				eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
				c := copyTo.requests()[0]
				if !bytes.Equal(c.Body, otlpBatchCopy) || c.Header.Get("Content-Type") != "application/x-protobuf" || c.Header.Get("X-Vendor-Key") != "" {
					t.Errorf("copy: %q %v", c.Body, c.Header)
				}
			} else {
				never(t, "a copy", func() bool { return len(copyTo.requests()) > 0 })
			}
		})
	}
}

func TestACompressedGRPCMessageIsCopiedDecompressed(t *testing.T) {
	backend := grpcBackend(t, "0")
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "agent", Protocol: ProtocolOTLPGRPC, Source: "vendor", Target: backend.srv.URL}, copyTo, gatewayOptions{})
	frames := grpcFrame(gzipped(t, otlpBatch), true)

	do(t, h2cClient(), grpcExport(t, g.url, frames, http.Header{"Grpc-Encoding": {"gzip"}}))
	if got := backend.requests(); len(got) != 1 || !bytes.Equal(got[0].Body, frames) {
		t.Fatalf("the backend did not get the compressed frames unchanged")
	}
	eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
	if c := copyTo.requests()[0]; !bytes.Equal(c.Body, otlpBatchCopy) {
		t.Errorf("copy %q, want the decompressed message, stamped", c.Body)
	}
}

func TestAGRPCMessageInAnUnknownCompressionIsForwardedAndMarkedPartial(t *testing.T) {
	backend := grpcBackend(t, "0")
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "agent", Protocol: ProtocolOTLPGRPC, Source: "vendor", Target: backend.srv.URL}, copyTo, gatewayOptions{})

	resp, _ := do(t, h2cClient(), grpcExport(t, g.url, grpcFrame([]byte("snappy bytes"), true), http.Header{"Grpc-Encoding": {"snappy"}}))
	if resp.Trailer.Get("Grpc-Status") != "0" {
		t.Fatalf("the export failed: %v", resp.Trailer)
	}
	eventually(t, "partial counted", func() bool { return g.stats.Snapshot()["agent"].Partial == 1 })
	never(t, "a copy", func() bool { return len(copyTo.requests()) > 0 })
}

const zipkinSpans = `[{"traceId":"5af7183fb1d4cf5f","parentId":"6b221d5bc9e6496c","id":"352bff9a74ca9ad2","kind":"CLIENT",` +
	`"name":"chat gpt-4o","timestamp":1556604172355737,"duration":1431,"localEndpoint":{"serviceName":"assistant"},` +
	`"remoteEndpoint":{"serviceName":"model-provider","ipv4":"203.0.113.9","port":443},"tags":{"gen_ai.request.model":"gpt-4o"}}]`

func TestARelayPassesEveryPathAndAnswerByteForByteAndCopiesWhatItCanDecode(t *testing.T) {
	backend := newRecorder(t, func(w http.ResponseWriter, r *http.Request, _ []byte) {
		w.Header().Set("X-Vendor-Answer", r.URL.Path)
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `["assistant"]`)
	})
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "zipkin-app", Protocol: ProtocolRelay, Target: backend.srv.URL + "/?token=" + plantedToken, Decode: "zipkin"}, copyTo, gatewayOptions{})

	// Any path: a read of the vendor's API is relayed and answered as the vendor answers.
	read := exportRequest(t, g.url+"/api/v2/services?limit=5", nil, http.Header{"X-Vendor-Key": {plantedKey}})
	read.Method = http.MethodGet
	resp, answer := do(t, http.DefaultClient, read)
	if resp.StatusCode != http.StatusOK || string(answer) != `["assistant"]` || resp.Header.Get("X-Vendor-Answer") != "/api/v2/services" {
		t.Errorf("read: %d %q %v", resp.StatusCode, answer, resp.Header)
	}
	// The spans path: relayed unchanged, then decoded in the background into an OTLP copy.
	body := gzipped(t, []byte(zipkinSpans))
	resp, _ = do(t, http.DefaultClient, exportRequest(t, g.url+"/api/v2/spans", body, http.Header{
		"Content-Type": {"application/json"}, "Content-Encoding": {"gzip"}, "X-Vendor-Key": {plantedKey},
	}))
	if resp.StatusCode != http.StatusAccepted {
		t.Errorf("spans: %d", resp.StatusCode)
	}
	got := backend.requests()
	if len(got) != 2 {
		t.Fatalf("backend saw %d requests", len(got))
	}
	if got[0].Path != "/api/v2/services" || got[0].Query != "token="+plantedToken+"&limit=5" || got[0].Header.Get("X-Vendor-Key") != plantedKey {
		t.Errorf("read reached the backend as %s?%s", got[0].Path, got[0].Query)
	}
	if !bytes.Equal(got[1].Body, body) || got[1].Header.Get("Content-Encoding") != "gzip" {
		t.Errorf("spans reached the backend changed")
	}

	eventually(t, "one copy", func() bool { return len(copyTo.requests()) == 1 })
	c := copyTo.requests()[0]
	if c.Header.Get("Content-Type") != "application/json" || c.Path != "/v1/traces" {
		t.Errorf("copy: %s %v", c.Path, c.Header)
	}
	var doc struct {
		ResourceSpans []struct {
			ScopeSpans []struct {
				Spans []struct {
					TraceID string `json:"traceId"`
					Name    string `json:"name"`
				} `json:"spans"`
			} `json:"scopeSpans"`
		} `json:"resourceSpans"`
	}
	if err := json.Unmarshal(c.Body, &doc); err != nil || len(doc.ResourceSpans) != 1 ||
		doc.ResourceSpans[0].ScopeSpans[0].Spans[0].TraceID != "00000000000000005af7183fb1d4cf5f" {
		t.Errorf("copy is not the OTLP form of the spans: %v %s", err, c.Body)
	}
	eventually(t, "the copy counted", func() bool { return g.stats.Snapshot()["zipkin-app"].Copied == 1 })
	if s := g.stats.Snapshot()["zipkin-app"]; s.Partial != 0 {
		t.Errorf("stats %+v", s)
	}
}

func TestARelayedPayloadThatCannotBeReadIsStillRelayedAndMarkedPartial(t *testing.T) {
	for name, req := range map[string]struct {
		body        string
		contentType string
	}{
		"not JSON":       {"\x0a\x03not json", "application/json"},
		"protobuf form":  {"\x0a\x10zipkin proto3", "application/x-protobuf"},
		"no valid spans": {`[{"traceId":"zz","id":"1"}]`, "application/json"},
	} {
		t.Run(name, func(t *testing.T) {
			backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) { w.WriteHeader(http.StatusAccepted) })
			copyTo := newRecorder(t, nil)
			g := startGateway(t, Route{Name: "zipkin-app", Protocol: ProtocolRelay, Target: backend.srv.URL, Decode: "zipkin"}, copyTo, gatewayOptions{})

			resp, _ := do(t, http.DefaultClient, exportRequest(t, g.url+"/api/v2/spans", []byte(req.body), http.Header{"Content-Type": {req.contentType}}))
			if resp.StatusCode != http.StatusAccepted {
				t.Errorf("status %d", resp.StatusCode)
			}
			if got := backend.requests(); len(got) != 1 || string(got[0].Body) != req.body {
				t.Errorf("the payload was not relayed unchanged")
			}
			eventually(t, "partial counted", func() bool { return g.stats.Snapshot()["zipkin-app"].Partial == 1 })
			never(t, "a copy", func() bool { return len(copyTo.requests()) > 0 })
		})
	}
}

func TestARelayWithoutADecoderOnlyRelays(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, nil)
	g := startGateway(t, Route{Name: "vendor", Protocol: ProtocolRelay, Target: backend.srv.URL}, copyTo, gatewayOptions{})
	do(t, http.DefaultClient, exportRequest(t, g.url+"/intake", []byte("vendor bytes"), nil))
	if got := backend.requests(); len(got) != 1 || string(got[0].Body) != "vendor bytes" {
		t.Fatalf("not relayed")
	}
	never(t, "a copy or a partial", func() bool {
		s := g.stats.Snapshot()["vendor"]
		return len(copyTo.requests()) > 0 || s.Partial > 0 || s.Queued > 0
	})
	if s := g.stats.Snapshot()["vendor"]; s.Forwarded != 1 {
		t.Errorf("stats %+v", s)
	}
}

func TestTheForwardDoesNotWaitForTheCopy(t *testing.T) {
	backend := newRecorder(t, nil)
	release := make(chan struct{})
	copyTo := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		<-release // impact.ai hangs
	})
	t.Cleanup(func() { close(release) }) // runs before the server closes, which waits for its handlers
	g := startGateway(t, Route{Name: "checkout", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"}, copyTo, gatewayOptions{})

	for i := range 20 {
		start := time.Now()
		resp, _ := do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", otlpBatch, protobuf()))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("export %d: %d", i, resp.StatusCode)
		}
		if d := time.Since(start); d > time.Second {
			t.Fatalf("export %d took %v while impact.ai hangs", i, d)
		}
	}
	if got := len(backend.requests()); got != 20 {
		t.Errorf("backend got %d of 20", got)
	}
}

func TestABatchLargerThanTheCopyLimitIsForwardedAndMarkedPartial(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, nil)
	stats := NewStats()
	queue := NewQueue(DefaultQueueBytes, stats)
	h, err := NewHandler(Route{Name: "big", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces"},
		Options{Queue: queue, Stats: stats, MaxBatchBytes: 1024})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	big := bytes.Repeat([]byte("x"), 4096)
	do(t, http.DefaultClient, exportRequest(t, srv.URL+"/v1/traces", big, http.Header{"Content-Type": {"application/x-protobuf"}}))
	if got := backend.requests(); len(got) != 1 || !bytes.Equal(got[0].Body, big) {
		t.Fatalf("the large batch was not forwarded whole")
	}
	eventually(t, "partial counted", func() bool { return stats.Snapshot()["big"].Partial == 1 })
	if queue.Len() != 0 {
		t.Errorf("queued a batch it could not hold")
	}
	_ = copyTo
}
