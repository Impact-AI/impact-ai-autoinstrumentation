// SPDX-License-Identifier: Apache-2.0

package source

import (
	"cmp"
	"slices"
	"strings"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
)

// The AI monitoring events.
const (
	nrSummary      = "LlmChatCompletionSummary"
	nrMessage      = "LlmChatCompletionMessage"
	nrEmbedding    = "LlmEmbedding"
	nrTool         = "LlmTool"
	nrAgent        = "LlmAgent"
	nrSearch       = "LlmVectorSearch"
	nrSearchResult = "LlmVectorSearchResult"
)

// nrProviders are the GenAI provider names of the vendors whose models the agents instrument. Any other vendor is a
// framework (langchain, langgraph, spring-ai, strands, ...), kept as newrelic.vendor.
var nrProviders = map[string]string{
	"openai":    "openai",
	"anthropic": "anthropic",
	"bedrock":   "aws.bedrock",
	"gemini":    "gcp.gemini",
}

// nrStructure are the attributes of an AI event that make its span, or join it to another event.
var nrStructure = map[string]bool{
	"id": true, "span_id": true, "trace_id": true, "timestamp": true, "duration": true, "error": true,
}

// nrRecord is one AI monitoring event: its type, its intrinsics and its attributes.
type nrRecord struct {
	kind       string
	intrinsics map[string]any
	attrs      map[string]any
}

// newrelicAISpans decodes the AI monitoring events of a custom_event_data call, in their order. Every other custom
// event (the app's own, or AI events not decoded yet such as LlmFeedbackMessage) is left out.
func newrelicAISpans(events []newrelicEvent) []otlp.Span {
	records := make([]nrRecord, 0, len(events))
	messages := map[string][]nrRecord{} // by completion_id
	results := map[string][]nrRecord{}  // by search_id
	for _, event := range events {
		kind := nrText(event.intrinsics["type"])
		attrs := map[string]any{}
		for _, m := range []map[string]any{event.agent, event.user} {
			for key, value := range m {
				if _, taken := attrs[key]; !taken {
					attrs[key] = value
				}
			}
		}
		record := nrRecord{kind: kind, intrinsics: event.intrinsics, attrs: attrs}
		switch kind {
		case nrMessage:
			id := nrText(attrs["completion_id"])
			messages[id] = append(messages[id], record)
		case nrSearchResult:
			id := nrText(attrs["search_id"])
			results[id] = append(results[id], record)
		}
		records = append(records, record)
	}

	var spans []otlp.Span
	for _, r := range records {
		var span otlp.Span
		var ok bool
		switch r.kind {
		case nrSummary:
			span, ok = nrSummarySpan(r, nrSequenced(messages[nrText(r.attrs["id"])]))
		case nrEmbedding:
			span, ok = nrEmbeddingSpan(r)
		case nrTool:
			span, ok = nrToolSpan(r)
		case nrAgent:
			span, ok = nrAgentSpan(r)
		case nrSearch:
			span, ok = nrSearchSpan(r, nrSequenced(results[nrText(r.attrs["id"])]))
		}
		if ok {
			spans = append(spans, span)
		}
	}
	return spans
}

// nrSequenced sorts messages or results by their sequence, keeping the order of the call between equals.
func nrSequenced(records []nrRecord) []nrRecord {
	sorted := slices.Clone(records)
	slices.SortStableFunc(sorted, func(a, b nrRecord) int {
		x, _ := nrInteger(a.attrs["sequence"])
		y, _ := nrInteger(b.attrs["sequence"])
		return cmp.Compare(x, y)
	})
	return sorted
}

// nrBase is the span of an AI event without its meaning: a child of the span the event names, in the event's trace,
// with an id derived from the event's own id, its time and its outcome. It is not valid without a trace id.
func nrBase(r nrRecord, name string, kind otlp.SpanKind) (otlp.Span, *newrelicAttributes, bool) {
	traceID, ok := newrelicTraceID(nrText(r.attrs["trace_id"]))
	if !ok {
		return otlp.Span{}, nil, false
	}
	id := nrText(r.attrs["id"])
	if id == "" {
		id = nrJSON(r.attrs)
	}
	_, spanID := otlp.IDsOf("newrelic-"+r.kind, id)
	span := otlp.Span{TraceID: traceID, SpanID: spanID, Name: name, Kind: kind}
	if parent, ok := newrelicSpanID(nrText(r.attrs["span_id"])); ok {
		span.ParentSpanID = parent
	}
	span.StartTimeUnixNano = nrMillis(r.intrinsics["timestamp"])
	if span.StartTimeUnixNano == 0 {
		span.StartTimeUnixNano = nrMillis(r.attrs["timestamp"])
	}
	span.EndTimeUnixNano = span.StartTimeUnixNano + nrMilliseconds(r.attrs["duration"])
	if failed, _ := r.attrs["error"].(bool); failed {
		span.Status = otlp.Status{Code: otlp.StatusError}
	}
	attrs := &newrelicAttributes{}
	attrs.put(otlp.Str("newrelic.event.type", r.kind))
	if id := nrText(r.attrs["id"]); id != "" {
		attrs.put(otlp.Str("newrelic.event.id", id))
	}
	return span, attrs, true
}

