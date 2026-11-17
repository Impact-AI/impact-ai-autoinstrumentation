// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"math"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
)

// A copy of an OTLP export says where its spans came from: every resource gets the attributes impact.source.id and
// impact.source.route, and loses any value of those keys (or of impact.source.decoder) that the workload's own
// resource attributes claimed. The rest of the request is copied as it came: the protobuf form is rewritten at the
// wire level, field by field, so fields this package does not know pass through unchanged.

func provenanceKey(key string) bool {
	return key == otlp.AttrSourceID || key == otlp.AttrSourceRoute || key == otlp.AttrSourceDecoder
}

func provenanceAttributes(p otlp.Provenance) [][2]string {
	out := [][2]string{{otlp.AttrSourceID, p.Source}, {otlp.AttrSourceRoute, p.Route}}
	if p.Decoder != "" {
		out = append(out, [2]string{otlp.AttrSourceDecoder, p.Decoder})
	}
	return out
}

// Protobuf wire types (https://protobuf.dev/programming-guides/encoding/).
const (
	wireVarint = 0
	wireI64    = 1
	wireLen    = 2
	wireI32    = 5
)

// protoField is one field of an encoded message: its number and wire type, its bytes as encoded (raw) and, for a
// length-delimited field, its payload.
type protoField struct {
	num  uint64
	typ  uint64
	raw  []byte
	data []byte
}

func protoFields(b []byte) ([]protoField, error) {
	var out []protoField
	for start := 0; start < len(b); {
		tag, n := binary.Uvarint(b[start:])
		if n <= 0 || tag>>3 == 0 || tag>>3 > math.MaxInt32 {
			return nil, unreadable("not an OTLP protobuf message")
		}
		f := protoField{num: tag >> 3, typ: tag & 7}
		i := start + n
		switch f.typ {
		case wireVarint:
			_, m := binary.Uvarint(b[i:])
			if m <= 0 {
				return nil, unreadable("not an OTLP protobuf message")
			}
			i += m
		case wireI64:
			i += 8
		case wireI32:
			i += 4
		case wireLen:
			size, m := binary.Uvarint(b[i:])
			if m <= 0 || size > uint64(len(b)-i-m) {
				return nil, unreadable("not an OTLP protobuf message")
			}
			i += m
			f.data = b[i : i+int(size)]
			i += int(size)
		default:
			return nil, unreadable("not an OTLP protobuf message")
		}
		if i > len(b) {
			return nil, unreadable("not an OTLP protobuf message")
		}
		f.raw = b[start:i]
		out = append(out, f)
		start = i
	}
	return out, nil
}

func appendLen(b []byte, num uint64, data []byte) []byte {
	b = binary.AppendUvarint(b, num<<3|wireLen)
	b = binary.AppendUvarint(b, uint64(len(data)))
	return append(b, data...)
}

// stampProtobuf stamps an ExportTraceServiceRequest: field 1 is its repeated ResourceSpans.
func stampProtobuf(b []byte, p otlp.Provenance) ([]byte, bool, error) {
	fields, err := protoFields(b)
	if err != nil {
		return nil, false, err
	}
	out := make([]byte, 0, len(b)+len(fields)*96)
	spans := false
	for _, f := range fields {
		if f.num != 1 {
			out = append(out, f.raw...)
			continue
		}
		if f.typ != wireLen {
			return nil, false, unreadable("not an OTLP protobuf message")
		}
		rs, err := stampResourceSpans(f.data, p)
		if err != nil {
			return nil, false, err
		}
		out = appendLen(out, 1, rs)
		spans = true
	}
	return out, spans, nil
}

