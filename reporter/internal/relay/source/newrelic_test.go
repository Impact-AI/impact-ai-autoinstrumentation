// SPDX-License-Identifier: Apache-2.0

package source

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/relaytest"
)

// The recorded canonical interaction of the Python and Node.js agents decodes to its golden OTLP/JSON.
func TestNewRelicRecordingsDecodeToTheirGoldenFiles(t *testing.T) {
	relaytest.GoldenDir(t, newrelic{}, "testdata/newrelic")
}

// decodeAll decodes every request of a recording that the decoder matches, as the relay does.
func nrDecodeAll(t *testing.T, name string) otlp.Traces {
	t.Helper()
	rec, err := relaytest.ReadRecording("testdata/newrelic/" + name + ".recording.json")
	if err != nil {
		t.Fatal(err)
	}
	traces, err := relaytest.Decode(newrelic{}, rec)
	if err != nil {
		t.Fatal(err)
	}
	return traces
}

func nrSpansOf(traces otlp.Traces) []otlp.Span {
	var spans []otlp.Span
	for _, rs := range traces.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			spans = append(spans, ss.Spans...)
		}
	}
	return spans
}

func nrAttr(span otlp.Span, key string) (otlp.AnyValue, bool) {
	for _, kv := range span.Attributes {
		if kv.Key == key {
			return kv.Value, true
		}
	}
	return otlp.AnyValue{}, false
}

func nrStr(span otlp.Span, key string) string {
	if v, ok := nrAttr(span, key); ok && v.StringValue != nil {
		return *v.StringValue
	}
	return ""
}

func nrIntAttr(span otlp.Span, key string) (int64, bool) {
	if v, ok := nrAttr(span, key); ok && v.IntValue != nil {
		return int64(*v.IntValue), true
	}
	return 0, false
}

func nrByOperation(spans []otlp.Span) map[string][]otlp.Span {
	out := map[string][]otlp.Span{}
	for _, span := range spans {
		out[nrStr(span, "gen_ai.operation.name")] = append(out[nrStr(span, "gen_ai.operation.name")], span)
	}
	return out
}

// Both agents' interaction reads as the canonical one: an agent support, two chats of gpt-4o with 420 input and 100
// output tokens, the tool get_order and a retrieval of three documents of a top-k of 5, all in one trace, each AI span
// a child of the agent's span it was recorded in.
func TestNewRelicGivesTheCanonicalInteraction(t *testing.T) {
	for _, name := range []string{"python", "nodejs"} {
		t.Run(name, func(t *testing.T) { nrCheckCanonical(t, nrSpansOf(nrDecodeAll(t, name))) })
	}
}

