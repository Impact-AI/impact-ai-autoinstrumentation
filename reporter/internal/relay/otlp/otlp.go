// SPDX-License-Identifier: Apache-2.0

// Package otlp holds the OTLP/JSON trace messages a relay decoder writes, small
// builders for attributes, deterministic ids and the provenance every relayed
// span carries.
//
// The encoding is OTLP/JSON as the OTLP specification defines it
// (https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding): the
// Protobuf JSON mapping with lowerCamelCase field names, trace and span ids as
// hex strings (not base64), enum values as integers and 64-bit integers as
// decimal strings. Standard library only.
package otlp

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
	"time"
)

// Traces is an ExportTraceServiceRequest, the body of POST /v1/traces.
type Traces struct {
	ResourceSpans []ResourceSpans `json:"resourceSpans,omitempty"`
}

// ResourceSpans is the spans of one resource.
type ResourceSpans struct {
	Resource   Resource     `json:"resource,omitzero"`
	ScopeSpans []ScopeSpans `json:"scopeSpans,omitempty"`
	SchemaURL  string       `json:"schemaUrl,omitempty"`
}

// Resource is what produced the spans: a service, a process, a host.
type Resource struct {
	Attributes             Attributes `json:"attributes,omitempty"`
	DroppedAttributesCount uint32     `json:"droppedAttributesCount,omitempty"`
}

// ScopeSpans is the spans of one instrumentation scope.
type ScopeSpans struct {
	Scope     Scope  `json:"scope,omitzero"`
	Spans     []Span `json:"spans,omitempty"`
	SchemaURL string `json:"schemaUrl,omitempty"`
}

// Scope is the instrumentation scope: the library that made the spans.
type Scope struct {
	Name                   string     `json:"name,omitempty"`
	Version                string     `json:"version,omitempty"`
	Attributes             Attributes `json:"attributes,omitempty"`
	DroppedAttributesCount uint32     `json:"droppedAttributesCount,omitempty"`
}

// Span is one operation.
type Span struct {
	TraceID                TraceID    `json:"traceId"`
	SpanID                 SpanID     `json:"spanId"`
	TraceState             string     `json:"traceState,omitempty"`
	ParentSpanID           SpanID     `json:"parentSpanId,omitzero"`
	Flags                  uint32     `json:"flags,omitempty"`
	Name                   string     `json:"name"`
	Kind                   SpanKind   `json:"kind,omitempty"`
	StartTimeUnixNano      Nanos      `json:"startTimeUnixNano"`
	EndTimeUnixNano        Nanos      `json:"endTimeUnixNano"`
	Attributes             Attributes `json:"attributes,omitempty"`
	DroppedAttributesCount uint32     `json:"droppedAttributesCount,omitempty"`
	Events                 []Event    `json:"events,omitempty"`
	DroppedEventsCount     uint32     `json:"droppedEventsCount,omitempty"`
	Links                  []Link     `json:"links,omitempty"`
	DroppedLinksCount      uint32     `json:"droppedLinksCount,omitempty"`
	Status                 Status     `json:"status,omitzero"`
}

// SpanKind is the span's role in the trace.
type SpanKind int32

// The span kinds of the specification.
const (
	SpanKindUnspecified SpanKind = 0
	SpanKindInternal    SpanKind = 1
	SpanKindServer      SpanKind = 2
	SpanKindClient      SpanKind = 3
	SpanKindProducer    SpanKind = 4
	SpanKindConsumer    SpanKind = 5
)

// Event is a time-stamped annotation of a span.
type Event struct {
	TimeUnixNano           Nanos      `json:"timeUnixNano"`
	Name                   string     `json:"name"`
	Attributes             Attributes `json:"attributes,omitempty"`
	DroppedAttributesCount uint32     `json:"droppedAttributesCount,omitempty"`
}

// Link points from a span to a span of the same or another trace.
type Link struct {
	TraceID                TraceID    `json:"traceId"`
	SpanID                 SpanID     `json:"spanId"`
	TraceState             string     `json:"traceState,omitempty"`
	Attributes             Attributes `json:"attributes,omitempty"`
	DroppedAttributesCount uint32     `json:"droppedAttributesCount,omitempty"`
	Flags                  uint32     `json:"flags,omitempty"`
}

// Status is the span's outcome. The zero value is unset and is not written.
type Status struct {
	Message string     `json:"message,omitempty"`
	Code    StatusCode `json:"code,omitempty"`
}

