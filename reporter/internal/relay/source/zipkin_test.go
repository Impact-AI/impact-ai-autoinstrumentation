// SPDX-License-Identifier: Apache-2.0

package source

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/otlp"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/relaytest"
)

func TestZipkinSpanReportsDecodeToTheGoldenOTLP(t *testing.T) {
	relaytest.GoldenDir(t, zipkin{}, "testdata/zipkin")
}

func TestZipkinClaimsOnlyItsSpanReports(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		want         bool
	}{
		{http.MethodPost, "/api/v2/spans", true},
		{http.MethodPost, "/vendor/prefix/api/v2/spans", true},
		{http.MethodGet, "/api/v2/spans", false},
		{http.MethodGet, "/api/v2/services", false},
		{http.MethodPost, "/api/v1/spans", false},
		{http.MethodPost, "/v1/traces", false},
	} {
		if got := (zipkin{}).Match(tc.method, tc.path, ""); got != tc.want {
			t.Errorf("Match(%s %s) = %v, want %v", tc.method, tc.path, got, tc.want)
		}
	}
}

// A payload the decoder cannot read whole is a *PayloadError that never quotes it: the relay has already passed it
// on, and reports the copy as partial.
func TestZipkinPayloadsItCannotReadAreUnreadableWithoutQuotingThem(t *testing.T) {
	const secret = "sk-zipkin-never-quoted"
	for name, tc := range map[string]struct{ contentType, body string }{
		"protobuf form":   {"application/x-protobuf", "\x0a\x10" + secret},
		"not JSON":        {"application/json", "{" + secret},
		"not a list":      {"application/json", `{"spans":"` + secret + `"}`},
		"bad trace id":    {"application/json", `[{"traceId":"` + secret + `","id":"352bff9a74ca9ad2","timestamp":1}]`},
		"zero trace id":   {"application/json", `[{"traceId":"0000000000000000","id":"352bff9a74ca9ad2","timestamp":1}]`},
		"short span id":   {"application/json", `[{"traceId":"5af7183fb1d4cf5f","id":"12","timestamp":1}]`},
		"bad parent id":   {"application/json", `[{"traceId":"5af7183fb1d4cf5f","id":"352bff9a74ca9ad2","parentId":"x","timestamp":1}]`},
		"no timestamp":    {"application/json", `[{"traceId":"5af7183fb1d4cf5f","id":"352bff9a74ca9ad2","name":"` + secret + `"}]`},
		"one bad of many": {"application/json", `[{"traceId":"5af7183fb1d4cf5f","id":"352bff9a74ca9ad2","timestamp":1},{"traceId":"zz","id":"1"}]`},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := relay.Decode(zipkin{}, relay.Request{Method: http.MethodPost, Path: zipkinPath, ContentType: tc.contentType, Body: []byte(tc.body)})
			var payloadErr *relay.PayloadError
			if !errors.As(err, &payloadErr) {
				t.Fatalf("error %v, want a *relay.PayloadError", err)
			}
			if strings.Contains(err.Error(), secret) {
				t.Errorf("the error quotes the payload: %v", err)
			}
		})
	}
}

func TestAnEmptyZipkinReportIsNoSpans(t *testing.T) {
	traces, err := relay.Decode(zipkin{}, relay.Request{Method: http.MethodPost, Path: zipkinPath, ContentType: "application/json", Body: []byte(`[]`)})
	if err != nil || len(traces.ResourceSpans) != 0 {
		t.Errorf("traces %+v, err %v", traces, err)
	}
}

func TestZipkinTagsKeepConventionNamesAndGoUnderTheSourceOtherwise(t *testing.T) {
	for tag, want := range map[string]string{
		"gen_ai.request.model":           "gen_ai.request.model",
		"gen_ai.system":                  "gen_ai.provider.name",
		"gen_ai.usage.prompt_tokens":     "gen_ai.usage.input_tokens",
		"gen_ai.usage.completion_tokens": "gen_ai.usage.output_tokens",
		"http.request.method":            "http.request.method",
		"db.system.name":                 "db.system.name",
		"component":                      "zipkin.component",
		"llm.prompt":                     "zipkin.content.llm.prompt",
		"llm.output":                     "zipkin.content.llm.output",
		"mvc.controller.class":           "zipkin.mvc.controller.class",
	} {
		if got := zipkinAttribute(tag); got != want {
			t.Errorf("tag %q became %q, want %q", tag, got, want)
		}
	}
}

// zipkinSpanWithTags is a Zipkin v2 report of one CLIENT span with the given tags and a remote endpoint.
func zipkinSpanWithTags(t *testing.T, tags map[string]string, remote string) relay.Request {
	t.Helper()
	span := map[string]any{
		"traceId": "5af7183fb1d4cf5f", "id": "352bff9a74ca9ad2", "kind": "CLIENT", "name": "chat gpt-4o",
		"timestamp": 1731150000000000, "duration": 1200, "localEndpoint": map[string]any{"serviceName": "support"},
		"tags": tags,
	}
	if remote != "" {
		span["remoteEndpoint"] = map[string]any{"ipv4": remote, "port": 443}
	}
	body, err := json.Marshal([]any{span})
	if err != nil {
		t.Fatal(err)
	}
	return relay.Request{Method: http.MethodPost, Path: zipkinPath, ContentType: "application/json", Body: body}
}