// nrRest keeps every attribute of the event that nothing else took, under "newrelic.": the decoder writes only the
// GenAI keys the vocabulary lists (a tool's description and a request's temperature have none there yet), and the
// app's own llm.* metadata becomes newrelic.llm.*.
func nrRest(r nrRecord, attrs *newrelicAttributes, taken ...string) {
	for _, key := range nrSortedKeys(r.attrs) {
		if nrStructure[key] || slices.Contains(taken, key) {
			continue
		}
		attrs.add("newrelic."+key, r.attrs[key])
	}
}

// nrProvider writes the provider of a model event, or keeps a framework's name.
func nrProvider(r nrRecord, attrs *newrelicAttributes) {
	vendor := nrText(r.attrs["vendor"])
	if provider, ok := nrProviders[strings.ToLower(vendor)]; ok {
		attrs.put(otlp.Str("gen_ai.provider.name", provider))
		return
	}
	attrs.add("newrelic.vendor", r.attrs["vendor"])
}

// nrMessageJSON is the GenAI JSON of messages: [{"role", "parts": [{"type": "text", "content"}]}], a tool's message
// as a tool_call_response part; a message without content has no parts. Output messages carry the finish reason.
func nrMessageJSON(records []nrRecord, finish string) string {
	type part struct {
		Type     string `json:"type"`
		Content  any    `json:"content,omitempty"`
		Response any    `json:"response,omitempty"`
	}
	type message struct {
		Role         string `json:"role"`
		Parts        []part `json:"parts"`
		FinishReason string `json:"finish_reason,omitempty"`
	}
	out := make([]message, 0, len(records))
	for _, r := range records {
		m := message{Role: nrText(r.attrs["role"]), Parts: []part{}, FinishReason: finish}
		if content, ok := r.attrs["content"]; ok && content != nil && content != "" {
			if m.Role == "tool" {
				m.Parts = append(m.Parts, part{Type: "tool_call_response", Response: content})
			} else {
				m.Parts = append(m.Parts, part{Type: "text", Content: content})
			}
		}
		out = append(out, m)
	}
	return nrJSON(out)
}

// nrSummarySpan is a chat: LlmChatCompletionSummary with its messages. A summary of LangChain's own (virtual_llm), the
// run of a chain rather than a call of a model, keeps everything, its messages too, under "newrelic.".
func nrSummarySpan(r nrRecord, messages []nrRecord) (otlp.Span, bool) {
	var input, output []nrRecord
	for _, m := range messages {
		if response, _ := m.attrs["is_response"].(bool); response {
			output = append(output, m)
		} else {
			input = append(input, m)
		}
	}
	finish := nrText(r.attrs["response.choices.finish_reason"])
	if virtual, _ := r.attrs["virtual_llm"].(bool); virtual {
		span, attrs, ok := nrBase(r, "chain", otlp.SpanKindInternal)
		if !ok {
			return otlp.Span{}, false
		}
		if len(input) > 0 {
			attrs.put(otlp.Str("newrelic.content.input.messages", nrMessageJSON(input, "")))
		}
		if len(output) > 0 {
			attrs.put(otlp.Str("newrelic.content.output.messages", nrMessageJSON(output, finish)))
		}
		nrRest(r, attrs)
		span.Attributes = attrs.list
		return span, true
	}

	model := nrText(r.attrs["request.model"])
	if model == "" {
		model = nrText(r.attrs["response.model"])
	}
	span, attrs, ok := nrBase(r, strings.TrimSpace("chat "+model), otlp.SpanKindClient)
	if !ok {
		return otlp.Span{}, false
	}
	attrs.put(otlp.Str("gen_ai.operation.name", "chat"))
	nrProvider(r, attrs)
	attrs.add("gen_ai.request.model", r.attrs["request.model"])
	attrs.add("gen_ai.response.model", r.attrs["response.model"])
	attrs.add("gen_ai.request.max_tokens", r.attrs["request.max_tokens"])
	if finish != "" {
		attrs.put(otlp.Strs("gen_ai.response.finish_reasons", finish))
	}
	attrs.add("gen_ai.usage.input_tokens", r.attrs["response.usage.prompt_tokens"])
	attrs.add("gen_ai.usage.output_tokens", r.attrs["response.usage.completion_tokens"])
	if len(input) > 0 {
		attrs.put(otlp.Str("gen_ai.input.messages", nrMessageJSON(input, "")))
	}
	if len(output) > 0 {
		attrs.put(otlp.Str("gen_ai.output.messages", nrMessageJSON(output, finish)))
	}
	nrRest(r, attrs, "vendor", "request.model", "response.model", "request.max_tokens",
		"response.choices.finish_reason", "response.usage.prompt_tokens", "response.usage.completion_tokens")
	span.Attributes = attrs.list
	return span, true
}