// StatusCode is unset, ok or error.
type StatusCode int32

// The status codes of the specification.
const (
	StatusUnset StatusCode = 0
	StatusOK    StatusCode = 1
	StatusError StatusCode = 2
)

// KeyValue is one attribute.
type KeyValue struct {
	Key   string   `json:"key"`
	Value AnyValue `json:"value"`
}

// Attributes is a list of attributes. It is written sorted by key (equal keys
// keep their order), so that a decoder that collects attributes in a map still
// writes the same bytes every time.
type Attributes []KeyValue

// MarshalJSON writes the attributes sorted by key.
func (a Attributes) MarshalJSON() ([]byte, error) {
	sorted := slices.Clone(a)
	slices.SortStableFunc(sorted, func(x, y KeyValue) int { return strings.Compare(x.Key, y.Key) })
	return encode([]KeyValue(sorted))
}

// AnyValue is one of a string, a bool, an integer, a double, an array, a list
// of key-value pairs or bytes. Exactly one field is set; none is the empty value.
type AnyValue struct {
	StringValue *string       `json:"stringValue,omitempty"`
	BoolValue   *bool         `json:"boolValue,omitempty"`
	IntValue    *Int64        `json:"intValue,omitempty"`
	DoubleValue *Float64      `json:"doubleValue,omitempty"`
	ArrayValue  *ArrayValue   `json:"arrayValue,omitempty"`
	KvlistValue *KeyValueList `json:"kvlistValue,omitempty"`
	BytesValue  []byte        `json:"bytesValue,omitempty"`
}

// ArrayValue is a list of values.
type ArrayValue struct {
	Values []AnyValue `json:"values,omitempty"`
}

// KeyValueList is a list of key-value pairs, a map.
type KeyValueList struct {
	Values Attributes `json:"values,omitempty"`
}

// TraceID is a 16-byte trace id, written as 32 lowercase hex digits.
type TraceID [16]byte

// SpanID is an 8-byte span id, written as 16 lowercase hex digits.
type SpanID [8]byte

// IsZero reports whether the id is all zero, which OTLP treats as invalid.
func (id TraceID) IsZero() bool { return id == TraceID{} }

// IsZero reports whether the id is all zero, which OTLP treats as invalid
// (for a parent span id: the span is a root).
func (id SpanID) IsZero() bool { return id == SpanID{} }

func (id TraceID) String() string { return hex.EncodeToString(id[:]) }
func (id SpanID) String() string  { return hex.EncodeToString(id[:]) }

// MarshalJSON writes the id as a hex string.
func (id TraceID) MarshalJSON() ([]byte, error) { return strconv.AppendQuote(nil, id.String()), nil }

// MarshalJSON writes the id as a hex string.
func (id SpanID) MarshalJSON() ([]byte, error) { return strconv.AppendQuote(nil, id.String()), nil }

// UnmarshalJSON reads a hex string in either case; an empty string is the zero id.
func (id *TraceID) UnmarshalJSON(data []byte) error { return unmarshalID(data, id[:]) }

// UnmarshalJSON reads a hex string in either case; an empty string is the zero id.
func (id *SpanID) UnmarshalJSON(data []byte) error { return unmarshalID(data, id[:]) }

// ParseTraceID reads 32 hex digits in either case.
func ParseTraceID(s string) (TraceID, error) {
	var id TraceID
	return id, parseID(s, id[:])
}

// ParseSpanID reads 16 hex digits in either case.
func ParseSpanID(s string) (SpanID, error) {
	var id SpanID
	return id, parseID(s, id[:])
}

// TraceIDFromUint64s is the trace id whose high and low 64 bits are the given
// numbers (big-endian, as W3C trace context writes them). A tracer with 64-bit
// trace ids has a high half of zero.
func TraceIDFromUint64s(high, low uint64) TraceID {
	var id TraceID
	for i := range 8 {
		id[i] = byte(high >> (56 - 8*i))
		id[8+i] = byte(low >> (56 - 8*i))
	}
	return id
}

// SpanIDFromUint64 is the span id of a 64-bit number (big-endian).
func SpanIDFromUint64(v uint64) SpanID {
	var id SpanID
	for i := range 8 {
		id[i] = byte(v >> (56 - 8*i))
	}
	return id
}

