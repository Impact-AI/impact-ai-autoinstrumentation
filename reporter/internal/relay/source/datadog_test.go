// SPDX-License-Identifier: Apache-2.0

package source

import (
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/relaytest"
)

const ddTestdata = "testdata/datadog"

// Every recording of the pinned tracers decodes to its golden OTLP/JSON (UPDATE_GOLDEN=1 rewrites them).
func TestDatadogRecordingsDecodeToTheirGoldenFiles(t *testing.T) {
	relaytest.GoldenDir(t, datadog{}, ddTestdata)
}

// The decoder claims the trace intake and LLM Observability's path of the EVP proxy, and nothing else the tracers send
// the Agent: /info, stats, telemetry, remote configuration, other EVP products, DogStatsD and profiles pass through
// unread.
func TestDatadogMatchesOnlyTheRequestsThatCarrySpans(t *testing.T) {
	d := datadog{}
	for _, path := range []string{
		"/v0.3/traces", "/v0.4/traces", "/v0.5/traces", "/v0.7/traces", "/v1.0/traces",
		"/evp_proxy/v2/api/v2/llmobs", "/evp_proxy/v4/api/v2/llmobs",
	} {
		for _, method := range []string{http.MethodPut, http.MethodPost} {
			if !d.Match(method, path, "") {
				t.Errorf("%s %s is not claimed", method, path)
			}
		}
		if d.Match(http.MethodGet, path, "") {
			t.Errorf("GET %s is claimed", path)
		}
	}
	for _, path := range []string{
		"/info", "/v0.6/stats", "/v0.1/pipeline_stats", "/telemetry/proxy/api/v2/apmtelemetry", "/v0.7/config",
		"/evp_proxy/v2/api/v2/citestcycle", "/evp_proxy/v4/api/v2/errorsintake",
		"/evp_proxy/v2/api/intake/llm-obs/v2/eval-metric", "/evp_proxy/vx/api/v2/llmobs", "/evp_proxy/v2/api/v2/llmobs/x",
		"/dogstatsd/v2/proxy", "/profiling/v1/input", "/debugger/v1/input", "/v0.4/traces/x", "/",
	} {
		if d.Match(http.MethodPost, path, "") || d.Match(http.MethodPut, path, "") {
			t.Errorf("%s is claimed", path)
		}
	}
}

// A payload the decoder cannot read is a *PayloadError, whose reason never quotes the payload: the relay has already
// passed it to the Agent, and the copy reads partial.
func TestDatadogRefusesWhatItCannotRead(t *testing.T) {
	for _, c := range []struct {
		name, path, contentType string
		body                    []byte
	}{
		{"not MessagePack", "/v0.4/traces", "application/msgpack", []byte("\xc1CONTENT-CANARY")},
		{"a map, not traces", "/v0.4/traces", "application/msgpack", mp(mpObj("CONTENT-CANARY", "x"))},
		{"a trace that is not spans", "/v0.4/traces", "application/msgpack", mp([]any{"CONTENT-CANARY"})},
		{"v0.5 without a string table", "/v0.5/traces", "application/msgpack", mp([]any{[]any{}})},
		{"v0.5 with an index out of the table", "/v0.5/traces", "application/msgpack",
			mp([]any{[]any{"a"}, []any{[]any{[]any{uint64(0), uint64(9), uint64(0), uint64(1), uint64(2), uint64(0),
				uint64(0), uint64(1), uint64(0), mpObj(), mpObj(), uint64(0)}}}})},
		{"JSON traces", "/v0.4/traces", "application/json", []byte(`[[{"name":"CONTENT-CANARY"}]]`)},
		{"the v1.0 format", "/v1.0/traces", "application/msgpack", mp([]any{})},
		{"the v0.7 format", "/v0.7/traces", "application/msgpack", mp([]any{})},
		{"LLM Observability that is not JSON", "/evp_proxy/v2/api/v2/llmobs", "application/json", []byte("CONTENT-CANARY")},
		{"LLM Observability that is not MessagePack", "/evp_proxy/v4/api/v2/llmobs", "application/msgpack", []byte("\xc1CONTENT-CANARY")},
		{"LLM Observability in another format", "/evp_proxy/v2/api/v2/llmobs", "application/x-protobuf", mp([]any{})},
		{"an LLM Observability span without ids", "/evp_proxy/v2/api/v2/llmobs", "application/json",
			[]byte(`[{"event_type":"span","spans":[{"name":"CONTENT-CANARY"}]}]`)},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := relay.Decode(datadog{}, relay.Request{Method: "PUT", Path: c.path, ContentType: c.contentType, Body: c.body})
			var payload *relay.PayloadError
			if !errors.As(err, &payload) {
				t.Fatalf("want a *relay.PayloadError, got %v", err)
			}
			if strings.Contains(payload.Reason, "CONTENT-CANARY") {
				t.Errorf("the reason quotes the payload: %q", payload.Reason)
			}
		})
	}
}

