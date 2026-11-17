// SPDX-License-Identifier: Apache-2.0

// Package relaytest checks a decoder against recorded requests and a golden
// OTLP/JSON file.
//
// A recording is a JSON file:
//
//	{
//	  "how": "what ran, against what, and how the requests were captured",
//	  "packages": {"<package>": "<pinned version>"},
//	  "recorded": "YYYY-MM-DD",
//	  "requests": [{
//	    "method": "PUT", "path": "/v0.4/traces", "query": "",
//	    "headers": {"<name>": "<value>"},
//	    "contentType": "application/msgpack", "contentEncoding": "",
//	    "bodyBase64": "..."
//	  }]
//	}
//
// testing/mock/recording.py writes one from what the kit's recording mock
// captured. Golden decodes the requests the decoder matches the way the relay
// does (relay.NewRequest, then relay.Decode), puts all their resource spans into
// one OTLP/JSON document and compares it with the golden file as canonical JSON.
// UPDATE_GOLDEN=1 rewrites the golden file instead.
package relaytest

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// Recording is a file of recorded requests.
type Recording struct {
	// How says what ran (an open-source tracer or SDK at a pinned version, or a
	// page of public documentation, linked) and how the requests were captured.
	How string `json:"how"`
	// Packages are the pinned versions of what made the requests.
	Packages map[string]string `json:"packages"`
	// Recorded is the date of the recording, YYYY-MM-DD.
	Recorded string            `json:"recorded"`
	Requests []RecordedRequest `json:"requests"`
}

// RecordedRequest is one request as it arrived.
type RecordedRequest struct {
	Method          string            `json:"method"`
	Path            string            `json:"path"`
	Query           string            `json:"query"`
	Headers         map[string]string `json:"headers"`
	ContentType     string            `json:"contentType"`
	ContentEncoding string            `json:"contentEncoding"`
	BodyBase64      string            `json:"bodyBase64"`
}

var date = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)

// ReadRecording reads a recording and checks that it says how, with what and
// when it was made.
func ReadRecording(path string) (Recording, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Recording{}, err
	}
	var rec Recording
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&rec); err != nil {
		return Recording{}, fmt.Errorf("%s: %w", path, err)
	}
	switch {
	case strings.TrimSpace(rec.How) == "":
		return Recording{}, fmt.Errorf("%s: says nothing in \"how\"", path)
	case rec.Packages == nil:
		return Recording{}, fmt.Errorf("%s: has no \"packages\" (an empty object for a fixture built from documentation)", path)
	case !date.MatchString(rec.Recorded):
		return Recording{}, fmt.Errorf("%s: \"recorded\" is %q, not YYYY-MM-DD", path, rec.Recorded)
	case len(rec.Requests) == 0:
		return Recording{}, fmt.Errorf("%s: has no requests", path)
	}
	return rec, nil
}

// HTTPRequest is the request as the relay would have received it, and its body.
// contentType and contentEncoding are set as headers when they are not empty.
func (r RecordedRequest) HTTPRequest() (*http.Request, []byte, error) {
	body, err := base64.StdEncoding.DecodeString(r.BodyBase64)
	if err != nil {
		return nil, nil, fmt.Errorf("bodyBase64: %w", err)
	}
	header := http.Header{}
	for name, value := range r.Headers {
		header.Set(name, value)
	}
	if r.ContentType != "" {
		header.Set("Content-Type", r.ContentType)
	}
	if r.ContentEncoding != "" {
		header.Set("Content-Encoding", r.ContentEncoding)
	}
	return &http.Request{
		Method: r.Method,
		URL:    &url.URL{Path: r.Path, RawQuery: r.Query},
		Header: header,
	}, body, nil
}

// errNoMatch is returned when the decoder matches none of a recording's requests.
var errNoMatch = errors.New("the decoder matches none of the recorded requests")

// Decode decodes every recorded request the decoder matches, the way the relay
// does, and returns all their resource spans as one document.
func Decode(d relay.Decoder, rec Recording) (otlp.Traces, error) {
	var all otlp.Traces
	matched := 0
	for i, recorded := range rec.Requests {
		if !d.Match(recorded.Method, recorded.Path, relay.CleanQuery(recorded.Query)) {
			continue
		}
		matched++
		r, body, err := recorded.HTTPRequest()
		if err != nil {
			return otlp.Traces{}, fmt.Errorf("request %d: %w", i, err)
		}
		req, err := relay.NewRequest(r, body, d)
		if err != nil {
			return otlp.Traces{}, fmt.Errorf("request %d: %w", i, err)
		}
		traces, err := relay.Decode(d, req)
		if err != nil {
			return otlp.Traces{}, fmt.Errorf("request %d: %w", i, err)
		}
		all.ResourceSpans = append(all.ResourceSpans, traces.ResourceSpans...)
	}
	if matched == 0 {
		return otlp.Traces{}, errNoMatch
	}
	return all, nil
}

