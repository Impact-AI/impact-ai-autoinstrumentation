// SPDX-License-Identifier: Apache-2.0

// The Zipkin source: any tracer that reports to a Zipkin-compatible backend in Zipkin's v2 JSON format, the generic
// vendor protocol the relay is proven with.
//
// A tracer POSTs a JSON list of spans to /api/v2/spans (https://zipkin.io/zipkin-api/#/default/post_spans), gzip
// compressed or not. Every other call (the UI's and the query API's reads, the protobuf form of the same route) is
// relayed and not decoded; a protobuf body on the spans route is reported as unreadable.
//
// The translation follows OpenTelemetry Collector's Zipkin receiver (receiver/zipkinreceiver, translator/zipkin/zipkinv2
// in opentelemetry-collector-contrib, Apache-2.0): a 64-bit trace id is widened with zero high bits, the span kinds map
// one to one, the local endpoint's service name is the resource's service.name, the "error" tag sets an error status,
// and annotations become events. Tags keep their names when impact.ai already withholds them as content or they are
// OpenTelemetry semantic conventions, and go under "zipkin." otherwise; a tag whose name says it can hold content goes
// under "zipkin.content.", which impact.ai withholds in Metadata only mode. A key is written once per span.

package source

import (
	"cmp"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"slices"
	"strconv"
	"strings"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// zipkinPath is the route a tracer reports spans to. A relay may be mounted under a prefix of the vendor's, so the
// path is matched at its end.
const zipkinPath = "/api/v2/spans"

type zipkin struct{}

func (zipkin) Source() string { return "zipkin" }

// Name is bumped when the output for the same input changes.
func (zipkin) Name() string { return "zipkin/1" }

// Headers: the decoder reads no header beyond the content type and encoding the relay always gives it.
func (zipkin) Headers() []string { return nil }

// Match claims the span reports; every other call is only relayed.
func (zipkin) Match(method, path, _ string) bool {
	return method == http.MethodPost && strings.HasSuffix(path, zipkinPath)
}

type zipkinEndpoint struct {
	ServiceName string `json:"serviceName"`
	IPv4        string `json:"ipv4"`
	IPv6        string `json:"ipv6"`
	Port        int64  `json:"port"`
}

type zipkinAnnotation struct {
	Timestamp uint64 `json:"timestamp"`
	Value     string `json:"value"`
}

type zipkinSpan struct {
	TraceID        string             `json:"traceId"`
	ParentID       string             `json:"parentId"`
	ID             string             `json:"id"`
	Kind           string             `json:"kind"`
	Name           string             `json:"name"`
	Timestamp      uint64             `json:"timestamp"`
	Duration       uint64             `json:"duration"`
	LocalEndpoint  *zipkinEndpoint    `json:"localEndpoint"`
	RemoteEndpoint *zipkinEndpoint    `json:"remoteEndpoint"`
	Tags           map[string]string  `json:"tags"`
	Annotations    []zipkinAnnotation `json:"annotations"`
}

// Decode turns a span list into OTLP spans, one resource per local service in the order they first appear. A span
// without readable ids or a timestamp makes the whole payload unreadable: the vendor has it, and the copy is reported
// as partial rather than missing spans without a trace.
func (zipkin) Decode(req relay.Request) (otlp.Traces, error) {
	if req.ContentType != "application/json" {
		return otlp.Traces{}, relay.Unreadable("the content type is not Zipkin v2 JSON", nil)
	}
	var spans []zipkinSpan
	if err := json.Unmarshal(req.Body, &spans); err != nil {
		return otlp.Traces{}, relay.Unreadable("not a Zipkin v2 JSON span list", nil)
	}
	var services []string
	byService := map[string][]otlp.Span{}
	for i, z := range spans {
		s, err := zipkinToOTLP(z)
		if err != nil {
			return otlp.Traces{}, relay.Unreadable(fmt.Sprintf("span %d: %s", i, err), nil)
		}
		service := ""
		if z.LocalEndpoint != nil {
			service = z.LocalEndpoint.ServiceName
		}
		if _, seen := byService[service]; !seen {
			services = append(services, service)
		}
		byService[service] = append(byService[service], s)
	}
	var out otlp.Traces
	for _, service := range services {
		out.ResourceSpans = append(out.ResourceSpans, otlp.ResourceSpans{
			Resource:   otlp.Resource{Attributes: otlp.Attributes{otlp.Str("service.name", cmp.Or(service, "unknown_service"))}},
			ScopeSpans: []otlp.ScopeSpans{{Scope: otlp.Scope{Name: "zipkin"}, Spans: byService[service]}},
		})
	}
	return out, nil
}

var zipkinKinds = map[string]otlp.SpanKind{
	"SERVER": otlp.SpanKindServer, "CLIENT": otlp.SpanKindClient,
	"PRODUCER": otlp.SpanKindProducer, "CONSUMER": otlp.SpanKindConsumer,
}

func zipkinToOTLP(z zipkinSpan) (otlp.Span, error) {
	traceID := z.TraceID
	if len(traceID) == 16 {
		traceID = strings.Repeat("0", 16) + traceID
	}
	tid, err := otlp.ParseTraceID(strings.ToLower(traceID))
	if err != nil || len(traceID) != 32 || tid.IsZero() {
		return otlp.Span{}, fmt.Errorf("the trace id is not 16 or 32 hex digits")
	}
	sid, err := otlp.ParseSpanID(strings.ToLower(z.ID))
	if err != nil || len(z.ID) != 16 || sid.IsZero() {
		return otlp.Span{}, fmt.Errorf("the span id is not 16 hex digits")
	}
	if z.Timestamp == 0 {
		return otlp.Span{}, fmt.Errorf("no timestamp")
	}
	s := otlp.Span{
		TraceID:           tid,
		SpanID:            sid,
		Name:              z.Name,
		Kind:              cmp.Or(zipkinKinds[z.Kind], otlp.SpanKindInternal),
		StartTimeUnixNano: otlp.Nanos(z.Timestamp * 1000),
		EndTimeUnixNano:   otlp.Nanos((z.Timestamp + z.Duration) * 1000),
	}
	if z.ParentID != "" {
		parent, err := otlp.ParseSpanID(strings.ToLower(z.ParentID))
		if err != nil || len(z.ParentID) != 16 {
			return otlp.Span{}, fmt.Errorf("the parent id is not 16 hex digits")
		}
		s.ParentSpanID = parent
	}
	// Each attribute key is written once: a tag that already uses a key's current name wins over an older name renamed
	// onto it, and a tag over what the remote endpoint says. impact.ai refuses a span that repeats a key.
	seen := map[string]bool{}
	add := func(kv otlp.KeyValue) {
		if !seen[kv.Key] {
			seen[kv.Key] = true
			s.Attributes = append(s.Attributes, kv)
		}
	}
	tags := slices.Sorted(maps.Keys(z.Tags))
	for _, renamed := range []bool{false, true} {
		for _, key := range tags {
			value := z.Tags[key]
			if key == "error" {
				if !renamed {
					s.Status = otlp.Status{Code: otlp.StatusError, Message: value}
				}
				continue
			}
			if _, ok := zipkinReplaced[key]; ok == renamed {
				add(zipkinTag(zipkinAttribute(key), value))
			}
		}
	}
	if r := z.RemoteEndpoint; r != nil {
		if r.ServiceName != "" {
			add(otlp.Str("peer.service", r.ServiceName))
		}
		if addr := cmp.Or(r.IPv4, r.IPv6); addr != "" {
			add(otlp.Str("network.peer.address", addr))
		}
		if r.Port > 0 {
			add(otlp.Int("network.peer.port", r.Port))
		}
	}
	for _, a := range z.Annotations {
		s.Events = append(s.Events, otlp.Event{TimeUnixNano: otlp.Nanos(a.Timestamp * 1000), Name: a.Value})
	}
	return s, nil
}

// zipkinSemconv are the namespaces of OpenTelemetry semantic conventions whose tags keep their names: a tracer that
// reports OpenTelemetry spans in Zipkin's format writes its attributes as tags.
var zipkinSemconv = []string{
	"gen_ai.", "http.", "url.", "server.", "client.", "network.", "db.", "rpc.", "messaging.", "error.", "exception.",
	"user_agent.", "code.", "thread.", "otel.",
}

// zipkinReplaced are GenAI keys an older tracer may still write, in their current spelling.
var zipkinReplaced = map[string]string{
	"gen_ai.system":                  "gen_ai.provider.name",
	"gen_ai.usage.prompt_tokens":     "gen_ai.usage.input_tokens",
	"gen_ai.usage.completion_tokens": "gen_ai.usage.output_tokens",
}

// zipkinNumbers are the GenAI keys whose values are numbers: Zipkin's tags are strings, OpenTelemetry's are typed.
var zipkinNumbers = map[string]bool{
	"gen_ai.usage.input_tokens": true, "gen_ai.usage.output_tokens": true, "gen_ai.request.max_tokens": true,
	"gen_ai.request.temperature": false, "gen_ai.request.top_p": false, "gen_ai.request.top_k": false,
	"gen_ai.request.frequency_penalty": false, "gen_ai.request.presence_penalty": false,
}

// zipkinTag is a tag as an attribute, a number where the key's convention says so and the value is one.
func zipkinTag(key, value string) otlp.KeyValue {
	integer, numeric := zipkinNumbers[key]
	if numeric && integer {
		if n, err := strconv.ParseInt(value, 10, 64); err == nil {
			return otlp.Int(key, n)
		}
	} else if numeric {
		if f, err := strconv.ParseFloat(value, 64); err == nil {
			return otlp.Double(key, f)
		}
	}
	return otlp.Str(key, value)
}

// zipkinContentWords are parts of tag names that say the tag can hold a prompt, a reply, a tool's arguments or result, a
// document or a message: such a tag goes under "zipkin.content.", which impact.ai withholds in Metadata only mode. A
// name is only a hint, so the list leans towards withholding.
var zipkinContentWords = []string{
	"prompt", "completion", "message", "msg", "content", "input", "output", "query", "document", "response", "request",
	"result", "argument", "value", "template", "variable", "transcript", "function_call", "text", "body", "answer",
	"question", "instruction", "chunk", "data",
}

// zipkinAttribute is the attribute a tag becomes. A tag that impact.ai already withholds as content in Metadata only
// mode (an OpenTelemetry SDK's Zipkin exporter writes every attribute as a tag: input.value, traceloop.entity.input,
// tool_arguments) keeps its name, so it is withheld as it would be over OTLP; it is never renamed to a key that is not.
func zipkinAttribute(tag string) string {
	if current, ok := zipkinReplaced[tag]; ok {
		return current
	}
	if intakeWithholds(tag) {
		return tag
	}
	for _, prefix := range zipkinSemconv {
		if strings.HasPrefix(tag, prefix) {
			return tag
		}
	}
	lower := strings.ToLower(tag)
	for _, word := range zipkinContentWords {
		if strings.Contains(lower, word) {
			return "zipkin.content." + tag
		}
	}
	return "zipkin." + tag
}