// Every request of every recording, cut short at every length, decodes or is refused, and never panics.
func TestDatadogSurvivesEveryTruncation(t *testing.T) {
	for _, req := range ddRecordedRequests(t) {
		for n := 0; n < len(req.Body); n += 1 + n/64 {
			cut := req
			cut.Body = req.Body[:n]
			func() {
				defer func() {
					if r := recover(); r != nil {
						t.Fatalf("%s cut at %d of %d bytes: panic %v", req.Path, n, len(req.Body), r)
					}
				}()
				if _, err := (datadog{}).Decode(cut); err != nil {
					var payload *relay.PayloadError
					if !errors.As(err, &payload) {
						t.Fatalf("%s cut at %d: %v is not a *relay.PayloadError", req.Path, n, err)
					}
				}
			}()
		}
	}
}

// A tag whose name says it holds a key, a password or an authorization never reaches impact.ai; a token count does.
func TestDatadogDropsCredentialTags(t *testing.T) {
	span := mpObj(
		"service", "support-assistant", "name", "openai.request", "resource", "createChatCompletion", "type", "llm",
		"trace_id", uint64(1), "span_id", uint64(2), "parent_id", uint64(0), "start", int64(1), "duration", int64(2),
		"error", int64(0),
		"meta", mpObj(
			"openai.user.api_key", "sk-live-abcdefghijklmnop",
			"http.request.headers.authorization", "sk-live-abcdefghijklmnop",
			"http.request.headers.cookie", "session=sk-live-abcdefghijklmnop",
			"gen_ai.operation.name", "llm", "gen_ai.provider.name", "openai", "gen_ai.request.model", "gpt-4o",
		),
		"metrics", mpObj("gen_ai.usage.input_tokens", 7.0),
	)
	out := ddDecodeV04(t, mp([]any{[]any{span}}))
	text := ddMarshal(t, out)
	if strings.Contains(text, "sk-live-abcdefghijklmnop") {
		t.Errorf("a credential reached the output: %s", text)
	}
	attrs := ddAttributesOf(out)
	if got := ddInt(attrs["gen_ai.usage.input_tokens"]); got != 7 {
		t.Errorf("gen_ai.usage.input_tokens = %d, want 7", got)
	}
}

// Datadog's 64-bit ids are widened with the trace's _dd.p.tid, which only the chunk's first span carries.
func TestDatadogWidensTraceIDsWithTheChunksTid(t *testing.T) {
	first := ddSpanMap(0xbeef, 1, 0, mpObj("_dd.p.tid", "6af1eba600000000"))
	child := ddSpanMap(0xbeef, 2, 1, mpObj())
	out := ddDecodeV04(t, mp([]any{[]any{child, first}}))
	want := otlp.TraceIDFromUint64s(0x6af1eba600000000, 0xbeef)
	for _, span := range ddSpans(out) {
		if span.TraceID != want {
			t.Errorf("span %s: trace id %s, want %s", span.SpanID, span.TraceID, want)
		}
	}
}