func unmarshalID(data []byte, out []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return fmt.Errorf("otlp: id is not a string: %w", err)
	}
	if s == "" {
		clear(out)
		return nil
	}
	return parseID(s, out)
}

func parseID(s string, out []byte) error {
	if len(s) != 2*len(out) {
		return fmt.Errorf("otlp: id has %d hex digits, want %d", len(s), 2*len(out))
	}
	if _, err := hex.Decode(out, []byte(s)); err != nil {
		return fmt.Errorf("otlp: id is not hex: %w", err)
	}
	return nil
}

// Nanos is a time in nanoseconds since the Unix epoch. OTLP/JSON writes it as a
// decimal string; a number is read too.
type Nanos uint64

// NanosOf is the time t in nanoseconds since the Unix epoch; zero for a time
// before it.
func NanosOf(t time.Time) Nanos {
	if t.Before(time.Unix(0, 0)) {
		return 0
	}
	return Nanos(t.UnixNano())
}

// MarshalJSON writes a decimal string.
func (n Nanos) MarshalJSON() ([]byte, error) {
	return strconv.AppendQuote(nil, strconv.FormatUint(uint64(n), 10)), nil
}

// UnmarshalJSON reads a decimal string or a number.
func (n *Nanos) UnmarshalJSON(data []byte) error {
	v, err := strconv.ParseUint(unquoteNumber(data), 10, 64)
	if err != nil {
		return fmt.Errorf("otlp: time is not an unsigned 64-bit integer: %w", err)
	}
	*n = Nanos(v)
	return nil
}

// Int64 is an integer value. OTLP/JSON writes it as a decimal string; a number
// is read too.
type Int64 int64

// MarshalJSON writes a decimal string.
func (i Int64) MarshalJSON() ([]byte, error) {
	return strconv.AppendQuote(nil, strconv.FormatInt(int64(i), 10)), nil
}

// UnmarshalJSON reads a decimal string or a number.
func (i *Int64) UnmarshalJSON(data []byte) error {
	v, err := strconv.ParseInt(unquoteNumber(data), 10, 64)
	if err != nil {
		return fmt.Errorf("otlp: intValue is not a 64-bit integer: %w", err)
	}
	*i = Int64(v)
	return nil
}

// Float64 is a double value. It is written as a JSON number, and NaN and the
// infinities as the strings "NaN", "Infinity" and "-Infinity", as the Protobuf
// JSON mapping writes them.
type Float64 float64

// MarshalJSON writes a number, or one of the three strings.
func (f Float64) MarshalJSON() ([]byte, error) {
	v := float64(f)
	switch {
	case math.IsNaN(v):
		return []byte(`"NaN"`), nil
	case math.IsInf(v, 1):
		return []byte(`"Infinity"`), nil
	case math.IsInf(v, -1):
		return []byte(`"-Infinity"`), nil
	}
	return json.Marshal(v)
}

// UnmarshalJSON reads a number, a numeric string or one of the three strings.
func (f *Float64) UnmarshalJSON(data []byte) error {
	s := unquoteNumber(data)
	switch s {
	case "NaN":
		*f = Float64(math.NaN())
		return nil
	case "Infinity":
		*f = Float64(math.Inf(1))
		return nil
	case "-Infinity":
		*f = Float64(math.Inf(-1))
		return nil
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return fmt.Errorf("otlp: doubleValue is not a number: %w", err)
	}
	*f = Float64(v)
	return nil
}

// unquoteNumber is the text of a JSON number, or of a string that holds one.
func unquoteNumber(data []byte) string {
	s := string(bytes.TrimSpace(data))
	if unquoted, err := strconv.Unquote(s); err == nil && strings.HasPrefix(s, `"`) {
		return unquoted
	}
	return s
}

// Marshal writes t as compact OTLP/JSON. The same Traces always gives the same
// bytes: fields in a fixed order, attributes sorted by key, and no HTML
// escaping of '<', '>' and '&'.
func Marshal(t Traces) ([]byte, error) { return encode(t) }

// Unmarshal reads OTLP/JSON. Unknown fields are ignored, as the specification
// asks of a receiver.
func Unmarshal(data []byte) (Traces, error) {
	var t Traces
	if err := json.Unmarshal(data, &t); err != nil {
		return Traces{}, fmt.Errorf("otlp: %w", err)
	}
	return t, nil
}

func encode(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}
