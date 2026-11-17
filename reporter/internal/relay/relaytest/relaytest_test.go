// SPDX-License-Identifier: Apache-2.0

package relaytest

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// planted is the fake key testdata/example.recording.json carries in a header and in the query.
const planted = "sk-live-abcdefghijklmnop"

// example decodes the made-up format of testdata/example.recording.json: JSON records with ids of their own.
type example struct {
	leak    bool // writes the planted key into a span
	zeroIDs bool // writes spans without ids
	twice   bool // writes an attribute key twice on a span
}

func (example) Source() string    { return "example" }
func (example) Name() string      { return "example/1.0.0" }
func (example) Headers() []string { return []string{"X-Example-Lang"} }
func (example) Match(method, path, _ string) bool {
	return method == http.MethodPost && path == "/v1/example/spans"
}

func (e example) Decode(req relay.Request) (otlp.Traces, error) {
	var payload struct {
		Records []struct {
			ID, Parent, Name, Operation, Model, Tool, Component string
			InputTokens, OutputTokens                           int64
			Start, End                                          uint64
		}
	}
	if err := json.Unmarshal(req.Body, &payload); err != nil {
		return otlp.Traces{}, relay.Unreadable("not JSON", err)
	}
	if req.Header.Get("X-Api-Key") != "" || strings.Contains(req.RawQuery, "license_key") {
		return otlp.Traces{}, fmt.Errorf("the decoder was given a credential")
	}
	var spans []otlp.Span
	for _, r := range payload.Records {
		root := r.ID
		if r.Parent != "" {
			root = r.Parent
		}
		s := otlp.Span{
			TraceID:           otlp.TraceIDOf("example-record", root),
			SpanID:            otlp.SpanIDOf("example-record", r.ID),
			Name:              r.Name,
			Kind:              otlp.SpanKindClient,
			StartTimeUnixNano: otlp.Nanos(r.Start),
			EndTimeUnixNano:   otlp.Nanos(r.End),
			Attributes: otlp.Attributes{
				otlp.Str("gen_ai.operation.name", r.Operation),
				otlp.Str("example.component", r.Component),
			},
		}
		if r.Parent != "" {
			s.ParentSpanID = otlp.SpanIDOf("example-record", r.Parent)
		}
		if r.Model != "" {
			s.Attributes = append(s.Attributes,
				otlp.Str("gen_ai.request.model", r.Model),
				otlp.Int("gen_ai.usage.input_tokens", r.InputTokens),
				otlp.Int("gen_ai.usage.output_tokens", r.OutputTokens))
		}
		if r.Tool != "" {
			s.Attributes = append(s.Attributes, otlp.Str("gen_ai.tool.name", r.Tool))
		}
		if e.leak {
			s.Attributes = append(s.Attributes, otlp.Str("example.key", planted))
		}
		if e.zeroIDs {
			s.TraceID, s.SpanID = otlp.TraceID{}, otlp.SpanID{}
		}
		if e.twice {
			s.Attributes = append(s.Attributes, otlp.Str("example.component", "again"))
		}
		spans = append(spans, s)
	}
	return otlp.Traces{ResourceSpans: []otlp.ResourceSpans{{
		Resource: otlp.Resource{Attributes: otlp.Attributes{otlp.Str("example.lang", req.Header.Get("X-Example-Lang"))}},
		ScopeSpans: []otlp.ScopeSpans{{
			Scope: otlp.Scope{Name: "example-sdk"},
			Spans: spans,
		}},
	}}}, nil
}

func TestTheGoldenFileOfTheExampleRecording(t *testing.T) {
	GoldenDir(t, example{}, "testdata")
}

// recorder stands in for a test, so that a failing check can be watched failing.
type recorder struct {
	testing.TB
	failed   bool
	messages []string
}