// A span whose LLM Observability event the tracer sent through the EVP proxy (_dd.llmobs.submitted) is decoded from
// the event, once: no span id appears twice in any recording's output.
func TestDatadogDecodesEachSpanOnce(t *testing.T) {
	submitted := ddSpanMap(1, 2, 0, mpObj("_dd.llmobs.submitted", "1", "gen_ai.operation.name", "llm"))
	other := ddSpanMap(1, 3, 2, mpObj())
	if spans := ddSpans(ddDecodeV04(t, mp([]any{[]any{submitted, other}}))); len(spans) != 1 ||
		spans[0].SpanID != otlp.SpanIDFromUint64(3) {
		t.Errorf("want only the span without an event of its own, got %v", spans)
	}
	golden, _ := filepath.Glob(filepath.Join(ddTestdata, "*.otlp.json"))
	for _, file := range golden {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		traces, err := otlp.Unmarshal(data)
		if err != nil {
			t.Fatal(err)
		}
		seen := map[otlp.SpanID]bool{}
		for _, span := range ddSpans(traces) {
			if seen[span.SpanID] {
				t.Errorf("%s: span %s twice", file, span.SpanID)
			}
			seen[span.SpanID] = true
		}
	}
}

// The GenAI keys this decoder writes: canonical keys of the vocabulary, in their current spelling. Datadog's own
// gen_ai tags (gen_ai.operation.name "llm", gen_ai.application.name) are read, never copied.
var ddGenAIKeys = []string{
	"gen_ai.agent.name", "gen_ai.conversation.id", "gen_ai.data_source.id", "gen_ai.input.messages",
	"gen_ai.operation.name", "gen_ai.output.messages", "gen_ai.provider.name", "gen_ai.request.max_tokens",
	"gen_ai.request.model", "gen_ai.request.top_k", "gen_ai.response.finish_reasons", "gen_ai.response.model",
	"gen_ai.retrieval.documents", "gen_ai.retrieval.query.text", "gen_ai.retrieval.top_k", "gen_ai.tool.call.arguments",
	"gen_ai.tool.call.id", "gen_ai.tool.call.result", "gen_ai.tool.definitions", "gen_ai.tool.name",
	"gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
	"gen_ai.usage.reasoning.output_tokens",
}

var ddOperationValues = []string{"chat", "text_completion", "embeddings", "invoke_agent", "execute_tool", "retrieval",
	"invoke_workflow", "execute_task"}

func TestDatadogWritesOnlyCanonicalGenAIKeys(t *testing.T) {
	golden, _ := filepath.Glob(filepath.Join(ddTestdata, "*.otlp.json"))
	for _, file := range golden {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		traces, err := otlp.Unmarshal(data)
		if err != nil {
			t.Fatal(err)
		}
		for _, span := range ddSpans(traces) {
			for _, kv := range span.Attributes {
				for _, prefix := range []string{"gen_ai.", "llm.", "openinference.", "traceloop."} {
					if strings.HasPrefix(kv.Key, prefix) && !slices.Contains(ddGenAIKeys, kv.Key) {
						t.Errorf("%s: span %q writes %s", file, span.Name, kv.Key)
					}
				}
				if kv.Key == "gen_ai.operation.name" && !slices.Contains(ddOperationValues, *kv.Value.StringValue) {
					t.Errorf("%s: span %q has the operation %q", file, span.Name, *kv.Value.StringValue)
				}
			}
		}
	}
}

