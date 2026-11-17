// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
)

// DefaultMaxBatchBytes is the largest request body the gateway keeps for a copy. A larger one is forwarded whole and
// counted as partial.
const DefaultMaxBatchBytes = 16 << 20

// bodyWait bounds how long a copy decision waits for the transport to finish reading a request body after the backend
// answered.
const bodyWait = 5 * time.Second

// Options are what the handlers of one gateway share.
type Options struct {
	Queue *Queue
	Stats *Stats
	// Transport sends to the backends. Nil means one made for the route's protocol (newTransport).
	Transport http.RoundTripper
	// MaxBatchBytes is the largest body kept for a copy; zero means DefaultMaxBatchBytes.
	MaxBatchBytes int64
	// Captures bounds the bodies all handlers that share it hold for a copy while they forward. Nil means one of
	// DefaultCaptureBytes for this handler alone.
	Captures *CaptureBudget
	Log      *slog.Logger
}

type handler struct {
	route    Route
	target   *url.URL
	proxy    *httputil.ReverseProxy
	queue    *Queue
	counts   *counters
	maxBatch int64
	captures *CaptureBudget
	log      *slog.Logger
}

// NewHandler returns the handler of one route: it forwards every request to the route's target and queues a copy of
// what the backend accepted.
func NewHandler(rt Route, o Options) (http.Handler, error) {
	if err := rt.checkForward(); err != nil {
		return nil, err
	}
	if o.Queue == nil || o.Stats == nil {
		return nil, errors.New("forward: a handler needs a queue and stats")
	}
	h := &handler{
		route:    rt,
		target:   rt.target,
		queue:    o.Queue,
		counts:   o.Stats.route(rt.Name),
		maxBatch: o.MaxBatchBytes,
		captures: o.Captures,
		log:      o.Log,
	}
	if h.captures == nil {
		h.captures = NewCaptureBudget(DefaultCaptureBytes)
	}
	if h.maxBatch <= 0 {
		h.maxBatch = DefaultMaxBatchBytes
	}
	if h.log == nil {
		h.log = slog.New(slog.DiscardHandler)
	}
	transport := o.Transport
	if transport == nil {
		transport = newTransport(rt.Protocol)
	}
	h.proxy = &httputil.ReverseProxy{
		Rewrite:      h.rewrite,
		Transport:    transport,
		ErrorHandler: h.unreachable,
		// The proxy's own log can name the outgoing request; everything worth saying is said by ErrorHandler.
		ErrorLog:      log.New(io.Discard, "", 0),
		FlushInterval: -1,
	}
	return h, nil
}

// newTransport sends to a vendor's backend: through the gateway's proxy settings, with HTTP/2 where the backend
// offers it, and for gRPC HTTP/2 only (over TLS, or in clear text with prior knowledge for an http:// target).
func newTransport(p Protocol) *http.Transport {
	t := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          256,
		MaxIdleConnsPerHost:   64,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
		// The exporter's own timeout ends a request: the gateway adds none of its own.
		DisableCompression: true,
	}
	if p == ProtocolOTLPGRPC {
		protocols := new(http.Protocols)
		protocols.SetHTTP2(true)
		protocols.SetUnencryptedHTTP2(true)
		t.Protocols = protocols
	}
	return t
}

// NewServer serves a route's handler in clear text over HTTP/1.1 and HTTP/2 (prior knowledge, which gRPC exporters
// use). It sets no read or write timeout, because the exporter's own timeout governs a request.
func NewServer(h http.Handler, l *slog.Logger) *http.Server {
	if l == nil {
		l = slog.New(slog.DiscardHandler)
	}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	return &http.Server{
		Handler:           h,
		Protocols:         protocols,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       2 * time.Minute,
		ErrorLog:          slog.NewLogLogger(l.Handler(), slog.LevelDebug),
	}
}

// forwardedHeaders are removed from the outgoing request by httputil.ReverseProxy when it rewrites; the workload sent
// them, so they go on as they came.
var forwardedHeaders = []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"}