func nrCheckCanonical(t *testing.T, spans []otlp.Span) {
	t.Helper()
	ops := nrByOperation(spans)
	if len(ops["chat"]) != 2 || len(ops["execute_tool"]) != 1 || len(ops["retrieval"]) != 1 || len(ops["invoke_agent"]) != 1 {
		t.Fatalf("chats %d, tools %d, retrievals %d, agents %d", len(ops["chat"]), len(ops["execute_tool"]),
			len(ops["retrieval"]), len(ops["invoke_agent"]))
	}
	var in, out int64
	finishes := map[string]bool{}
	for _, chat := range ops["chat"] {
		i, _ := nrIntAttr(chat, "gen_ai.usage.input_tokens")
		o, _ := nrIntAttr(chat, "gen_ai.usage.output_tokens")
		in, out = in+i, out+o
		if got := nrStr(chat, "gen_ai.response.model"); got != "gpt-4o-2024-08-06" {
			t.Errorf("response model %q", got)
		}
		if got := nrStr(chat, "gen_ai.provider.name"); got != "openai" {
			t.Errorf("provider %q", got)
		}
		v, _ := nrAttr(chat, "gen_ai.response.finish_reasons")
		if v.ArrayValue != nil && len(v.ArrayValue.Values) == 1 {
			finishes[*v.ArrayValue.Values[0].StringValue] = true
		}
	}
	if in != 420 || out != 100 || !finishes["stop"] || !finishes["tool_calls"] {
		t.Errorf("tokens %d/%d, finishes %v", in, out, finishes)
	}
	if got := nrStr(ops["invoke_agent"][0], "gen_ai.agent.name"); got != "support" {
		t.Errorf("agent %q", got)
	}
	tool := ops["execute_tool"][0]
	if nrStr(tool, "gen_ai.tool.name") != "get_order" || nrStr(tool, "gen_ai.tool.call.id") != "call_1" {
		t.Errorf("tool %q, call %q", nrStr(tool, "gen_ai.tool.name"), nrStr(tool, "gen_ai.tool.call.id"))
	}
	retrieval := ops["retrieval"][0]
	var documents []map[string]any
	if err := json.Unmarshal([]byte(nrStr(retrieval, "gen_ai.retrieval.documents")), &documents); err != nil || len(documents) != 3 {
		t.Errorf("documents %v (%v)", documents, err)
	}
	if k, _ := nrIntAttr(retrieval, "gen_ai.retrieval.top_k"); k != 5 {
		t.Errorf("top-k %d", k)
	}

	traces, ids := map[otlp.TraceID]bool{}, map[otlp.SpanID]bool{}
	for _, span := range spans {
		traces[span.TraceID] = true
		ids[span.SpanID] = true
	}
	if len(traces) != 1 {
		t.Errorf("%d traces", len(traces))
	}
	for _, span := range spans {
		if nrStr(span, "newrelic.event.type") != "" && !ids[span.ParentSpanID] {
			t.Errorf("%s: its parent %s is not a span of the recording", span.Name, span.ParentSpanID)
		}
		if span.EndTimeUnixNano < span.StartTimeUnixNano || span.StartTimeUnixNano == 0 {
			t.Errorf("%s: %d to %d", span.Name, span.StartTimeUnixNano, span.EndTimeUnixNano)
		}
	}
}

// nrContentKeys are the GenAI keys that carry content; nrWrittenKeys the other GenAI keys the decoder writes.
var (
	nrContentKeys = map[string]bool{
		"gen_ai.input.messages": true, "gen_ai.output.messages": true, "gen_ai.tool.call.arguments": true,
		"gen_ai.tool.call.result": true, "gen_ai.retrieval.query.text": true,
	}
	nrWrittenKeys = map[string]bool{
		"gen_ai.operation.name": true, "gen_ai.provider.name": true, "gen_ai.request.model": true,
		"gen_ai.response.model": true, "gen_ai.request.max_tokens": true, "gen_ai.response.finish_reasons": true,
		"gen_ai.usage.input_tokens": true, "gen_ai.usage.output_tokens": true, "gen_ai.tool.name": true,
		"gen_ai.tool.call.id": true, "gen_ai.agent.name": true, "gen_ai.retrieval.top_k": true,
		"gen_ai.retrieval.documents": true,
	}
)

// Content is moved, not copied: the planted text is only in the GenAI content keys and under newrelic.content., and
// no key is another convention's (llm.*, openinference.*, traceloop.*) or a GenAI key the decoder does not write.
func TestNewRelicContentIsOnlyWhereItIsWithheld(t *testing.T) {
	for _, name := range []string{"python", "nodejs"} {
		t.Run(name, func(t *testing.T) { nrCheckContent(t, nrSpansOf(nrDecodeAll(t, name))) })
	}
}

// Fresh recordings, made by testing/apps/newrelic_cells.sh with the pinned agents, read as the canonical interaction
// too, so that a change in what an agent sends fails here before a golden file is rewritten. Without
// NEWRELIC_RECORDINGS (a directory of *.recording.json) there is nothing to read.
func TestNewRelicFreshRecordings(t *testing.T) {
	dir := os.Getenv("NEWRELIC_RECORDINGS")
	if dir == "" {
		t.Skip("NEWRELIC_RECORDINGS names no directory of fresh recordings")
	}
	paths, err := filepath.Glob(filepath.Join(dir, "*.recording.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no recording in %s (%v)", dir, err)
	}
	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			rec, err := relaytest.ReadRecording(path)
			if err != nil {
				t.Fatal(err)
			}
			traces, err := relaytest.Decode(newrelic{}, rec)
			if err != nil {
				t.Fatal(err)
			}
			out, err := otlp.Marshal(traces)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Contains(out, []byte(nrPlantedLicense)) {
				t.Error("the output holds the license key")
			}
			nrCheckCanonical(t, nrSpansOf(traces))
			nrCheckContent(t, nrSpansOf(traces))
		})
	}
}