// impact.ai refuses a span that repeats an attribute key, so a tag and the remote endpoint, or a tag and an older name
// renamed onto it, give one attribute: the tag that already uses the current name.
func TestAZipkinSpanWritesEachAttributeKeyOnce(t *testing.T) {
	traces, err := relay.Decode(zipkin{}, zipkinSpanWithTags(t, map[string]string{
		"network.peer.address":           "203.0.113.9",
		"gen_ai.system":                  "openai-legacy",
		"gen_ai.provider.name":           "openai",
		"gen_ai.usage.prompt_tokens":     "1",
		"gen_ai.usage.input_tokens":      "420",
		"gen_ai.usage.completion_tokens": "2",
	}, "198.51.100.4"))
	if err != nil {
		t.Fatal(err)
	}
	span := traces.ResourceSpans[0].ScopeSpans[0].Spans[0]
	got := map[string]string{}
	for _, kv := range span.Attributes {
		if _, twice := got[kv.Key]; twice {
			t.Errorf("the key %q is written twice", kv.Key)
		}
		got[kv.Key] = attributeText(kv.Value)
	}
	for key, want := range map[string]string{
		"network.peer.address": "203.0.113.9", "gen_ai.provider.name": "openai",
		"gen_ai.usage.input_tokens": "420", "gen_ai.usage.output_tokens": "2", "network.peer.port": "443",
	} {
		if got[key] != want {
			t.Errorf("%s = %q, want %q", key, got[key], want)
		}
	}
}

func attributeText(v otlp.AnyValue) string {
	switch {
	case v.StringValue != nil:
		return *v.StringValue
	case v.IntValue != nil:
		return strconv.FormatInt(int64(*v.IntValue), 10)
	}
	return fmt.Sprintf("%v", v)
}

// intakeContent is testdata/zipkin/intake-content.json: impact.ai's Metadata only list and its span attribute vectors.
type intakeContent struct {
	Keys     []string `json:"keys"`
	Prefixes []string `json:"prefixes"`
	Patterns []string `json:"patterns"`
	Vectors  []struct {
		Key     string `json:"key"`
		Content bool   `json:"content"`
	} `json:"vectors"`
}

func readIntakeContent(t *testing.T) intakeContent {
	t.Helper()
	data, err := os.ReadFile("testdata/zipkin/intake-content.json")
	if err != nil {
		t.Fatal(err)
	}
	var c intakeContent
	if err := json.Unmarshal(data, &c); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestTheDecodersCopyOfTheIntakeContentListIsTheTestdatasOne(t *testing.T) {
	c := readIntakeContent(t)
	if !slices.Equal(c.Keys, intakeContentKeys) || !slices.Equal(c.Prefixes, intakeContentPrefixes) || !slices.Equal(c.Patterns, intakeContentPatterns) {
		t.Errorf("zipkin_content.go and testdata/zipkin/intake-content.json name different keys")
	}
	for _, v := range c.Vectors {
		if v.Content && !intakeWithholds(v.Key) {
			t.Errorf("the vector %q is content, but the list does not withhold it", v.Key)
		}
	}
}

// Metadata only withholds a vendor's content too: whatever tag carries content, through the Zipkin decoder it lands
// under a key impact.ai withholds, never under one it keeps.
func TestZipkinContentTagsLandUnderKeysImpactAIWithholds(t *testing.T) {
	const canary = "CONTENT-CANARY"
	tags := map[string]string{}
	for _, v := range readIntakeContent(t).Vectors {
		if v.Content {
			tags[v.Key] = canary
		}
	}
	// Names that only say they can hold content: the decoder's own hint.
	for _, tag := range []string{"llm.prompt", "user_question", "agent.answer", "tool.args.result", "chain.msg", "rag.document_text"} {
		tags[tag] = canary
	}
	traces, err := relay.Decode(zipkin{}, zipkinSpanWithTags(t, tags, ""))
	if err != nil {
		t.Fatal(err)
	}
	span := traces.ResourceSpans[0].ScopeSpans[0].Spans[0]
	n := 0
	for _, kv := range span.Attributes {
		if attributeText(kv.Value) != canary {
			continue
		}
		n++
		if !intakeWithholds(kv.Key) && !strings.HasPrefix(kv.Key, "zipkin.content.") {
			t.Errorf("content landed under %q, which impact.ai keeps in Metadata only mode", kv.Key)
		}
	}
	if n != len(tags) {
		t.Errorf("%d of %d content tags were kept", n, len(tags))
	}
}