// Canonical is a JSON document with its object keys sorted, indented by two
// spaces and ending in a newline: the form golden files are written in.
func Canonical(data []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// Golden decodes the recording with d and compares the result with the golden
// OTLP/JSON file. It also fails when the decoder's declarations are not valid
// (relay.Validate), when a span has a zero id, or when the value of a credential
// header or query parameter of the recording appears in the output.
// With UPDATE_GOLDEN=1 it writes the golden file instead.
func Golden(t testing.TB, d relay.Decoder, recordingPath, goldenPath string) {
	t.Helper()
	rec, err := ReadRecording(recordingPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := relay.Validate(d); err != nil {
		t.Fatal(err)
	}
	traces, err := Decode(d, rec)
	if err != nil {
		t.Fatalf("%s: %v", recordingPath, err)
	}
	for _, problem := range append(zeroIDs(traces), repeatedKeys(traces)...) {
		t.Errorf("%s: %s", recordingPath, problem)
	}
	out, err := otlp.Marshal(traces)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Canonical(out)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range credentials(rec) {
		if bytes.Contains(got, []byte(secret)) {
			t.Errorf("%s: the output holds the value of a credential the recording carried", recordingPath)
		}
	}
	if os.Getenv("UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll(filepath.Dir(goldenPath), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(goldenPath, got, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s", goldenPath)
		return
	}
	data, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("%v (UPDATE_GOLDEN=1 writes it)", err)
	}
	want, err := Canonical(data)
	if err != nil {
		t.Fatalf("%s: %v", goldenPath, err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("%s decodes to something else than %s (UPDATE_GOLDEN=1 rewrites it):\n%s",
			recordingPath, goldenPath, firstDifference(string(want), string(got)))
	}
}

// GoldenDir runs Golden for every <name>.recording.json in dir against
// <name>.otlp.json next to it, each as a subtest.
func GoldenDir(t *testing.T, d relay.Decoder, dir string) {
	t.Helper()
	recordings, err := filepath.Glob(filepath.Join(dir, "*.recording.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(recordings) == 0 {
		t.Fatalf("no *.recording.json in %s", dir)
	}
	for _, recording := range recordings {
		name := strings.TrimSuffix(filepath.Base(recording), ".recording.json")
		t.Run(name, func(t *testing.T) {
			Golden(t, d, recording, filepath.Join(dir, name+".otlp.json"))
		})
	}
}

// repeatedKeys names every resource, span and event that writes an attribute key twice: impact.ai refuses such a span.
func repeatedKeys(traces otlp.Traces) []string {
	var problems []string
	check := func(where string, attrs otlp.Attributes) {
		seen := map[string]bool{}
		for _, kv := range attrs {
			if seen[kv.Key] {
				problems = append(problems, fmt.Sprintf("%s writes the attribute %q twice", where, kv.Key))
			}
			seen[kv.Key] = true
		}
	}
	for _, rs := range traces.ResourceSpans {
		check("a resource", rs.Resource.Attributes)
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				check(fmt.Sprintf("the span %q", span.Name), span.Attributes)
				for _, e := range span.Events {
					check(fmt.Sprintf("an event %q of the span %q", e.Name, span.Name), e.Attributes)
				}
			}
		}
	}
	return problems
}

func zeroIDs(traces otlp.Traces) []string {
	var problems []string
	for _, rs := range traces.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				if span.TraceID.IsZero() || span.SpanID.IsZero() {
					problems = append(problems, fmt.Sprintf("the span %q has a zero trace or span id", span.Name))
				}
			}
		}
	}
	return problems
}

// credentials are the values, and the words of the values, of the recording's
// headers and query parameters whose names look like credentials. Words shorter
// than eight characters ("Bearer", "Basic") are left out.
func credentials(rec Recording) []string {
	var values []string
	add := func(value string) {
		for _, word := range append(strings.FieldsFunc(value, func(r rune) bool {
			return r == ' ' || r == '\t' || r == ';' || r == ',' || r == '='
		}), value) {
			if len(word) >= 8 {
				values = append(values, word)
			}
		}
	}
	for _, r := range rec.Requests {
		for name, value := range r.Headers {
			if relay.CredentialName(name) {
				add(value)
			}
		}
		query, _ := url.ParseQuery(strings.ReplaceAll(r.Query, ";", "&"))
		for name, all := range query {
			if relay.CredentialName(name) {
				for _, value := range all {
					add(value)
				}
			}
		}
	}
	return values
}

// firstDifference shows the first line where two texts differ, with a little context.
func firstDifference(want, got string) string {
	w, g := strings.Split(want, "\n"), strings.Split(got, "\n")
	for i := 0; i < len(w) || i < len(g); i++ {
		var wl, gl string
		if i < len(w) {
			wl = w[i]
		}
		if i < len(g) {
			gl = g[i]
		}
		if wl != gl {
			return fmt.Sprintf("line %d\n  want: %s\n  got:  %s", i+1, wl, gl)
		}
	}
	return "(no line differs)"
}