func nrCheckContent(t *testing.T, spans []otlp.Span) {
	t.Helper()
	found := 0
	for _, span := range spans {
		for _, kv := range span.Attributes {
			data, _ := json.Marshal(kv.Value)
			planted := bytes.Contains(data, []byte("CONTENT-CANARY"))
			switch {
			case nrContentKeys[kv.Key] || strings.HasPrefix(kv.Key, "newrelic.content."):
				if planted {
					found++
				}
			case planted:
				t.Errorf("%s: %s holds content outside a withheld key", span.Name, kv.Key)
			}
			if strings.HasPrefix(kv.Key, "gen_ai.") && !nrContentKeys[kv.Key] && !nrWrittenKeys[kv.Key] {
				t.Errorf("%s: %s is not a key the decoder writes", span.Name, kv.Key)
			}
			for _, other := range []string{"llm.", "openinference.", "traceloop."} {
				if strings.HasPrefix(kv.Key, other) {
					t.Errorf("%s: %s is another convention's key", span.Name, kv.Key)
				}
			}
		}
	}
	if found == 0 {
		t.Error("the planted content is nowhere: the recording has none")
	}
}

func nrRequest(t *testing.T, query, body string, header http.Header) relay.Request {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, newrelicPath+"?"+query, nil)
	for name, values := range header {
		r.Header[name] = values
	}
	req, err := relay.NewRequest(r, []byte(body), newrelic{})
	if err != nil {
		t.Fatal(err)
	}
	return req
}

const nrPlantedLicense = "planted0license0key0000000000000000NRAL"

// Only span_event_data and custom_event_data are claimed; every other call of the protocol is only relayed, and
// gives nothing, and no error, if it reaches Decode.
func TestNewRelicDecodesOnlyTheCallsThatCarrySpans(t *testing.T) {
	d := newrelic{}
	for _, method := range []string{http.MethodPost, http.MethodPut} {
		for _, call := range []string{"span_event_data", "custom_event_data"} {
			query := "protocol_version=17&marshal_format=json&method=" + call + "&run_id=1"
			if !d.Match(method, newrelicPath, query) {
				t.Errorf("%s %s?%s is not matched", method, newrelicPath, query)
			}
		}
	}
	for _, path := range []string{"/", "/v1/traces", "/agent_listener/invoke_raw_method/x"} {
		if d.Match(http.MethodPost, path, "method=span_event_data") {
			t.Errorf("%s is matched", path)
		}
	}
	if d.Match(http.MethodGet, newrelicPath, "method=span_event_data") {
		t.Error("a GET is matched")
	}
	calls := []string{"preconnect", "connect", "metric_data", "analytic_event_data", "error_event_data",
		"transaction_sample_data", "log_event_data", "shutdown", ""}
	for _, call := range calls {
		if d.Match(http.MethodPost, newrelicPath, "method="+call) {
			t.Errorf("%q is matched", call)
		}
		got, err := relay.Decode(d, nrRequest(t, "method="+call+"&license_key="+nrPlantedLicense, `{"license":"x"}`, nil))
		if err != nil || len(got.ResourceSpans) != 0 {
			t.Errorf("%q: %v, %v", call, got, err)
		}
	}
}

