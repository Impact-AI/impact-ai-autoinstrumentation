// SPDX-License-Identifier: Apache-2.0

package otlp

import (
	"crypto/sha256"
	"strings"
)

// Str is a string attribute.
func Str(key, value string) KeyValue { return KeyValue{Key: key, Value: StrValue(value)} }

// Int is an integer attribute.
func Int(key string, value int64) KeyValue { return KeyValue{Key: key, Value: IntValue(value)} }

// Double is a double attribute.
func Double(key string, value float64) KeyValue { return KeyValue{Key: key, Value: DoubleValue(value)} }

// Bool is a boolean attribute.
func Bool(key string, value bool) KeyValue { return KeyValue{Key: key, Value: BoolValue(value)} }

// Strs is an attribute that is an array of strings.
func Strs(key string, values ...string) KeyValue {
	array := make([]AnyValue, len(values))
	for i, v := range values {
		array[i] = StrValue(v)
	}
	return KeyValue{Key: key, Value: ArrayOf(array...)}
}

// KV is an attribute of any value.
func KV(key string, value AnyValue) KeyValue { return KeyValue{Key: key, Value: value} }

// StrValue is a string value.
func StrValue(v string) AnyValue { return AnyValue{StringValue: &v} }

// IntValue is an integer value.
func IntValue(v int64) AnyValue {
	i := Int64(v)
	return AnyValue{IntValue: &i}
}

// DoubleValue is a double value.
func DoubleValue(v float64) AnyValue {
	f := Float64(v)
	return AnyValue{DoubleValue: &f}
}

// BoolValue is a boolean value.
func BoolValue(v bool) AnyValue { return AnyValue{BoolValue: &v} }

// ArrayOf is an array value.
func ArrayOf(values ...AnyValue) AnyValue {
	return AnyValue{ArrayValue: &ArrayValue{Values: values}}
}

// KVListOf is a value that is a list of key-value pairs.
func KVListOf(values ...KeyValue) AnyValue {
	return AnyValue{KvlistValue: &KeyValueList{Values: values}}
}

// IDsOf derives a trace id and a span id from a record's own ids, for a source
// that has none of OpenTelemetry's: the SHA-256 of the parts joined by
// newlines, its first 16 bytes the trace id and the next 8 the span id. The
// first part names the kind of record (for example "<source>-span"), so that
// two kinds of record with the same id do not collide. The same record always
// gives the same ids, so a record that is sent again is the same span. Neither
// id is ever all zero.
func IDsOf(parts ...string) (TraceID, SpanID) {
	return idsOfSum(sha256.Sum256([]byte(strings.Join(parts, "\n"))))
}

func idsOfSum(sum [sha256.Size]byte) (TraceID, SpanID) {
	var trace TraceID
	var span SpanID
	copy(trace[:], sum[:16])
	copy(span[:], sum[16:24])
	if trace.IsZero() {
		trace[len(trace)-1] = 1
	}
	if span.IsZero() {
		span[len(span)-1] = 1
	}
	return trace, span
}

// TraceIDOf is the trace id IDsOf derives from the parts.
func TraceIDOf(parts ...string) TraceID {
	trace, _ := IDsOf(parts...)
	return trace
}

// SpanIDOf is the span id IDsOf derives from the parts.
func SpanIDOf(parts ...string) SpanID {
	_, span := IDsOf(parts...)
	return span
}
