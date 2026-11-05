// SPDX-License-Identifier: Apache-2.0

// Package kubetest is a fake Kubernetes API server for tests. It serves fixed
// JSON, pages lists with continue tokens the way the real API server does, and
// records what was asked.
package kubetest

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// Request is one request the server received.
type Request struct {
	Method      string
	Path        string
	Query       string
	AuthHead    string
	ContentType string
	Body        []byte
	At          time.Time
}

// Response is one scripted answer.
type Response struct {
	Status int // zero means 200
	Body   string
	Header map[string]string
}

// sequence answers successive requests with successive responses and repeats
// the last one.
type sequence struct {
	steps []Response
	next  int
}

func (q *sequence) take() Response {
	r := q.steps[min(q.next, len(q.steps)-1)]
	q.next++
	return r
}

type route struct {
	status int
	body   []byte
	items  []json.RawMessage
	page   int
	isList bool
}

// Server is a fake API server.
type Server struct {
	*httptest.Server
	mu        sync.Mutex
	routes    map[string]route
	sequences map[string]*sequence // "METHOD /path" -> scripted answers
	requests  []Request
}

func newServer() *Server {
	return &Server{routes: map[string]route{}, sequences: map[string]*sequence{}}
}

// New starts a plain HTTP fake API server that is closed when the test ends.
func New(t testing.TB) *Server {
	t.Helper()
	s := newServer()
	s.Server = httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(s.Close)
	return s
}

// NewTLS starts a fake API server that speaks HTTPS with httptest's certificate.
func NewTLS(t testing.TB) *Server {
	t.Helper()
	s := newServer()
	s.Server = httptest.NewTLSServer(http.HandlerFunc(s.serve))
	t.Cleanup(s.Close)
	return s
}

func toRaw(v any) []byte {
	switch x := v.(type) {
	case string:
		return []byte(x)
	case []byte:
		return x
	case json.RawMessage:
		return x
	}
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

// Handle serves body (a JSON string, bytes or any value to marshal) at path.
func (s *Server) Handle(path string, body any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.routes[path] = route{status: http.StatusOK, body: toRaw(body)}
}

// HandleStatus serves an error status at path.
func (s *Server) HandleStatus(path string, status int, body string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.routes[path] = route{status: status, body: []byte(body)}
}

// HandleList serves items as a list at path. When pageSize is positive the
// server returns at most that many items per page and a continue token, even
// if the client asked for more, as a real API server may.
func (s *Server) HandleList(path string, items []any, pageSize int) {
	raw := make([]json.RawMessage, len(items))
	for i, item := range items {
		raw[i] = toRaw(item)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.routes[path] = route{status: http.StatusOK, items: raw, page: pageSize, isList: true}
}

// HandleSequence scripts the answers to one method and path: the first request
// gets the first response, the second the second, and so on; the last response
// repeats. A scripted route takes precedence over Handle for that method.
func (s *Server) HandleSequence(method, path string, steps ...Response) {
	if len(steps) == 0 {
		panic("kubetest: HandleSequence needs at least one response")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sequences[method+" "+path] = &sequence{steps: steps}
}

// Requests returns a copy of the recorded requests, in arrival order.
func (s *Server) Requests() []Request {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Request(nil), s.requests...)
}

// RequestsFor returns the recorded requests for one path.
func (s *Server) RequestsFor(path string) []Request {
	var out []Request
	for _, r := range s.Requests() {
		if r.Path == path {
			out = append(out, r)
		}
	}
	return out
}

func (s *Server) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	s.mu.Lock()
	s.requests = append(s.requests, Request{
		Method: r.Method, Path: r.URL.Path, Query: r.URL.RawQuery, AuthHead: r.Header.Get("Authorization"),
		ContentType: r.Header.Get("Content-Type"), Body: body, At: time.Now(),
	})
	if seq, ok := s.sequences[r.Method+" "+r.URL.Path]; ok {
		resp := seq.take()
		s.mu.Unlock()
		for k, v := range resp.Header {
			w.Header().Set(k, v)
		}
		w.Header().Set("Content-Type", "application/json")
		status := resp.Status
		if status == 0 {
			status = http.StatusOK
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, resp.Body)
		return
	}
	rt, ok := s.routes[r.URL.Path]
	s.mu.Unlock()
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		_, _ = fmt.Fprintf(w, `{"kind":"Status","code":404,"message":"no route %s"}`, r.URL.Path)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if !rt.isList {
		w.WriteHeader(rt.status)
		_, _ = w.Write(rt.body)
		return
	}
	limit := len(rt.items)
	if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && v > 0 && v < limit {
		limit = v
	}
	if rt.page > 0 && rt.page < limit {
		limit = rt.page
	}
	start := 0
	if tok := r.URL.Query().Get("continue"); tok != "" {
		n, err := strconv.Atoi(strings.TrimPrefix(tok, "tok-"))
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		start = n
	}
	end := min(start+limit, len(rt.items))
	next := ""
	if end < len(rt.items) {
		next = "tok-" + strconv.Itoa(end)
	}
	page := struct {
		Metadata struct {
			Continue string `json:"continue,omitempty"`
		} `json:"metadata"`
		Items []json.RawMessage `json:"items"`
	}{Items: rt.items[start:end]}
	page.Metadata.Continue = next
	_ = json.NewEncoder(w).Encode(page)
}