// A body that is not the protocol's JSON is a payload error, which the relay reports as a partial copy.
func TestNewRelicReportsAnUnreadableBody(t *testing.T) {
	for name, body := range map[string]string{
		"not json":      "\x00\x01",
		"not an array":  `{"a":1}`,
		"no events":     `["run"]`,
		"events object": `["run", {}, {}]`,
	} {
		_, err := relay.Decode(newrelic{}, nrRequest(t, "method=span_event_data", body, nil))
		var payload *relay.PayloadError
		if !errors.As(err, &payload) {
			t.Errorf("%s: %v", name, err)
		} else if strings.Contains(payload.Reason, body) && len(body) > 4 {
			t.Errorf("%s: the reason quotes the payload: %s", name, payload.Reason)
		}
	}
	_, err := relay.Decode(newrelic{}, nrRequest(t, "method=span_event_data&marshal_format=protobuf", `["run",[]]`, nil))
	var payload *relay.PayloadError
	if !errors.As(err, &payload) {
		t.Errorf("another marshal format: %v", err)
	}
}

// An older agent's 16-digit trace id is widened with a high half of zero; an event without valid ids is left out; the
// Node.js agent's custom events come without the reservoir object; the language and version come from User-Agent.
func TestNewRelicReadsTheShapesTheAgentsWrite(t *testing.T) {
	body := `["run",{"reservoir_size":2000,"events_seen":3},[
	  [{"type":"Span","traceId":"00f067aa0ba902b7","guid":"53995c3f42cd8ad8","name":"WebTransaction/Function/app:index",
	    "timestamp":1700000000000,"duration":0.25,"nr.entryPoint":true,"category":"generic"},
	   {"user.plan":"gold"},{"http.statusCode":200,"error.class":"ValueError","error.message":"bad order"}],
	  [{"type":"Span","traceId":"","guid":"53995c3f42cd8ad9","name":"lost","timestamp":1700000000000,"duration":0.1},{},{}],
	  [{"type":"Span","traceId":"4bf92f3577b34da6a3ce929d0e0e4736","guid":"zz","name":"bad","timestamp":1,"duration":0.1},{},{}]
	]]`
	header := http.Header{"User-Agent": {"NewRelic-PythonAgent/9.1.0 (Python 3.11.4 linux)"}}
	traces, err := relay.Decode(newrelic{}, nrRequest(t, "method=span_event_data", body, header))
	if err != nil {
		t.Fatal(err)
	}
	spans := nrSpansOf(traces)
	if len(spans) != 1 {
		t.Fatalf("%d spans: %+v", len(spans), spans)
	}
	span := spans[0]
	if span.TraceID.String() != "000000000000000000f067aa0ba902b7" || span.Kind != otlp.SpanKindServer {
		t.Errorf("trace %s, kind %d", span.TraceID, span.Kind)
	}
	if span.StartTimeUnixNano != 1_700_000_000_000_000_000 || span.EndTimeUnixNano-span.StartTimeUnixNano != 250_000_000 {
		t.Errorf("time %d to %d", span.StartTimeUnixNano, span.EndTimeUnixNano)
	}
	if span.Status.Code != otlp.StatusError || nrStr(span, "error.type") != "ValueError" ||
		nrStr(span, "newrelic.content.error.message") != "bad order" || nrStr(span, "newrelic.user.plan") != "gold" {
		t.Errorf("attributes %+v, status %+v", span.Attributes, span.Status)
	}
	if code, _ := nrIntAttr(span, "http.response.status_code"); code != 200 {
		t.Errorf("status code %d", code)
	}
	resource := traces.ResourceSpans[0].Resource.Attributes
	want := map[string]string{"telemetry.sdk.name": "newrelic", "telemetry.sdk.language": "python", "telemetry.sdk.version": "9.1.0"}
	for _, kv := range resource {
		if w, ok := want[kv.Key]; ok && *kv.Value.StringValue != w {
			t.Errorf("%s = %s", kv.Key, *kv.Value.StringValue)
		}
	}

	node := `["run",[[{"type":"LlmChatCompletionSummary","timestamp":1700000000000},
	  {"id":"c1","span_id":"53995c3f42cd8ad8","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","vendor":"bedrock",
	   "request.model":"anthropic.claude-3","duration":12.5,"error":true,"llm.conversation_id":"conv-1"}],
	  [{"type":"Purchase","timestamp":1700000000000},{"amount":3}]]]`
	traces, err = relay.Decode(newrelic{}, nrRequest(t, "method=custom_event_data", node, nil))
	if err != nil {
		t.Fatal(err)
	}
	spans = nrSpansOf(traces)
	if len(spans) != 1 {
		t.Fatalf("%d spans", len(spans))
	}
	chat := spans[0]
	if nrStr(chat, "gen_ai.provider.name") != "aws.bedrock" || chat.Status.Code != otlp.StatusError ||
		nrStr(chat, "newrelic.llm.conversation_id") != "conv-1" || chat.EndTimeUnixNano-chat.StartTimeUnixNano != 12_500_000 {
		t.Errorf("%+v", chat)
	}
	if _, ok := nrAttr(chat, "gen_ai.usage.input_tokens"); ok {
		t.Error("a token count the event does not have is written")
	}
	if chat.ParentSpanID.String() != "53995c3f42cd8ad8" || chat.SpanID != otlp.SpanIDOf("newrelic-LlmChatCompletionSummary", "c1") {
		t.Errorf("parent %s, id %s", chat.ParentSpanID, chat.SpanID)
	}
}

