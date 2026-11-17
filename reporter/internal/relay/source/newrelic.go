// SPDX-License-Identifier: Apache-2.0

// The New Relic source: New Relic's APM agents (Python, Node.js, Java and the others) and their AI monitoring.
//
// The agents speak New Relic's agent protocol: every call is a POST (or a PUT) to /agent_listener/invoke_raw_method,
// the name of the call in the query (method=span_event_data and so on), the body JSON, gzip or deflate compressed above
// a size. Two calls carry what is decoded:
//
//   - span_event_data: [run id, {reservoir_size, events_seen}, [[intrinsics, user attributes, agent attributes], ...]],
//     one span event per span of a sampled transaction, with its W3C trace id (traceId), its own id (guid) and its
//     parent's (parentId).
//   - custom_event_data: [run id, {reservoir_size, events_seen}, [[intrinsics, attributes], ...]] (the Node.js agent
//     leaves out the object). AI monitoring writes its events here: LlmChatCompletionSummary with its
//     LlmChatCompletionMessage events (joined by completion_id), LlmEmbedding, LlmTool, LlmAgent, and LlmVectorSearch
//     with its LlmVectorSearchResult events (joined by search_id). Each names its trace (trace_id) and the span it was
//     recorded in (span_id); content (prompts, replies, tool input and output, the query and the documents) is on by
//     default (ai_monitoring.record_content.enabled).
//
// Every other call (preconnect, connect, metric_data, analytic_event_data, ...) is relayed and not decoded.
//
// A span event becomes a span with its own ids. An AI monitoring event becomes a span of its own, a child of the span
// it names, with an id derived from the event's own: span events are sent only for sampled transactions and AI events
// for all, in different calls, so neither can stand in for the other. Messages go into their summary's span and
// results into their search's; one whose summary or search is not in the same call is not decoded (the agents record
// them together). The format is the one the agents' open-source code writes (Apache-2.0): newrelic 14.2.0 for Python
// (newrelic/core/data_collector.py, newrelic/hooks/mlmodel_openai.py, newrelic/hooks/mlmodel_langchain.py) and
// newrelic 14.11.0 for Node.js (lib/collector/, lib/llm-events/).

package source

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// newrelicPath is the one path of the agent protocol; the call is named in the query.
const newrelicPath = "/agent_listener/invoke_raw_method"

// The calls that carry spans and AI monitoring events.
const (
	newrelicSpanEvents   = "span_event_data"
	newrelicCustomEvents = "custom_event_data"
)

type newrelic struct{}

func (newrelic) Source() string { return "newrelic" }

// Name is bumped when the output for the same input changes.
func (newrelic) Name() string { return "newrelic/1" }

// Headers: the agent's name and version, which say the language.
func (newrelic) Headers() []string { return []string{"User-Agent"} }

// Match claims the two calls that carry spans and AI monitoring events, named in the query
// (method=span_event_data, method=custom_event_data); every other call is only relayed.
func (newrelic) Match(method, path, query string) bool {
	if (method != http.MethodPost && method != http.MethodPut) || path != newrelicPath {
		return false
	}
	call := newrelicCall(query)
	return call == newrelicSpanEvents || call == newrelicCustomEvents
}

// newrelicCall is the name of the call in a query: its method parameter.
func newrelicCall(query string) string {
	values, _ := url.ParseQuery(query) // what can be read of it
	return values.Get("method")
}

func (newrelic) Decode(req relay.Request) (otlp.Traces, error) {
	query, _ := url.ParseQuery(req.RawQuery) // what can be read of it; the relay already took out the credentials
	call := query.Get("method")
	if call != newrelicSpanEvents && call != newrelicCustomEvents {
		return otlp.Traces{}, nil // a call Match does not claim
	}
	if format := query.Get("marshal_format"); format != "" && format != "json" {
		return otlp.Traces{}, relay.Unreadable("the marshal format is not json", nil)
	}
	events, err := newrelicEvents(req.Body)
	if err != nil {
		return otlp.Traces{}, err
	}
	var spans []otlp.Span
	if call == newrelicSpanEvents {
		spans = newrelicSpans(events)
	} else {
		spans = newrelicAISpans(events)
	}
	if len(spans) == 0 {
		return otlp.Traces{}, nil
	}
	sdk, version := newrelicAgentOf(req.Header.Get("User-Agent"))
	resource := otlp.Attributes{otlp.Str("telemetry.sdk.name", "newrelic")}
	if sdk != "" {
		resource = append(resource, otlp.Str("telemetry.sdk.language", sdk))
	}
	if version != "" {
		resource = append(resource, otlp.Str("telemetry.sdk.version", version))
	}
	return otlp.Traces{ResourceSpans: []otlp.ResourceSpans{{
		Resource:   otlp.Resource{Attributes: resource},
		ScopeSpans: []otlp.ScopeSpans{{Scope: otlp.Scope{Name: "newrelic", Version: version}, Spans: spans}},
	}}}, nil
}

// newrelicEvent is one event of a call: its intrinsics and its attributes, user and agent attributes as the agent
// sent them (custom events have one map of attributes, kept as user).
type newrelicEvent struct {
	intrinsics, user, agent map[string]any
}

// newrelicEvents reads the events of a span_event_data or custom_event_data body: the last element of the array.
func newrelicEvents(body []byte) ([]newrelicEvent, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var payload []any
	if err := dec.Decode(&payload); err != nil {
		return nil, relay.Unreadable("the body is not a JSON array", err)
	}
	if len(payload) < 2 {
		return nil, relay.Unreadable("the body has no events", nil)
	}
	list, ok := payload[len(payload)-1].([]any)
	if !ok {
		return nil, relay.Unreadable("the body's events are not an array", nil)
	}
	events := make([]newrelicEvent, 0, len(list))
	for _, item := range list {
		parts, ok := item.([]any)
		if !ok || len(parts) == 0 {
			continue
		}
		event := newrelicEvent{intrinsics: nrObject(parts[0])}
		if event.intrinsics == nil {
			continue
		}
		if len(parts) > 1 {
			event.user = nrObject(parts[1])
		}
		if len(parts) > 2 {
			event.agent = nrObject(parts[2])
		}
		events = append(events, event)
	}
	return events, nil
}

func nrObject(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

// newrelicAgents are the agents' names in their User-Agent header and the language each says.
var newrelicAgents = map[string]string{
	"NewRelic-PythonAgent": "python",
	"NewRelic-NodeAgent":   "nodejs",
	"NewRelic-JavaAgent":   "java",
	"NewRelic-RubyAgent":   "ruby",
	"NewRelic-DotNetAgent": "dotnet",
	"NewRelic-Go-Agent":    "go",
	"NewRelic-PHPAgent":    "php",
}

// newrelicAgentOf is the language and the agent's version a User-Agent header names, such as
// "NewRelic-PythonAgent/14.2.0 (Python 3.12.3 linux)".
func newrelicAgentOf(userAgent string) (language, version string) {
	product, _, _ := strings.Cut(strings.TrimSpace(userAgent), " ")
	name, version, _ := strings.Cut(product, "/")
	language, ok := newrelicAgents[name]
	if !ok {
		return "", ""
	}
	if !nrVersion(version) {
		version = ""
	}
	return language, version
}

func nrVersion(s string) bool {
	if s == "" || len(s) > 40 {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r == '.' || r == '-' || r == '+' || r == '_') {
			return false
		}
	}
	return true
}
