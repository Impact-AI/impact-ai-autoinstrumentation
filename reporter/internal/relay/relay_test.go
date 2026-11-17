// SPDX-License-Identifier: Apache-2.0

package relay

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
)

// planted is the fake key the reporter's tests plant to prove that no value is ever passed on.
const planted = "sk-live-abcdefghijklmnop"

// spy is a decoder that keeps the request it was given.
type spy struct {
	headers []string
	seen    *Request
	traces  otlp.Traces
	err     error
	panics  bool
}

func (s *spy) Source() string    { return "example" }
func (s *spy) Name() string      { return "example-spy/1.0.0" }
func (s *spy) Headers() []string { return s.headers }
func (s *spy) Match(method, path, _ string) bool {
	return method == http.MethodPut && path == "/v0.4/traces"
}
func (s *spy) Decode(r Request) (otlp.Traces, error) {
	s.seen = &r
	if s.panics {
		var spans []otlp.Span
		_ = spans[3]
	}
	return s.traces, s.err
}

func TestAPlantedKeyInAHeaderOrTheQueryNeverReachesDecode(t *testing.T) {
	query := "license_key=" + planted + "&method=span_event_data&api_key=" + planted + "&dd-api-key=" + planted +
		"&protocol_version=17&access_token=" + planted + ";X-Amz-Security-Token=" + planted +
		"&license%5Fkey=" + planted + "&%zz=" + planted + "&marshal_format=json"
	r := httptest.NewRequest(http.MethodPut, "/v0.4/traces?"+query, nil)
	r.Header.Set("DD-API-KEY", planted)
	r.Header.Set("X-License-Key", planted)
	r.Header.Set("Api-Key", planted)
	r.Header.Set("Authorization", "Bearer "+planted)
	r.Header.Set("Proxy-Authorization", "Basic "+planted)
	r.Header.Set("Cookie", "session="+planted)
	r.Header.Set("X-Amz-Security-Token", planted)
	r.Header.Set("Datadog-Meta-Lang", "python")
	r.Header.Add("X-Datadog-Trace-Count", "1")
	r.Header.Add("X-Datadog-Trace-Count", "2")
	r.Header.Set("X-Undeclared", "kept out")
	r.Header.Set("Content-Type", "application/msgpack")

	// The decoder even declares two of the credential headers: they are still removed, and Validate refuses it.
	d := &spy{headers: []string{"datadog-meta-lang", "X-Datadog-Trace-Count", "DD-API-KEY", "Authorization"}}
	if err := Validate(d); err == nil || !strings.Contains(err.Error(), "DD-API-KEY") {
		t.Errorf("Validate let the credential header through: %v", err)
	}
	req, err := NewRequest(r, []byte("payload"), d)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Decode(d, req); err != nil {
		t.Fatal(err)
	}
	if d.seen == nil {
		t.Fatal("Decode was not called")
	}
	if seen := fmt.Sprintf("%#v", *d.seen); strings.Contains(seen, planted) || strings.Contains(seen, "kept out") {
		t.Fatalf("the decoder saw a credential or an undeclared header: %s", seen)
	}
	if d.seen.RawQuery != "method=span_event_data&protocol_version=17&marshal_format=json" {
		t.Errorf("query %q", d.seen.RawQuery)
	}
	want := http.Header{"Datadog-Meta-Lang": {"python"}, "X-Datadog-Trace-Count": {"1", "2"}}
	if fmt.Sprint(d.seen.Header) != fmt.Sprint(want) {
		t.Errorf("headers %v, want %v", d.seen.Header, want)
	}
	if d.seen.Method != http.MethodPut || d.seen.Path != "/v0.4/traces" || d.seen.ContentType != "application/msgpack" {
		t.Errorf("request %s %s %s", d.seen.Method, d.seen.Path, d.seen.ContentType)
	}
	// The request the relay forwards keeps everything.
	if r.Header.Get("DD-API-KEY") != planted || !strings.Contains(r.URL.RawQuery, "license_key="+planted) {
		t.Error("making the copy changed the forwarded request")
	}
}