func (r *recorder) Helper()             {}
func (r *recorder) Logf(string, ...any) {}
func (r *recorder) Errorf(f string, a ...any) {
	r.failed = true
	r.messages = append(r.messages, fmt.Sprintf(f, a...))
}
func (r *recorder) Fatalf(f string, a ...any) { r.Errorf(f, a...); runtime.Goexit() }
func (r *recorder) Fatal(a ...any)            { r.Errorf("%s", fmt.Sprint(a...)); runtime.Goexit() }

func run(check func(testing.TB)) *recorder {
	r := &recorder{}
	done := make(chan struct{})
	go func() {
		defer close(done)
		check(r)
	}()
	<-done
	return r
}

func writeFile(t *testing.T, path, content string) string {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestGoldenFails(t *testing.T) {
	dir := t.TempDir()
	recording := filepath.Join("testdata", "example.recording.json")
	golden := filepath.Join("testdata", "example.otlp.json")
	other := writeFile(t, filepath.Join(dir, "other.otlp.json"), `{"resourceSpans":[]}`)
	data, err := os.ReadFile(recording)
	if err != nil {
		t.Fatal(err)
	}
	var withoutHow map[string]any
	if err := json.Unmarshal(data, &withoutHow); err != nil {
		t.Fatal(err)
	}
	delete(withoutHow, "how")
	undocumented, _ := json.Marshal(withoutHow)
	noHow := writeFile(t, filepath.Join(dir, "no-how.recording.json"), string(undocumented))
	misspelt := writeFile(t, filepath.Join(dir, "misspelt.recording.json"),
		strings.Replace(string(data), `"bodyBase64"`, `"body_b64"`, 1))

	for _, c := range []struct {
		name               string
		decoder            relay.Decoder
		recording, golden  string
		wantInFirstMessage string
	}{
		{"when the output differs", example{}, recording, other, "UPDATE_GOLDEN=1"},
		{"when a credential reaches the output", example{leak: true}, recording, golden, "credential"},
		{"when a span has no ids", example{zeroIDs: true}, recording, golden, "zero"},
		{"when a span writes a key twice", example{twice: true}, recording, golden, `"example.component" twice`},
		{"when the recording does not say how it was made", example{}, noHow, golden, `"how"`},
		{"when a request keeps the mock's own field name", example{}, misspelt, golden, "body_b64"},
		{"when the decoder matches no request", unmatched{}, recording, golden, "matches none"},
	} {
		r := run(func(tb testing.TB) { Golden(tb, c.decoder, c.recording, c.golden) })
		if !r.failed {
			t.Errorf("%s: Golden passed", c.name)
		} else if !strings.Contains(r.messages[0], c.wantInFirstMessage) {
			t.Errorf("%s: %v", c.name, r.messages)
		}
	}
}

type unmatched struct{ example }

func (unmatched) Match(string, string, string) bool { return false }

func TestUpdateGoldenWritesTheGoldenFile(t *testing.T) {
	golden := filepath.Join(t.TempDir(), "new", "example.otlp.json")
	t.Setenv("UPDATE_GOLDEN", "1")
	Golden(t, example{}, filepath.Join("testdata", "example.recording.json"), golden)
	written, err := os.ReadFile(golden)
	if err != nil {
		t.Fatal(err)
	}
	committed, err := os.ReadFile(filepath.Join("testdata", "example.otlp.json"))
	if err != nil {
		t.Fatal(err)
	}
	if string(written) != string(committed) {
		t.Errorf("UPDATE_GOLDEN wrote another file than the committed golden:\n%s", written)
	}
	t.Setenv("UPDATE_GOLDEN", "")
	Golden(t, example{}, filepath.Join("testdata", "example.recording.json"), golden)
}

func TestCanonicalSortsKeysAndKeepsNumbers(t *testing.T) {
	got, err := Canonical([]byte(`{"b":1.50,"a":{"d":"<x>","c":[2,1]}}`))
	if err != nil {
		t.Fatal(err)
	}
	want := "{\n  \"a\": {\n    \"c\": [\n      2,\n      1\n    ],\n    \"d\": \"<x>\"\n  },\n  \"b\": 1.50\n}\n"
	if string(got) != want {
		t.Errorf("got\n%s", got)
	}
}