// With LLM Observability on, each language's recording carries the planted content only in GenAI content keys and
// under datadog.content.; APM tracing alone carries none, which is why it reads Calls only.
func TestDatadogMovesContentAndAPMAloneHasNone(t *testing.T) {
	content := []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.call.arguments",
		"gen_ai.tool.call.result", "gen_ai.retrieval.query.text", "gen_ai.system_instructions"}
	golden, _ := filepath.Glob(filepath.Join(ddTestdata, "*.otlp.json"))
	for _, file := range golden {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		traces, err := otlp.Unmarshal(data)
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, span := range ddSpans(traces) {
			for _, kv := range span.Attributes {
				text := ddMarshal(t, otlp.Traces{ResourceSpans: []otlp.ResourceSpans{{ScopeSpans: []otlp.ScopeSpans{
					{Spans: []otlp.Span{{Attributes: otlp.Attributes{kv}}}}}}}})
				if !strings.Contains(text, "CONTENT-CANARY") {
					continue
				}
				found = true
				if !slices.Contains(content, kv.Key) && !strings.HasPrefix(kv.Key, "datadog.content.") {
					t.Errorf("%s: span %q carries content in %s", file, span.Name, kv.Key)
				}
			}
			if strings.Contains(span.Name, "CONTENT-CANARY") {
				t.Errorf("%s: a span name carries content", file)
			}
		}
		apm := strings.Contains(filepath.Base(file), "-apm-")
		if apm && found {
			t.Errorf("%s: APM tracing alone carries content", file)
		}
		if !apm && !found {
			t.Errorf("%s: LLM Observability's content is missing", file)
		}
	}
}

// Random bytes on every claimed path never panic.
func FuzzDatadogDecode(f *testing.F) {
	for _, req := range ddRecordedRequests(f) {
		f.Add(req.Path, req.Body)
	}
	f.Fuzz(func(t *testing.T, path string, body []byte) {
		if !(datadog{}).Match(http.MethodPut, path, "") {
			path = "/v0.4/traces"
		}
		for _, contentType := range []string{"application/msgpack", "application/json"} {
			out, err := (datadog{}).Decode(relay.Request{Method: "PUT", Path: path, ContentType: contentType, Body: body})
			if err == nil {
				if _, err := otlp.Marshal(out); err != nil {
					t.Fatalf("the output does not marshal: %v", err)
				}
			}
		}
	})
}

// --- helpers ---------------------------------------------------------------------------------------------------

func ddRecordedRequests(t testing.TB) []relay.Request {
	t.Helper()
	files, _ := filepath.Glob(filepath.Join(ddTestdata, "*.recording.json"))
	var out []relay.Request
	for _, file := range files {
		rec, err := relaytest.ReadRecording(file)
		if err != nil {
			t.Fatal(err)
		}
		for _, r := range rec.Requests {
			body, err := base64.StdEncoding.DecodeString(r.BodyBase64)
			if err != nil {
				t.Fatal(err)
			}
			hr, _, err := r.HTTPRequest()
			if err != nil {
				t.Fatal(err)
			}
			hr.URL = &url.URL{Path: r.Path}
			req, err := relay.NewRequest(hr, body, datadog{})
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, req)
		}
	}
	if len(out) == 0 {
		t.Fatal("no recorded request")
	}
	return out
}

func ddDecodeV04(t *testing.T, body []byte) otlp.Traces {
	t.Helper()
	out, err := relay.Decode(datadog{}, relay.Request{Method: "PUT", Path: "/v0.4/traces",
		ContentType: "application/msgpack", Body: body})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func ddSpanMap(trace, span, parent uint64, meta mpMap) mpMap {
	return mpObj("service", "svc", "name", "op", "resource", "op", "type", "", "trace_id", trace, "span_id", span,
		"parent_id", parent, "start", int64(1), "duration", int64(1), "error", int64(0), "meta", meta,
		"metrics", mpObj())
}

func ddSpans(t otlp.Traces) []otlp.Span {
	var out []otlp.Span
	for _, rs := range t.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			out = append(out, ss.Spans...)
		}
	}
	return out
}

func ddAttributesOf(t otlp.Traces) map[string]otlp.AnyValue {
	out := map[string]otlp.AnyValue{}
	for _, span := range ddSpans(t) {
		for _, kv := range span.Attributes {
			out[kv.Key] = kv.Value
		}
	}
	return out
}

func ddInt(v otlp.AnyValue) int64 {
	if v.IntValue == nil {
		return -1
	}
	return int64(*v.IntValue)
}