// A summary of LangChain's own (virtual_llm) is a chain, not a model call: no GenAI key, its messages withheld.
func TestNewRelicAChainIsNotAModelCall(t *testing.T) {
	body := `["run",[[{"type":"LlmChatCompletionSummary","timestamp":1700000000000},
	  {"id":"v1","span_id":"53995c3f42cd8ad8","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","vendor":"langchain",
	   "virtual_llm":true,"request.model":"gpt-4o"}],
	  [{"type":"LlmChatCompletionMessage","timestamp":1700000000000},
	  {"id":"m1","completion_id":"v1","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","role":"user","content":"hi","sequence":0,"virtual_llm":true}]]]`
	traces, err := relay.Decode(newrelic{}, nrRequest(t, "method=custom_event_data", body, nil))
	if err != nil {
		t.Fatal(err)
	}
	spans := nrSpansOf(traces)
	if len(spans) != 1 {
		t.Fatalf("%d spans", len(spans))
	}
	for _, kv := range spans[0].Attributes {
		if strings.HasPrefix(kv.Key, "gen_ai.") {
			t.Errorf("%s on a chain", kv.Key)
		}
	}
	if !strings.Contains(nrStr(spans[0], "newrelic.content.input.messages"), `"hi"`) {
		t.Errorf("%+v", spans[0].Attributes)
	}
}

// A search whose results came in another call still says how many documents it returned; none is a count of zero.
func TestNewRelicCountsTheDocumentsOfASearch(t *testing.T) {
	for want, n := range map[string]string{"[{},{}]": "2", "[]": "0"} {
		body := `["run",{},[[{"type":"LlmVectorSearch","timestamp":1700000000000},
		  {"id":"s1","span_id":"53995c3f42cd8ad8","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","request.k":4,
		   "response.number_of_documents":` + n + `}]]]`
		traces, err := relay.Decode(newrelic{}, nrRequest(t, "method=custom_event_data", body, nil))
		if err != nil {
			t.Fatal(err)
		}
		if got := nrStr(nrSpansOf(traces)[0], "gen_ai.retrieval.documents"); got != want {
			t.Errorf("%s documents: %s", n, got)
		}
	}
}

// The recordings were made with the planted license key, in the query of every call, and it reaches no output.
func TestNewRelicRecordingsCarryThePlantedKey(t *testing.T) {
	for _, name := range []string{"python", "nodejs"} {
		data, err := os.ReadFile("testdata/newrelic/" + name + ".recording.json")
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Contains(data, []byte("license_key="+nrPlantedLicense)) {
			t.Errorf("%s: the recording does not carry the planted key", name)
		}
		out, err := otlp.Marshal(nrDecodeAll(t, name))
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(out, []byte(nrPlantedLicense)) || bytes.Contains(out, []byte("recording-run-1")) {
			t.Errorf("%s: the output holds the license key or the run id", name)
		}
	}
}
