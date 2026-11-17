// SPDX-License-Identifier: Apache-2.0

package source

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"math"
	"slices"
	"strconv"
	"strings"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
)

// newrelicSpanKeys are the span event attributes that already have an OpenTelemetry semantic convention outside GenAI,
// under its current name. Every other attribute is kept under "newrelic.".
var newrelicSpanKeys = map[string]string{
	"http.method":     "http.request.method",
	"http.url":        "url.full",
	"http.statusCode": "http.response.status_code",
	"server.address":  "server.address",
	"server.port":     "server.port",
	"db.statement":    "db.query.text",
	"db.instance":     "db.namespace",
	"db.system":       "db.system.name",
	"db.collection":   "db.collection.name",
	"db.operation":    "db.operation.name",
	"error.class":     "error.type",
	"code.filepath":   "code.file.path",
	"code.lineno":     "code.line.number",
	"code.column":     "code.column.number",
	"code.function":   "code.function.name",
}

// newrelicSpanContent are span event attributes that can hold what a user wrote: they go under "newrelic.content.".
var newrelicSpanContent = map[string]bool{"error.message": true}

// newrelicSpanStructure are the attributes that make the span itself rather than an attribute of it. The Node.js
// agent's nr_exclusive_duration_millis is its bookkeeping of the time spent in the span and not in its children, which
// the spans' own times give.
var newrelicSpanStructure = map[string]bool{
	"type": true, "traceId": true, "guid": true, "parentId": true, "name": true, "timestamp": true, "duration": true,
	"span.kind": true, "nr_exclusive_duration_millis": true,
}

var newrelicKinds = map[string]otlp.SpanKind{
	"client":   otlp.SpanKindClient,
	"server":   otlp.SpanKindServer,
	"producer": otlp.SpanKindProducer,
	"consumer": otlp.SpanKindConsumer,
	"internal": otlp.SpanKindInternal,
}

// newrelicSpans decodes the span events of a span_event_data call, in their order. An event without a valid trace or
// span id is left out.
func newrelicSpans(events []newrelicEvent) []otlp.Span {
	var spans []otlp.Span
	for _, event := range events {
		in := event.intrinsics
		if t, _ := in["type"].(string); t != "" && t != "Span" {
			continue
		}
		traceID, ok := newrelicTraceID(nrText(in["traceId"]))
		if !ok {
			continue
		}
		spanID, ok := newrelicSpanID(nrText(in["guid"]))
		if !ok {
			continue
		}
		span := otlp.Span{TraceID: traceID, SpanID: spanID, Name: nrText(in["name"]), Kind: otlp.SpanKindInternal}
		if parent, ok := newrelicSpanID(nrText(in["parentId"])); ok {
			span.ParentSpanID = parent
		}
		if kind, ok := newrelicKinds[strings.ToLower(nrText(in["span.kind"]))]; ok {
			span.Kind = kind
		} else if entry, _ := in["nr.entryPoint"].(bool); entry {
			span.Kind = otlp.SpanKindServer
		}
		span.StartTimeUnixNano = nrMillis(in["timestamp"])
		span.EndTimeUnixNano = span.StartTimeUnixNano + nrSeconds(in["duration"])

		attrs := newrelicAttributes{}
		for i, m := range []map[string]any{in, event.agent, event.user} {
			for _, key := range nrSortedKeys(m) {
				if (i == 0 || key == "nr_exclusive_duration_millis") && newrelicSpanStructure[key] {
					continue
				}
				switch {
				case newrelicSpanKeys[key] != "":
					attrs.add(newrelicSpanKeys[key], m[key])
				case newrelicSpanContent[key]:
					attrs.add("newrelic.content."+key, m[key])
				default:
					attrs.add("newrelic."+key, m[key])
				}
			}
		}
		if _, failed := event.agent["error.class"]; failed {
			span.Status = otlp.Status{Code: otlp.StatusError}
		}
		span.Attributes = attrs.list
		spans = append(spans, span)
	}
	return spans
}

// newrelicAttributes collects attributes; the first value of a key wins (intrinsics, then agent, then user
// attributes), and a value that is null is not written.
type newrelicAttributes struct {
	list otlp.Attributes
	seen map[string]bool
}

func (a *newrelicAttributes) add(key string, v any) {
	value, ok := nrValue(v)
	if !ok {
		return
	}
	a.put(otlp.KV(key, value))
}