func (h *handler) rewrite(pr *httputil.ProxyRequest) {
	if h.route.Protocol == ProtocolOTLPHTTP && !h.route.Base {
		// The target is the workload's traces URL, as it was before the switch.
		u := *h.target
		pr.Out.URL = &u
		pr.Out.Host = ""
	} else {
		pr.SetURL(h.target)
		// httputil.ReverseProxy re-encodes a query it cannot parse (one with ';' or a bad '%' escape) before Rewrite;
		// the workload's query goes on byte for byte, after the target's, as SetURL joins them.
		pr.Out.URL.RawQuery = joinQuery(h.target.RawQuery, pr.In.URL.RawQuery)
	}
	for _, k := range forwardedHeaders {
		if v, ok := pr.In.Header[k]; ok {
			pr.Out.Header[k] = v
		}
	}
}

func joinQuery(target, in string) string {
	if target == "" || in == "" {
		return target + in
	}
	return target + "&" + in
}

// unreachable answers when the backend gave no answer. The error is logged without the request's URL, which can hold
// a token.
func (h *handler) unreachable(w http.ResponseWriter, r *http.Request, err error) {
	h.counts.unreachable.Add(1)
	if sw, ok := w.(*statusWriter); ok {
		sw.noAnswer = true
	}
	level := slog.LevelWarn
	if errors.Is(err, context.Canceled) {
		level = slog.LevelDebug // the exporter gave up
	}
	h.log.Log(r.Context(), level, "the backend gave no answer; the exporter gets 502", "route", h.route.Name,
		"backend", h.target.Host, "error", redact(err))
	w.WriteHeader(http.StatusBadGateway)
}

// redact returns an error's text without the URL that net/http puts in it.
func redact(err error) string {
	var u *url.Error
	if errors.As(err, &u) {
		return u.Op + ": " + u.Err.Error()
	}
	return err.Error()
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Only a request that can become a copy is kept while it is forwarded, and only while the capture budget allows.
	kind, copyable := h.copyKind(r)
	var body *capture
	if copyable && r.Body != nil && r.Body != http.NoBody && r.ContentLength != 0 {
		body = &capture{rc: r.Body, max: h.maxBatch, budget: h.captures, closed: make(chan struct{})}
		r.Body = body
	}
	handedOver := false
	defer func() {
		if body != nil && !handedOver {
			body.discard()
		}
	}()
	sw := &statusWriter{ResponseWriter: w}
	h.proxy.ServeHTTP(sw, r)
	if sw.status == 0 || sw.noAnswer {
		return
	}
	h.counts.forwarded.Add(1)
	h.log.Debug("forwarded", "route", h.route.Name, "backend", h.target.Host, "method", r.Method, "status", sw.status)
	if !h.accepted(sw) {
		h.counts.notAccepted.Add(1)
		return
	}
	if body == nil {
		return
	}
	contentType, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	encoding := r.Header.Get("Content-Encoding")
	if kind == KindGRPC {
		encoding = r.Header.Get("Grpc-Encoding")
	}
	var call *RelayCall
	if kind == KindRelay {
		call = relayCall(h.route.decoder, r)
	}
	length := r.ContentLength
	handedOver = true
	// The transport may still be reading the last bytes of the body when the backend's answer is complete; the copy is
	// decided once it closed the body, without holding up the exporter.
	go func() {
		select {
		case <-body.closed:
		case <-time.After(bodyWait):
		}
		data, complete := body.result(length)
		if !complete {
			h.counts.partial.Add(1)
			h.log.Debug("a forwarded batch was not kept for a copy", "route", h.route.Name, "reason", "larger than the copy limit, over the capture budget or not read whole")
			return
		}
		h.queue.Push(Item{Route: h.route.Name, Source: h.route.Source, Kind: kind, Body: data, ContentType: contentType, Encoding: encoding, Relay: call})
	}()
}

// accepted tells whether the backend accepted the batch: a 2xx, and over gRPC also grpc-status 0 (in the headers of a
// trailers-only answer, or in the trailers).
func (h *handler) accepted(sw *statusWriter) bool {
	if sw.status < 200 || sw.status > 299 {
		return false
	}
	if h.route.Protocol != ProtocolOTLPGRPC {
		return true
	}
	header := sw.Header()
	status := header.Get("Grpc-Status")
	if v := header[http.TrailerPrefix+"Grpc-Status"]; len(v) > 0 {
		status = v[0]
	}
	return status == "0"
}