func ddMarshal(t testing.TB, traces otlp.Traces) string {
	t.Helper()
	data, err := otlp.Marshal(traces)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// mpObj is a MessagePack map of string keys, in the order given.
func mpObj(kv ...any) mpMap {
	m := mpMap{}
	for i := 0; i+1 < len(kv); i += 2 {
		m = append(m, mpEntry{Key: kv[i], Value: kv[i+1]})
	}
	return m
}

// mp encodes a value the way the tracers do, for tests that need a payload no recording has.
func mp(v any) []byte {
	var b []byte
	var enc func(any)
	enc = func(v any) {
		switch x := v.(type) {
		case nil:
			b = append(b, 0xc0)
		case bool:
			if x {
				b = append(b, 0xc3)
			} else {
				b = append(b, 0xc2)
			}
		case string:
			b = append(b, 0xdb)
			b = binary.BigEndian.AppendUint32(b, uint32(len(x)))
			b = append(b, x...)
		case []byte:
			b = append(b, 0xc6)
			b = binary.BigEndian.AppendUint32(b, uint32(len(x)))
			b = append(b, x...)
		case uint64:
			b = append(b, 0xcf)
			b = binary.BigEndian.AppendUint64(b, x)
		case int64:
			b = append(b, 0xd3)
			b = binary.BigEndian.AppendUint64(b, uint64(x))
		case float64:
			b = append(b, 0xcb)
			b = binary.BigEndian.AppendUint64(b, math.Float64bits(x))
		case []any:
			b = append(b, 0xdd)
			b = binary.BigEndian.AppendUint32(b, uint32(len(x)))
			for _, e := range x {
				enc(e)
			}
		case mpMap:
			b = append(b, 0xdf)
			b = binary.BigEndian.AppendUint32(b, uint32(len(x)))
			for _, e := range x {
				enc(e.Key)
				enc(e.Value)
			}
		default:
			panic("mp: unsupported value")
		}
	}
	enc(v)
	return b
}

// The test encoder and the decoder agree on every type the tracers write.
func TestMsgpackReadsWhatTheTracersWrite(t *testing.T) {
	in := []any{nil, true, false, "CONTENT", []byte{1, 2}, uint64(math.MaxUint64), int64(-5), 1.5,
		mpObj("k", []any{int64(1)})}
	out, err := decodeMsgpack(mp(in))
	if err != nil {
		t.Fatal(err)
	}
	got := out.([]any)
	want := []any{nil, true, false, "CONTENT", []byte{1, 2}, uint64(math.MaxUint64), int64(-5), 1.5}
	for i := range want {
		if w, ok := want[i].([]byte); ok {
			if g, _ := got[i].([]byte); string(g) != string(w) {
				t.Errorf("%d: %v, want %v", i, got[i], want[i])
			}
			continue
		}
		if got[i] != want[i] {
			t.Errorf("%d: %#v, want %#v", i, got[i], want[i])
		}
	}
	// The compact forms: fixint, negative fixint, fixstr, fixarray, fixmap, float32, the short integers.
	compact := []byte{0x95, 0x07, 0xff, 0xa2, 'h', 'i', 0x81, 0xa1, 'k', 0x90, 0xca, 0x3f, 0xc0, 0x00, 0x00}
	v, err := decodeMsgpack(compact)
	if err != nil {
		t.Fatal(err)
	}
	list := v.([]any)
	if list[0] != int64(7) || list[1] != int64(-1) || list[2] != "hi" || list[4] != 1.5 {
		t.Errorf("compact forms: %#v", list)
	}
	// A count beyond the bytes left is refused before anything is allocated.
	if _, err := decodeMsgpack([]byte{0xdd, 0xff, 0xff, 0xff, 0xff}); err == nil {
		t.Error("a lying array length is read")
	}
	deep := slices.Repeat([]byte{0x91}, 100)
	if _, err := decodeMsgpack(append(deep, 0xc0)); err == nil {
		t.Error("nesting without end is read")
	}
}

// Nested counts fit the bytes left but end in an invalid value. Allocating the advertised capacity at every
// level would amplify the payload; count bytes, not allocations (one allocation can be arbitrarily large).
func TestMsgpackNestedCountsDoNotAmplify(t *testing.T) {
	for _, kind := range []byte{0xdd, 0xdf} { // array32, map32
		t.Run(fmt.Sprintf("type-%x", kind), func(t *testing.T) {
			const depth = 8
			body := make([]byte, 64<<10)
			for offset := 0; offset < depth*5; offset += 5 {
				body[offset] = kind
				count := len(body) - offset - 5
				if kind == 0xdf {
					count /= 2 // each map entry requires a key and a value
				}
				binary.BigEndian.PutUint32(body[offset+1:], uint32(count))
			}
			body[depth*5] = 0xc1 // reserved MessagePack byte, reached after allocating all eight containers
			var before, after runtime.MemStats
			runtime.ReadMemStats(&before)
			r := &mpReader{b: body}
			_, err := r.value(0)
			runtime.ReadMemStats(&after)
			if err == nil || r.off != depth*5+1 {
				t.Fatalf("did not reach the invalid nested value: offset %d, error %v", r.off, err)
			}
			if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 256<<10 {
				t.Fatalf("nested counts allocated %d bytes for a %d-byte payload", allocated, len(body))
			}
		})
	}
}

// The recordings the Datadog harness made in its cells (testing/apps/datadog/cells.py writes them, named
// <cell>-<mode>-<app>), when DATADOG_HARNESS_RECORDINGS names their directory: each decodes, and gives what its mode
// allows. With LLM Observability on (llmobs, evp) the canonical interaction has its agent, tool and retrieval steps,
// two model calls and content; with APM alone (apm) its model calls have a model and tokens and no content.
func TestDatadogHarnessRecordings(t *testing.T) {
	dir := os.Getenv("DATADOG_HARNESS_RECORDINGS")
	if dir == "" {
		t.Skip("DATADOG_HARNESS_RECORDINGS is not set: `make test-datadog` sets it")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.recording.json"))
	if len(files) == 0 {
		t.Fatalf("no recording in %s", dir)
	}
	for _, file := range files {
		name := strings.TrimSuffix(filepath.Base(file), ".recording.json")
		t.Run(name, func(t *testing.T) {
			parts := strings.Split(name, "-")
			if len(parts) != 3 {
				t.Fatalf("%s is not <cell>-<mode>-<app>", name)
			}
			mode, app := parts[1], parts[2]
			rec, err := relaytest.ReadRecording(file)
			if err != nil {
				t.Fatal(err)
			}
			traces, err := relaytest.Decode(datadog{}, rec)
			if err != nil {
				t.Fatal(err)
			}
			ops := map[string]int{}
			content := false
			for _, span := range ddSpans(traces) {
				attrs := map[string]otlp.AnyValue{}
				for _, kv := range span.Attributes {
					attrs[kv.Key] = kv.Value
					if strings.Contains(ddMarshal(t, otlp.Traces{ResourceSpans: []otlp.ResourceSpans{{ScopeSpans: []otlp.ScopeSpans{
						{Spans: []otlp.Span{{Attributes: otlp.Attributes{kv}}}}}}}}), "CONTENT-CANARY") {
						content = true
					}
				}
				if op := attrs["gen_ai.operation.name"].StringValue; op != nil {
					ops[*op]++
					if *op == "chat" && (attrs["gen_ai.response.model"].StringValue == nil ||
						attrs["gen_ai.usage.input_tokens"].IntValue == nil) {
						t.Errorf("a model call without its model or tokens: %q", span.Name)
					}
				}
			}
			switch {
			case mode == "apm" && content:
				t.Error("APM tracing alone carried content")
			case mode != "apm" && !content:
				t.Error("LLM Observability's content is missing")
			case app == "canonical" && mode != "apm" &&
				(ops["chat"] != 2 || ops["invoke_agent"] != 1 || ops["execute_tool"] != 1 || ops["retrieval"] != 1):
				t.Errorf("the canonical interaction's steps: %v", ops)
			case app == "canonical" && mode == "apm" && ops["chat"] != 2:
				t.Errorf("APM alone: %v model calls, want 2", ops["chat"])
			case app == "providers" && ops["chat"] != 3:
				t.Errorf("one call to each provider: %v", ops)
			}
		})
	}
}