func TestCredentialNames(t *testing.T) {
	for _, name := range []string{
		"DD-API-KEY", "dd-api-key", "X-License-Key", "Api-Key", "X-Insert-Key", "license_key", "api_key", "apikey",
		"Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-Amz-Security-Token", "access_token",
		"client_secret", "password", "X-Amz-Signature", "X-Amz-Credential", "X-Session-Id", "jwt",
	} {
		if !CredentialName(name) {
			t.Errorf("%s does not look like a credential", name)
		}
	}
	for _, name := range []string{
		"Content-Type", "Content-Encoding", "User-Agent", "Datadog-Meta-Lang", "Datadog-Meta-Tracer-Version",
		"X-Datadog-Trace-Count", "Datadog-Container-Id", "method", "protocol_version", "marshal_format", "run_id",
	} {
		if CredentialName(name) {
			t.Errorf("%s looks like a credential", name)
		}
	}
}

func compressed(t *testing.T, coding string, data []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	var w interface {
		Write([]byte) (int, error)
		Close() error
	}
	switch coding {
	case "gzip":
		w = gzip.NewWriter(&buf)
	case "zlib":
		w = zlib.NewWriter(&buf)
	case "flate":
		fw, err := flate.NewWriter(&buf, flate.DefaultCompression)
		if err != nil {
			t.Fatal(err)
		}
		w = fw
	}
	if _, err := w.Write(data); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func copyOf(t *testing.T, encoding string, body []byte, limits Limits) (Request, error) {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, "/v1/spans", nil)
	if encoding != "" {
		r.Header.Set("Content-Encoding", encoding)
	}
	return limits.NewRequest(r, body, &spy{})
}

func TestBodiesAreDecompressedFromGzipAndDeflate(t *testing.T) {
	payload := []byte(`{"spans":[{"name":"chat"}]}`)
	for _, c := range []struct {
		encoding string
		body     []byte
	}{
		{"", payload},
		{"identity", payload},
		{"gzip", compressed(t, "gzip", payload)},
		{"GZIP", compressed(t, "gzip", payload)},
		{"x-gzip", compressed(t, "gzip", payload)},
		{"deflate", compressed(t, "zlib", payload)},
		{"deflate", compressed(t, "flate", payload)},
		{"deflate, gzip", compressed(t, "gzip", compressed(t, "zlib", payload))},
	} {
		req, err := copyOf(t, c.encoding, c.body, Limits{})
		if err != nil {
			t.Errorf("%q: %v", c.encoding, err)
			continue
		}
		if !bytes.Equal(req.Body, payload) {
			t.Errorf("%q: body %q", c.encoding, req.Body)
		}
	}
}

func TestTheBodyIsACopy(t *testing.T) {
	body := []byte("original")
	req, err := copyOf(t, "", body, Limits{})
	if err != nil {
		t.Fatal(err)
	}
	req.Body[0] = 'X'
	if string(body) != "original" {
		t.Fatalf("the decoder's copy shares the forwarded body: %q", body)
	}
	if req, err := copyOf(t, "", body[:0], Limits{}); err != nil || req.Body != nil {
		t.Fatalf("an empty body: %q %v", req.Body, err)
	}
}

func TestPayloadsThatCannotBeReadArePayloadErrors(t *testing.T) {
	bomb := compressed(t, "gzip", make([]byte, 1<<20))
	for _, c := range []struct {
		name     string
		encoding string
		body     []byte
		limits   Limits
	}{
		{"too large", "", []byte("0123456789"), Limits{Body: 9}},
		{"too large once decompressed", "gzip", bomb, Limits{Decoded: 1000}},
		{"brotli", "br", []byte("x"), Limits{}},
		{"an encoding with a strange name", "x-" + strings.Repeat("y", 40), []byte("x"), Limits{}},
		{"broken gzip", "gzip", []byte("not gzip"), Limits{}},
		{"truncated gzip", "gzip", compressed(t, "gzip", []byte("abc"))[:12], Limits{}},
	} {
		_, err := copyOf(t, c.encoding, c.body, c.limits)
		var payload *PayloadError
		if !errors.As(err, &payload) {
			t.Errorf("%s: %v is not a PayloadError", c.name, err)
			continue
		}
		if strings.Contains(err.Error(), "yyyy") {
			t.Errorf("%s: the error quotes the request: %v", c.name, err)
		}
	}
}