// copyKind says what a request becomes once the backend accepted it, from the route, method, path and query alone:
// OTLP trace exports are copied (on a base URL only the POSTs to a traces path); a relay copies what its source's
// decoder claims (Match) and only relays the rest.
func (h *handler) copyKind(r *http.Request) (Kind, bool) {
	switch h.route.Protocol {
	case ProtocolOTLPHTTP:
		return KindOTLP, r.Method == http.MethodPost && (!h.route.Base || strings.HasSuffix(r.URL.Path, TracesPath))
	case ProtocolOTLPGRPC:
		return KindGRPC, strings.HasSuffix(r.URL.Path, "/opentelemetry.proto.collector.trace.v1.TraceService/Export")
	}
	if d := h.route.decoder; d != nil && d.Match(r.Method, r.URL.Path, relay.CleanQuery(r.URL.RawQuery)) {
		return KindRelay, true
	}
	return 0, false
}

// relayCall keeps what the decoder reads of a request, taken before the handler returns: never a header or a query
// parameter whose name looks like a credential.
func relayCall(d relay.Decoder, r *http.Request) *RelayCall {
	header := http.Header{}
	for _, name := range d.Headers() {
		key := http.CanonicalHeaderKey(name)
		if relay.CredentialName(key) {
			continue
		}
		if values := r.Header.Values(key); len(values) > 0 {
			header[key] = slices.Clone(values)
		}
	}
	return &RelayCall{Source: d.Source(), Method: r.Method, Path: r.URL.Path, Query: relay.CleanQuery(r.URL.RawQuery), Header: header}
}

// statusWriter remembers the status the proxy wrote.
type statusWriter struct {
	http.ResponseWriter
	status   int
	noAnswer bool // the backend gave no answer: the 502 is the gateway's own
}

func (s *statusWriter) WriteHeader(code int) {
	if s.status == 0 && code >= 200 {
		s.status = code
	}
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusWriter) Write(p []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	return s.ResponseWriter.Write(p)
}

func (s *statusWriter) Unwrap() http.ResponseWriter { return s.ResponseWriter }

// capture passes a request body through and keeps a copy of it, up to max bytes and as far as its budget allows.
type capture struct {
	rc     io.ReadCloser
	max    int64
	budget *CaptureBudget

	mu       sync.Mutex
	buf      bytes.Buffer
	reserved int64
	over     bool // not kept: larger than max or than the budget allowed
	taken    bool // result or discard was called: nothing more is kept
	eof      bool
	once     sync.Once
	closed   chan struct{}
}

func (c *capture) Read(p []byte) (int, error) {
	n, err := c.rc.Read(p)
	c.mu.Lock()
	if n > 0 && !c.over && !c.taken {
		if int64(c.buf.Len()+n) > c.max || !c.budget.reserve(int64(n)) {
			c.drop()
		} else {
			c.reserved += int64(n)
			c.buf.Write(p[:n])
		}
	}
	if errors.Is(err, io.EOF) {
		c.eof = true
	}
	c.mu.Unlock()
	return n, err
}

// drop gives up the copy and what it held; c.mu is held.
func (c *capture) drop() {
	c.over = true
	c.buf = bytes.Buffer{}
	c.budget.release(c.reserved)
	c.reserved = 0
}

func (c *capture) Close() error {
	err := c.rc.Close()
	c.once.Do(func() { close(c.closed) })
	return err
}

// result returns the kept body and whether it is the whole body: read to its end, or to its announced length. The
// body is handed over (the queue bounds it from now on) and leaves the capture budget.
func (c *capture) result(length int64) ([]byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.taken = true
	if c.over {
		return nil, false
	}
	complete := c.eof || (length > 0 && int64(c.buf.Len()) == length)
	data := c.buf.Bytes()
	c.buf = bytes.Buffer{}
	c.budget.release(c.reserved)
	c.reserved = 0
	if !complete {
		return nil, false
	}
	return data, true
}

// discard gives the kept body up when it will not be copied.
func (c *capture) discard() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.taken = true
	c.drop()
}