// stampResourceSpans rewrites one ResourceSpans: its Resource (field 1) becomes one field that holds every attribute
// it had except the provenance keys, and then the copy's provenance. Several occurrences of the Resource field are
// merged, as a protobuf parser merges them.
func stampResourceSpans(b []byte, p otlp.Provenance) ([]byte, error) {
	fields, err := protoFields(b)
	if err != nil {
		return nil, err
	}
	var resource []byte
	at := -1 // where the Resource goes among the other fields
	var rest [][]byte
	for _, f := range fields {
		if f.num != 1 {
			rest = append(rest, f.raw)
			continue
		}
		if f.typ != wireLen {
			return nil, unreadable("not an OTLP protobuf message")
		}
		kept, err := withoutProvenance(f.data)
		if err != nil {
			return nil, err
		}
		resource = append(resource, kept...)
		if at < 0 {
			at = len(rest)
		}
	}
	for _, kv := range provenanceAttributes(p) {
		resource = appendLen(resource, 1, keyValue(kv[0], kv[1]))
	}
	if at < 0 {
		at = 0
	}
	out := make([]byte, 0, len(b)+len(resource))
	for i, raw := range rest {
		if i == at {
			out = appendLen(out, 1, resource)
		}
		out = append(out, raw...)
	}
	if at == len(rest) {
		out = appendLen(out, 1, resource)
	}
	return out, nil
}

// withoutProvenance is a Resource's fields without the attributes (field 1, KeyValue) of a provenance key.
func withoutProvenance(b []byte) ([]byte, error) {
	fields, err := protoFields(b)
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, len(b))
	for _, f := range fields {
		if f.num == 1 {
			if f.typ != wireLen {
				return nil, unreadable("not an OTLP protobuf message")
			}
			key, err := keyOf(f.data)
			if err != nil {
				return nil, err
			}
			if provenanceKey(key) {
				continue
			}
		}
		out = append(out, f.raw...)
	}
	return out, nil
}

// keyOf is a KeyValue's key (field 1; the last one wins, as in any parser).
func keyOf(b []byte) (string, error) {
	fields, err := protoFields(b)
	if err != nil {
		return "", err
	}
	key := ""
	for _, f := range fields {
		if f.num == 1 && f.typ == wireLen {
			key = string(f.data)
		}
	}
	return key, nil
}

// keyValue encodes KeyValue{key, AnyValue{string_value}}.
func keyValue(key, value string) []byte {
	kv := appendLen(nil, 1, []byte(key))
	return appendLen(kv, 2, appendLen(nil, 1, []byte(value)))
}

// stampJSON stamps an OTLP/JSON ExportTraceServiceRequest. Numbers keep their text.
func stampJSON(b []byte, p otlp.Provenance) ([]byte, bool, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil || doc == nil {
		return nil, false, unreadable("not OTLP JSON")
	}
	all, _ := doc["resourceSpans"].([]any)
	if doc["resourceSpans"] != nil && all == nil {
		return nil, false, unreadable("not OTLP JSON")
	}
	for _, item := range all {
		rs, ok := item.(map[string]any)
		if !ok {
			return nil, false, unreadable("not OTLP JSON")
		}
		resource, ok := rs["resource"].(map[string]any)
		if !ok {
			if rs["resource"] != nil {
				return nil, false, unreadable("not OTLP JSON")
			}
			resource = map[string]any{}
		}
		attrs, ok := resource["attributes"].([]any)
		if !ok && resource["attributes"] != nil {
			return nil, false, unreadable("not OTLP JSON")
		}
		kept := make([]any, 0, len(attrs)+3)
		for _, a := range attrs {
			if kv, ok := a.(map[string]any); ok {
				if key, _ := kv["key"].(string); provenanceKey(key) {
					continue
				}
			}
			kept = append(kept, a)
		}
		for _, kv := range provenanceAttributes(p) {
			kept = append(kept, map[string]any{"key": kv[0], "value": map[string]any{"stringValue": kv[1]}})
		}
		resource["attributes"] = kept
		rs["resource"] = resource
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(doc); err != nil {
		return nil, false, unreadable("not OTLP JSON")
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), len(all) > 0, nil
}