// nrEmbeddingSpan is an embeddings call. Its input has no GenAI key and goes under "newrelic.content.".
func nrEmbeddingSpan(r nrRecord) (otlp.Span, bool) {
	model := nrText(r.attrs["request.model"])
	span, attrs, ok := nrBase(r, strings.TrimSpace("embeddings "+model), otlp.SpanKindClient)
	if !ok {
		return otlp.Span{}, false
	}
	attrs.put(otlp.Str("gen_ai.operation.name", "embeddings"))
	nrProvider(r, attrs)
	attrs.add("gen_ai.request.model", r.attrs["request.model"])
	attrs.add("gen_ai.response.model", r.attrs["response.model"])
	attrs.add("gen_ai.usage.input_tokens", r.attrs["response.usage.total_tokens"])
	attrs.add("newrelic.content.input", r.attrs["input"])
	nrRest(r, attrs, "vendor", "request.model", "response.model", "response.usage.total_tokens", "input")
	span.Attributes = attrs.list
	return span, true
}

// nrToolSpan is a tool's run: its name, the call's id, its input as the arguments and its output as the result.
func nrToolSpan(r nrRecord) (otlp.Span, bool) {
	name := nrText(r.attrs["name"])
	span, attrs, ok := nrBase(r, strings.TrimSpace("execute_tool "+name), otlp.SpanKindInternal)
	if !ok {
		return otlp.Span{}, false
	}
	attrs.put(otlp.Str("gen_ai.operation.name", "execute_tool"))
	attrs.add("gen_ai.tool.name", r.attrs["name"])
	attrs.add("gen_ai.tool.call.id", r.attrs["run_id"])
	attrs.add("gen_ai.tool.call.arguments", r.attrs["input"])
	attrs.add("gen_ai.tool.call.result", r.attrs["output"])
	nrRest(r, attrs, "name", "run_id", "input", "output")
	span.Attributes = attrs.list
	return span, true
}

// nrAgentSpan is an agent's run, by its name.
func nrAgentSpan(r nrRecord) (otlp.Span, bool) {
	name := nrText(r.attrs["name"])
	span, attrs, ok := nrBase(r, strings.TrimSpace("invoke_agent "+name), otlp.SpanKindInternal)
	if !ok {
		return otlp.Span{}, false
	}
	attrs.put(otlp.Str("gen_ai.operation.name", "invoke_agent"))
	attrs.add("gen_ai.agent.name", r.attrs["name"])
	nrRest(r, attrs, "name")
	span.Attributes = attrs.list
	return span, true
}

// nrSearchSpan is a retrieval: its query, the top-k it asked for, and the documents it returned. The documents' ids
// (their metadata.id) and their number are metadata, in gen_ai.retrieval.documents; their text and the rest of their
// metadata are content, under "newrelic.content.retrieval.documents".
func nrSearchSpan(r nrRecord, results []nrRecord) (otlp.Span, bool) {
	span, attrs, ok := nrBase(r, "retrieval", otlp.SpanKindClient)
	if !ok {
		return otlp.Span{}, false
	}
	attrs.put(otlp.Str("gen_ai.operation.name", "retrieval"))
	attrs.add("gen_ai.retrieval.query.text", r.attrs["request.query"])
	attrs.add("gen_ai.retrieval.top_k", r.attrs["request.k"])
	documents, known := []map[string]any{}, len(results) > 0
	if known {
		content := make([]map[string]any, 0, len(results))
		for _, result := range results {
			document, text := map[string]any{}, map[string]any{}
			if id := nrText(result.attrs["metadata.id"]); id != "" {
				document["id"] = id
				text["id"] = id
			}
			if page, ok := result.attrs["page_content"]; ok && page != nil {
				text["content"] = page
			}
			for key, value := range result.attrs {
				if strings.HasPrefix(key, "metadata.") && key != "metadata.id" {
					text[key] = value
				}
			}
			documents = append(documents, document)
			content = append(content, text)
		}
		attrs.put(otlp.Str("newrelic.content.retrieval.documents", nrJSON(content)))
	} else if n, ok := nrInteger(r.attrs["response.number_of_documents"]); ok && n >= 0 && n <= 10000 {
		known = true
		for range n {
			documents = append(documents, map[string]any{})
		}
	}
	if known {
		attrs.put(otlp.Str("gen_ai.retrieval.documents", nrJSON(documents)))
	}
	nrRest(r, attrs, "request.query", "request.k")
	span.Attributes = attrs.list
	return span, true
}