func (a *newrelicAttributes) put(kv otlp.KeyValue) {
	if a.seen == nil {
		a.seen = map[string]bool{}
	}
	if a.seen[kv.Key] {
		return
	}
	a.seen[kv.Key] = true
	a.list = append(a.list, kv)
}

// newrelicTraceID reads a trace id of up to 32 hex digits: the W3C trace id the agents write, or an older agent's
// 16-digit one, widened with a high half of zero. An empty or all-zero id is not valid.
func newrelicTraceID(s string) (otlp.TraceID, bool) {
	var id otlp.TraceID
	if !nrHexID(s, id[:]) || id.IsZero() {
		return otlp.TraceID{}, false
	}
	return id, true
}

// newrelicSpanID reads a span id of up to 16 hex digits.
func newrelicSpanID(s string) (otlp.SpanID, bool) {
	var id otlp.SpanID
	if !nrHexID(s, id[:]) || id.IsZero() {
		return otlp.SpanID{}, false
	}
	return id, true
}

// hexID reads up to 2*len(out) hex digits, right-aligned, into out.
func nrHexID(s string, out []byte) bool {
	s = strings.ToLower(strings.TrimSpace(s))
	if s == "" || len(s) > 2*len(out) {
		return false
	}
	s = strings.Repeat("0", 2*len(out)-len(s)) + s
	_, err := hex.Decode(out, []byte(s))
	return err == nil
}

// text is a string value, or the text of a number; anything else is empty.
func nrText(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case json.Number:
		return x.String()
	}
	return ""
}

// number is a numeric value, read from a number or a numeric string.
func nrNumber(v any) (float64, bool) {
	switch x := v.(type) {
	case json.Number:
		f, err := x.Float64()
		return f, err == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
	}
	return 0, false
}

// integer is a whole number value.
func nrInteger(v any) (int64, bool) {
	if n, ok := v.(json.Number); ok {
		if i, err := n.Int64(); err == nil {
			return i, true
		}
	}
	f, ok := nrNumber(v)
	if !ok || f != math.Trunc(f) || math.Abs(f) > 1<<53 {
		return 0, false
	}
	return int64(f), true
}

// millis is a time in milliseconds since the epoch, in nanoseconds.
func nrMillis(v any) otlp.Nanos {
	if i, ok := nrInteger(v); ok && i > 0 {
		return otlp.Nanos(i) * 1_000_000
	}
	if f, ok := nrNumber(v); ok && f > 0 {
		return otlp.Nanos(math.Round(f * 1e6))
	}
	return 0
}

// seconds is a duration in seconds, in nanoseconds.
func nrSeconds(v any) otlp.Nanos {
	if f, ok := nrNumber(v); ok && f > 0 {
		return otlp.Nanos(math.Round(f * 1e9))
	}
	return 0
}

// milliseconds is a duration in milliseconds, in nanoseconds.
func nrMilliseconds(v any) otlp.Nanos {
	if f, ok := nrNumber(v); ok && f > 0 {
		return otlp.Nanos(math.Round(f * 1e6))
	}
	return 0
}

// anyValue is an attribute's value: a string, a bool, an integer or a double as it is, an array of those as an array,
// and anything nested as its JSON. A null is no value.
func nrValue(v any) (otlp.AnyValue, bool) {
	switch x := v.(type) {
	case nil:
		return otlp.AnyValue{}, false
	case string:
		return otlp.StrValue(x), true
	case bool:
		return otlp.BoolValue(x), true
	case json.Number:
		if !strings.ContainsAny(x.String(), ".eE") {
			if i, err := x.Int64(); err == nil {
				return otlp.IntValue(i), true
			}
		}
		if f, err := x.Float64(); err == nil {
			return otlp.DoubleValue(f), true
		}
		return otlp.StrValue(x.String()), true
	case []any:
		values := make([]otlp.AnyValue, 0, len(x))
		for _, item := range x {
			switch item.(type) {
			case string, bool, json.Number:
				value, _ := nrValue(item)
				values = append(values, value)
			default:
				return otlp.StrValue(nrJSON(x)), true
			}
		}
		return otlp.ArrayOf(values...), true
	}
	return otlp.StrValue(nrJSON(v)), true
}

// jsonText is v as compact JSON, its object keys sorted and nothing HTML-escaped.
func nrJSON(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return ""
	}
	return strings.TrimSuffix(buf.String(), "\n")
}

func nrSortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}