func TestTheContentTypeIsItsMediaType(t *testing.T) {
	for value, want := range map[string]string{
		"Application/MsgPack":              "application/msgpack",
		"application/json; charset=utf-8":  "application/json",
		"application/x-protobuf;;broken=;": "application/x-protobuf",
		"":                                 "",
	} {
		r := httptest.NewRequest(http.MethodPost, "/", nil)
		r.Header.Set("Content-Type", value)
		req, err := NewRequest(r, nil, &spy{})
		if err != nil || req.ContentType != want {
			t.Errorf("%q: %q %v", value, req.ContentType, err)
		}
	}
}

func TestValidateChecksWhatADecoderDeclares(t *testing.T) {
	if err := Validate(&spy{headers: []string{"Datadog-Meta-Lang"}}); err != nil {
		t.Errorf("a good decoder: %v", err)
	}
	for _, d := range []Decoder{
		named{source: "Data Dog", name: "datadog/1"},
		named{source: "datadog", name: "datadog"},
		named{source: "datadog", name: "datadog/"},
		named{source: "datadog", name: "datadog/1", headers: []string{""}},
		named{source: "datadog", name: "datadog/1", headers: []string{"X-License-Key"}},
	} {
		if err := Validate(d); err == nil {
			t.Errorf("%+v passed", d)
		}
	}
}

type named struct {
	source, name string
	headers      []string
}

func (n named) Source() string                      { return n.source }
func (n named) Name() string                        { return n.name }
func (n named) Headers() []string                   { return n.headers }
func (n named) Match(string, string, string) bool   { return false }
func (n named) Decode(Request) (otlp.Traces, error) { return otlp.Traces{}, nil }

func TestDecodeSetsTheProvenanceOfTheRelay(t *testing.T) {
	d := &spy{traces: otlp.Traces{ResourceSpans: []otlp.ResourceSpans{{
		Resource: otlp.Resource{Attributes: otlp.Attributes{otlp.Str("service.name", "checkout")}},
	}}}}
	traces, err := Decode(d, Request{})
	if err != nil {
		t.Fatal(err)
	}
	var keys []string
	for _, kv := range traces.ResourceSpans[0].Resource.Attributes {
		keys = append(keys, kv.Key+"="+*kv.Value.StringValue)
	}
	slices.Sort(keys)
	want := []string{
		"impact.source.decoder=example-spy/1.0.0", "impact.source.id=example", "impact.source.route=relay",
		"service.name=checkout",
	}
	if !slices.Equal(keys, want) {
		t.Errorf("resource %v, want %v", keys, want)
	}
}

func TestDecodeErrorsAndPanicsAreErrors(t *testing.T) {
	failure := errors.New("decoder failed")
	if _, err := Decode(&spy{err: failure}, Request{}); !errors.Is(err, failure) {
		t.Errorf("decoder error was not preserved: %v", err)
	}
	unreadable := &spy{err: Unreadable("not msgpack", errors.New("unexpected byte"))}
	if _, err := Decode(unreadable, Request{}); err == nil || !strings.Contains(err.Error(), "not msgpack") {
		t.Errorf("unreadable: %v", err)
	}
	traces, err := Decode(&spy{panics: true, traces: otlp.Traces{ResourceSpans: make([]otlp.ResourceSpans, 1)}}, Request{})
	if err == nil || !strings.Contains(err.Error(), "panicked") || len(traces.ResourceSpans) != 0 {
		t.Errorf("a panic: %v %v", traces, err)
	}
}

// Match sees the query a protocol names its calls in, never a credential in it.
func TestCleanQueryKeepsTheCallAndDropsCredentials(t *testing.T) {
	got := CleanQuery("method=span_event_data&license_key=sk-live-abcdefghijklmnop&protocol_version=17&run_id=7")
	if got != "method=span_event_data&protocol_version=17&run_id=7" {
		t.Fatalf("CleanQuery = %q", got)
	}
}
