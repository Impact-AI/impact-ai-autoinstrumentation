// SPDX-License-Identifier: Apache-2.0

package otlp

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// canonical is a JSON document as a generic value, so that two documents compare
// by content and not by spacing or the order of object keys.
func canonical(t *testing.T, data []byte) any {
	t.Helper()
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, data)
	}
	return v
}

func marshal(t *testing.T, traces Traces) string {
	t.Helper()
	out, err := Marshal(traces)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

// testdata/traces.json is written by hand after the OTLP specification's JSON
// encoding (https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding) and
// the example of opentelemetry-proto (examples/trace.json), with every kind of
// value, an event, a link and a status added.
func TestTheSpecificationSampleRoundTrips(t *testing.T) {
	sample, err := os.ReadFile(filepath.Join("testdata", "traces.json"))
	if err != nil {
		t.Fatal(err)
	}
	traces, err := Unmarshal(sample)
	if err != nil {
		t.Fatal(err)
	}
	out := marshal(t, traces)
	if !reflect.DeepEqual(canonical(t, sample), canonical(t, []byte(out))) {
		t.Fatalf("the sample changed in a round trip:\n%s", out)
	}

	span := traces.ResourceSpans[0].ScopeSpans[0].Spans[0]
	if span.TraceID.String() != "5b8efff798038103d269b633813fc60c" || span.ParentSpanID.String() != "eee19b7ec3c1b173" {
		t.Errorf("ids: %s %s", span.TraceID, span.ParentSpanID)
	}
	if span.Kind != SpanKindClient || span.Status.Code != StatusError || span.StartTimeUnixNano != 1544712660000000000 {
		t.Errorf("kind %d, status %d, start %d", span.Kind, span.Status.Code, span.StartTimeUnixNano)
	}
	values := map[string]AnyValue{}
	for _, kv := range span.Attributes {
		values[kv.Key] = kv.Value
	}
	if v := values["example.int"].IntValue; v == nil || *v != 420 {
		t.Errorf("example.int: %v", v)
	}
	if v := values["example.double"].DoubleValue; v == nil || *v != 0.5 {
		t.Errorf("example.double: %v", v)
	}
	if v := values["example.map"].KvlistValue; v == nil || len(v.Values) != 2 {
		t.Errorf("example.map: %v", v)
	}
	if root := traces.ResourceSpans[0].ScopeSpans[0].Spans[1]; !root.ParentSpanID.IsZero() {
		t.Errorf("the server span has a parent: %s", root.ParentSpanID)
	}
}

func TestIdsAreReadInEitherCaseAndWrittenInLowerCase(t *testing.T) {
	var span Span
	if err := json.Unmarshal([]byte(`{"traceId":"5B8EFFF798038103D269B633813FC60C","spanId":"EEE19B7EC3C1B174","parentSpanId":""}`), &span); err != nil {
		t.Fatal(err)
	}
	want, err := ParseTraceID("5b8efff798038103d269b633813fc60c")
	if err != nil {
		t.Fatal(err)
	}
	if span.TraceID != want || span.SpanID.String() != "eee19b7ec3c1b174" || !span.ParentSpanID.IsZero() {
		t.Fatalf("read %s %s %s", span.TraceID, span.SpanID, span.ParentSpanID)
	}
	out, err := json.Marshal(span)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174"`) ||
		strings.Contains(string(out), "parentSpanId") {
		t.Fatalf("written %s", out)
	}
	for _, bad := range []string{`"5b8e"`, `"zz8efff798038103d269b633813fc60c"`, `42`} {
		var id TraceID
		if err := json.Unmarshal([]byte(bad), &id); err == nil {
			t.Errorf("%s was read as an id", bad)
		}
	}
}

func TestIntegersAndTimesAreStringsAndNumbersAreReadToo(t *testing.T) {
	var kv KeyValue
	if err := json.Unmarshal([]byte(`{"key":"n","value":{"intValue":42}}`), &kv); err != nil {
		t.Fatal(err)
	}
	if out, _ := json.Marshal(kv); string(out) != `{"key":"n","value":{"intValue":"42"}}` {
		t.Fatalf("written %s", out)
	}
	var event Event
	if err := json.Unmarshal([]byte(`{"timeUnixNano":1544712660500000000,"name":"e"}`), &event); err != nil {
		t.Fatal(err)
	}
	if out, _ := json.Marshal(event); string(out) != `{"timeUnixNano":"1544712660500000000","name":"e"}` {
		t.Fatalf("written %s", out)
	}
	if out, _ := json.Marshal(Int("min", math.MinInt64)); !strings.Contains(string(out), `"-9223372036854775808"`) {
		t.Fatalf("written %s", out)
	}
}

func TestDoublesWriteTheirSpecialValuesAsTheProtobufMappingDoes(t *testing.T) {
	for _, c := range []struct {
		v    float64
		want string
	}{{0.5, `0.5`}, {2, `2`}, {math.NaN(), `"NaN"`}, {math.Inf(1), `"Infinity"`}, {math.Inf(-1), `"-Infinity"`}} {
		out, err := json.Marshal(Float64(c.v))
		if err != nil || string(out) != c.want {
			t.Errorf("%v: %s, %v", c.v, out, err)
		}
		var back Float64
		if err := json.Unmarshal(out, &back); err != nil {
			t.Errorf("%s: %v", out, err)
		} else if float64(back) != c.v && !(math.IsNaN(c.v) && math.IsNaN(float64(back))) {
			t.Errorf("%s read as %v", out, back)
		}
	}
}

func TestTheBuildersWriteTheValuesOfTheSpecification(t *testing.T) {
	traces := Traces{ResourceSpans: []ResourceSpans{{
		ScopeSpans: []ScopeSpans{{Spans: []Span{{
			TraceID: TraceIDFromUint64s(0, 1),
			SpanID:  SpanIDFromUint64(2),
			Name:    "s",
			Attributes: Attributes{
				Str("a.str", "x"),
				Int("a.int", 420),
				Double("a.double", 1.5),
				Bool("a.bool", false),
				Strs("a.strs", "p", "q"),
				KV("a.map", KVListOf(Str("k", "v"))),
			},
		}}}},
	}}}
	want := `{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"00000000000000000000000000000001",` +
		`"spanId":"0000000000000002","name":"s","startTimeUnixNano":"0","endTimeUnixNano":"0","attributes":[` +
		`{"key":"a.bool","value":{"boolValue":false}},` +
		`{"key":"a.double","value":{"doubleValue":1.5}},` +
		`{"key":"a.int","value":{"intValue":"420"}},` +
		`{"key":"a.map","value":{"kvlistValue":{"values":[{"key":"k","value":{"stringValue":"v"}}]}}},` +
		`{"key":"a.str","value":{"stringValue":"x"}},` +
		`{"key":"a.strs","value":{"arrayValue":{"values":[{"stringValue":"p"},{"stringValue":"q"}]}}}]}]}]}]}`
	if got := marshal(t, traces); got != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
	if got := marshal(t, Traces{}); got != `{}` {
		t.Errorf("empty: %s", got)
	}
}

func TestMarshalIsStableAndDoesNotEscapeHTML(t *testing.T) {
	build := func(attributes ...KeyValue) Traces {
		return Traces{ResourceSpans: []ResourceSpans{{
			Resource:   Resource{Attributes: attributes},
			ScopeSpans: []ScopeSpans{{Spans: []Span{{Name: "<b>&", Attributes: attributes}}}},
		}}}
	}
	one := marshal(t, build(Str("b", "2"), Str("a", "1"), Str("c", "<3>"), Str("a", "again")))
	two := marshal(t, build(Str("c", "<3>"), Str("a", "1"), Str("b", "2"), Str("a", "again")))
	if one != two {
		t.Fatalf("the order of the attributes changed the bytes:\n%s\n%s", one, two)
	}
	if !strings.Contains(one, `"name":"<b>&"`) || !strings.Contains(one, `"<3>"`) {
		t.Errorf("HTML was escaped: %s", one)
	}
	if strings.Index(one, `"1"`) > strings.Index(one, `"again"`) {
		t.Errorf("equal keys changed their order: %s", one)
	}
	// Marshal never changes what it is given.
	attributes := Attributes{Str("z", "1"), Str("a", "2")}
	if _, err := Marshal(build(attributes...)); err != nil || attributes[0].Key != "z" {
		t.Errorf("the attributes were sorted in place: %v %v", attributes, err)
	}
}

func TestIdsOfARecordAreItsSHA256AndNeverZero(t *testing.T) {
	sum := sha256.Sum256([]byte("example-span\nrecord-1"))
	trace, span := IDsOf("example-span", "record-1")
	if trace.String() != hex.EncodeToString(sum[:16]) || span.String() != hex.EncodeToString(sum[16:24]) {
		t.Fatalf("ids %s %s, digest %x", trace, span, sum)
	}
	if TraceIDOf("example-span", "record-1") != trace || SpanIDOf("example-span", "record-1") != span {
		t.Error("TraceIDOf and SpanIDOf disagree with IDsOf")
	}
	if again, _ := IDsOf("example-span", "record-1"); again != trace {
		t.Error("the same record gave another id")
	}
	if other, _ := IDsOf("example-span", "record-2"); other == trace {
		t.Error("two records gave the same id")
	}
	zeroTrace, zeroSpan := idsOfSum([sha256.Size]byte{})
	if zeroTrace.IsZero() || zeroSpan.IsZero() {
		t.Errorf("an all-zero digest gave a zero id: %s %s", zeroTrace, zeroSpan)
	}
}

func TestWideningSixtyFourBitIds(t *testing.T) {
	if got := TraceIDFromUint64s(0x0102030405060708, 0x090a0b0c0d0e0f10).String(); got != "0102030405060708090a0b0c0d0e0f10" {
		t.Errorf("trace id %s", got)
	}
	if got := TraceIDFromUint64s(0, 1234).String(); got != "000000000000000000000000000004d2" {
		t.Errorf("trace id %s", got)
	}
	if got := SpanIDFromUint64(0xfedcba9876543210).String(); got != "fedcba9876543210" {
		t.Errorf("span id %s", got)
	}
}

func TestProvenanceIsSetOnEveryResourceAndCannotBeClaimedByThePayload(t *testing.T) {
	traces := Traces{ResourceSpans: []ResourceSpans{
		{Resource: Resource{Attributes: Attributes{
			Str("service.name", "checkout"),
			Str(AttrSourceID, "someone-else"),
			Str(AttrSourceDecoder, "forged/1"),
		}}},
		{},
	}}
	Provenance{Source: "example", Route: RouteCopy}.Apply(&traces)
	for i, rs := range traces.ResourceSpans {
		got := map[string]string{}
		for _, kv := range rs.Resource.Attributes {
			got[kv.Key] = *kv.Value.StringValue
		}
		if got[AttrSourceID] != "example" || got[AttrSourceRoute] != "copy" {
			t.Errorf("resource %d: %v", i, got)
		}
		if _, ok := got[AttrSourceDecoder]; ok {
			t.Errorf("resource %d keeps the payload's decoder: %v", i, got)
		}
	}
	Provenance{Source: "example", Route: RouteRelay, Decoder: "example/1.0.0"}.Apply(&traces)
	attrs := traces.ResourceSpans[0].Resource.Attributes
	if len(attrs) != 4 {
		t.Fatalf("attributes %v", attrs)
	}
	for _, kv := range attrs {
		if kv.Key == AttrSourceDecoder && *kv.Value.StringValue != "example/1.0.0" {
			t.Errorf("decoder %s", *kv.Value.StringValue)
		}
	}
}
