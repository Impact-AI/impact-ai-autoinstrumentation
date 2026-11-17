// SPDX-License-Identifier: Apache-2.0

// The Datadog source: Datadog's tracers (ddtrace for Python, dd-trace for Node.js, dd-java-agent) talking to the
// customer's own Datadog Agent. The relay passes every request to the Agent unchanged; this decoder reads a copy of
// the requests that carry spans:
//
//   - the trace intake, PUT or POST /v0.3/traces, /v0.4/traces and /v0.5/traces, MessagePack. LLM Observability rides
//     inside it: each span the tracer also reports to LLM Observability carries its event in meta_struct["_llmobs"],
//     itself MessagePack (ddtrace 4.x and dd-trace 6.x in agent mode);
//   - the LLM Observability events the tracer sends through the Agent's EVP proxy, POST
//     /evp_proxy/v<n>/api/v2/llmobs (JSON from ddtrace and dd-trace, MessagePack from dd-java-agent), which ddtrace
//     and dd-trace do when APM tracing is off or for spans they expect the Agent to drop, and dd-java-agent always.
//
// APM spans follow the translation rules of the OpenTelemetry Collector's Datadog receiver
// (opentelemetry-collector-contrib, receiver/datadogreceiver/internal/translator/traces_translator.go and tags.go at
// commit f56f9a38c9864e892af542f1dbd28f475d6779dc, release v0.165.0, Apache-2.0): 64-bit ids widened with the trace's
// _dd.p.tid, the span kind from span.kind or the span type, the status from the error flag, Datadog's known tags
// renamed to their OpenTelemetry semantic conventions, and the names of HTTP, database, gRPC and AWS spans. The receiver
// reads APM spans only; the LLM Observability event, which carries the model call's content, agent, tool and retrieval
// steps, has no upstream translation, and is read here into OpenTelemetry GenAI keys.
//
// What it writes: AI meaning only in OpenTelemetry GenAI keys; every other field kept under datadog.; content (prompts,
// replies, tool arguments and results, the retrieval query) moved to GenAI content keys, and any other field that can
// hold content (document text, an agent's input and output, an error's message) under datadog.content.; a key whose
// name says it holds a credential is dropped. Datadog's own gen_ai.* tags on APM spans are read, never copied.

package source

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

type datadog struct{}

func (datadog) Source() string { return "datadog" }

// Name is the decoder and its version; bump the version when the output for the same input changes.
func (datadog) Name() string { return "datadog/1" }

// The headers the tracers send with every trace payload that say what wrote it.
const (
	ddHeaderLang          = "Datadog-Meta-Lang"
	ddHeaderLangVersion   = "Datadog-Meta-Lang-Version"
	ddHeaderTracerVersion = "Datadog-Meta-Tracer-Version"
	ddHeaderContainerID   = "Datadog-Container-Id"
)

func (datadog) Headers() []string {
	return []string{ddHeaderLang, ddHeaderLangVersion, ddHeaderTracerVersion, ddHeaderContainerID}
}

// Match claims the trace intake of every version (v0.7 and v1.0 are refused as unreadable, so that the copy reads
// partial rather than silently empty) and the LLM Observability path of the EVP proxy.
func (datadog) Match(method, path, _ string) bool {
	if method != "PUT" && method != "POST" {
		return false
	}
	switch path {
	case "/v0.3/traces", "/v0.4/traces", "/v0.5/traces", "/v0.7/traces", "/v1.0/traces":
		return true
	}
	return ddIsLLMObsPath(path)
}

func ddIsLLMObsPath(path string) bool {
	rest, ok := strings.CutPrefix(path, "/evp_proxy/v")
	if !ok {
		return false
	}
	version, endpoint, ok := strings.Cut(rest, "/")
	if _, err := strconv.Atoi(version); !ok || err != nil {
		return false
	}
	return "/"+endpoint == "/api/v2/llmobs"
}

func (d datadog) Decode(req relay.Request) (otlp.Traces, error) {
	if ddIsLLMObsPath(req.Path) {
		return d.decodeEVP(req)
	}
	switch req.Path {
	case "/v0.7/traces", "/v1.0/traces":
		return otlp.Traces{}, relay.Unreadable("the "+strings.TrimSuffix(strings.TrimPrefix(req.Path, "/"), "/traces")+
			" trace format is not decoded", nil)
	}
	if req.ContentType != "" && req.ContentType != "application/msgpack" {
		return otlp.Traces{}, relay.Unreadable("the trace payload is "+safeMediaType(req.ContentType)+", not MessagePack", nil)
	}
	if len(req.Body) == 0 {
		return otlp.Traces{}, nil
	}
	value, err := decodeMsgpack(req.Body)
	if err != nil {
		return otlp.Traces{}, relay.Unreadable("the trace payload is not MessagePack", err)
	}
	var traces [][]ddSpan
	if req.Path == "/v0.5/traces" {
		traces, err = ddTracesV05(value)
	} else {
		traces, err = ddTracesV04(value)
	}
	if err != nil {
		return otlp.Traces{}, relay.Unreadable("the trace payload is not in the "+strings.Trim(req.Path, "/")+" format", err)
	}
	return ddAPMToOTLP(traces, ddPayloadOf(req)), nil
}

// safeMediaType names a media type without echoing anything odd a client sent.
func safeMediaType(media string) string {
	for _, r := range media {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || strings.ContainsRune("/+.-", r)) || len(media) > 60 {
			return "another media type"
		}
	}
	return media
}

// ddPayload is what the request's headers say about the tracer that wrote it.
type ddPayload struct {
	lang, langVersion, tracerVersion, containerID string
}

func ddPayloadOf(req relay.Request) ddPayload {
	return ddPayload{
		lang:          req.Header.Get(ddHeaderLang),
		langVersion:   req.Header.Get(ddHeaderLangVersion),
		tracerVersion: req.Header.Get(ddHeaderTracerVersion),
		containerID:   req.Header.Get(ddHeaderContainerID),
	}
}

// ddSpan is one span of the trace intake, in either format.
type ddSpan struct {
	Service, Name, Resource, Type string
	TraceID, SpanID, ParentID     uint64
	Start, Duration, Error        int64
	Meta                          map[string]string
	Metrics                       map[string]float64
	LLMObs                        map[string]any // meta_struct["_llmobs"], decoded
	Links                         []any          // span_links, as the tracer wrote them
}

// ddTracesV04 reads the v0.3 and v0.4 formats: an array of traces, each an array of span maps.
func ddTracesV04(v any) ([][]ddSpan, error) {
	list, ok := v.([]any)
	if !ok {
		return nil, fmt.Errorf("the payload is not an array of traces")
	}
	out := make([][]ddSpan, 0, len(list))
	for i, t := range list {
		spans, ok := t.([]any)
		if !ok {
			return nil, fmt.Errorf("trace %d is not an array of spans", i)
		}
		trace := make([]ddSpan, 0, len(spans))
		for j, s := range spans {
			m, ok := s.(mpMap)
			if !ok {
				return nil, fmt.Errorf("span %d of trace %d is not a map", j, i)
			}
			trace = append(trace, ddSpanOfMap(m))
		}
		out = append(out, trace)
	}
	return out, nil
}

func ddSpanOfMap(m mpMap) ddSpan {
	var s ddSpan
	for _, e := range m {
		key, _ := e.Key.(string)
		switch key {
		case "service":
			s.Service, _ = e.Value.(string)
		case "name":
			s.Name, _ = e.Value.(string)
		case "resource":
			s.Resource, _ = e.Value.(string)
		case "type":
			s.Type, _ = e.Value.(string)
		case "trace_id":
			s.TraceID, _ = asUint64(e.Value)
		case "span_id":
			s.SpanID, _ = asUint64(e.Value)
		case "parent_id":
			s.ParentID, _ = asUint64(e.Value)
		case "start":
			s.Start, _ = asInt64(e.Value)
		case "duration":
			s.Duration, _ = asInt64(e.Value)
		case "error":
			s.Error, _ = asInt64(e.Value)
		case "meta":
			s.Meta = map[string]string{}
			if mm, ok := e.Value.(mpMap); ok {
				for _, kv := range mm {
					k, _ := kv.Key.(string)
					if v, ok := kv.Value.(string); ok && k != "" {
						s.Meta[k] = v
					}
				}
			}
		case "metrics":
			s.Metrics = map[string]float64{}
			if mm, ok := e.Value.(mpMap); ok {
				for _, kv := range mm {
					k, _ := kv.Key.(string)
					if v, ok := asFloat64(kv.Value); ok && k != "" {
						s.Metrics[k] = v
					}
				}
			}
		case "meta_struct":
			if mm, ok := e.Value.(mpMap); ok {
				if raw, ok := mm.get("_llmobs"); ok {
					if b, ok := raw.([]byte); ok {
						if decoded, err := decodeMsgpack(b); err == nil {
							s.LLMObs, _ = plain(decoded).(map[string]any)
						}
					}
				}
			}
		case "span_links":
			s.Links, _ = e.Value.([]any)
		}
	}
	return s
}

// ddTracesV05 reads the v0.5 format: [strings, traces], each span an array of 12 fields whose strings are indexes
// into the string table.
func ddTracesV05(v any) ([][]ddSpan, error) {
	top, ok := v.([]any)
	if !ok || len(top) != 2 {
		return nil, fmt.Errorf("the payload is not [strings, traces]")
	}
	table, ok := top[0].([]any)
	if !ok {
		return nil, fmt.Errorf("the string table is not an array")
	}
	str := func(x any) (string, error) {
		i, ok := asUint64(x)
		if !ok || i >= uint64(len(table)) {
			return "", fmt.Errorf("a string index is out of the table")
		}
		s, ok := table[i].(string)
		if !ok {
			return "", fmt.Errorf("the string table holds a value that is not a string")
		}
		return s, nil
	}
	list, ok := top[1].([]any)
	if !ok {
		return nil, fmt.Errorf("the traces are not an array")
	}
	out := make([][]ddSpan, 0, len(list))
	for i, t := range list {
		spans, ok := t.([]any)
		if !ok {
			return nil, fmt.Errorf("trace %d is not an array of spans", i)
		}
		trace := make([]ddSpan, 0, len(spans))
		for j, raw := range spans {
			f, ok := raw.([]any)
			if !ok || len(f) != 12 {
				return nil, fmt.Errorf("span %d of trace %d is not an array of 12 fields", j, i)
			}
			var s ddSpan
			var err error
			if s.Service, err = str(f[0]); err != nil {
				return nil, err
			}
			if s.Name, err = str(f[1]); err != nil {
				return nil, err
			}
			if s.Resource, err = str(f[2]); err != nil {
				return nil, err
			}
			s.TraceID, _ = asUint64(f[3])
			s.SpanID, _ = asUint64(f[4])
			s.ParentID, _ = asUint64(f[5])
			s.Start, _ = asInt64(f[6])
			s.Duration, _ = asInt64(f[7])
			s.Error, _ = asInt64(f[8])
			s.Meta = map[string]string{}
			if mm, ok := f[9].(mpMap); ok {
				for _, kv := range mm {
					k, err := str(kv.Key)
					if err != nil {
						return nil, err
					}
					v, err := str(kv.Value)
					if err != nil {
						return nil, err
					}
					s.Meta[k] = v
				}
			}
			s.Metrics = map[string]float64{}
			if mm, ok := f[10].(mpMap); ok {
				for _, kv := range mm {
					k, err := str(kv.Key)
					if err != nil {
						return nil, err
					}
					if v, ok := asFloat64(kv.Value); ok {
						s.Metrics[k] = v
					}
				}
			}
			if s.Type, err = str(f[11]); err != nil {
				return nil, err
			}
			trace = append(trace, s)
		}
		out = append(out, trace)
	}
	return out, nil
}

// plain turns a decoded MessagePack or JSON value into nil, bool, int64, float64, string, []any or map[string]any.
func plain(v any) any {
	switch x := v.(type) {
	case mpMap:
		m := make(map[string]any, len(x))
		for _, e := range x {
			k, ok := e.Key.(string)
			if !ok {
				k = fmt.Sprint(e.Key)
			}
			m[k] = plain(e.Value)
		}
		return m
	case map[string]any:
		for k, e := range x {
			x[k] = plain(e)
		}
		return x
	case []any:
		for i, e := range x {
			x[i] = plain(e)
		}
		return x
	case json.Number:
		if i, err := x.Int64(); err == nil {
			return i
		}
		f, _ := x.Float64()
		return f
	case uint64:
		if x <= math.MaxInt64 {
			return int64(x)
		}
		return float64(x)
	case []byte:
		return string(x)
	}
	return v
}

// ddResourceKey groups spans into one resource.
type ddResourceKey struct {
	service, env, version, lang, langVersion, tracerVersion, containerID string
}

type ddBuilder struct {
	order     []ddResourceKey
	resources map[ddResourceKey]*otlp.ResourceSpans
}

func (b *ddBuilder) add(key ddResourceKey, span otlp.Span) {
	if b.resources == nil {
		b.resources = map[ddResourceKey]*otlp.ResourceSpans{}
	}
	rs, ok := b.resources[key]
	if !ok {
		attrs := otlp.Attributes{otlp.Str("service.name", key.service), otlp.Str("telemetry.sdk.name", "Datadog")}
		if key.env != "" {
			attrs = append(attrs, otlp.Str("deployment.environment.name", key.env))
		}
		if key.version != "" {
			attrs = append(attrs, otlp.Str("service.version", key.version))
		}
		if key.lang != "" {
			attrs = append(attrs, otlp.Str("telemetry.sdk.language", key.lang))
		}
		if key.tracerVersion != "" {
			attrs = append(attrs, otlp.Str("telemetry.sdk.version", key.tracerVersion))
		}
		if key.langVersion != "" {
			attrs = append(attrs, otlp.Str("process.runtime.version", key.langVersion))
		}
		if key.containerID != "" {
			attrs = append(attrs, otlp.Str("container.id", key.containerID))
		}
		rs = &otlp.ResourceSpans{
			Resource:   otlp.Resource{Attributes: attrs},
			ScopeSpans: []otlp.ScopeSpans{{Scope: otlp.Scope{Name: "Datadog", Version: key.tracerVersion}}},
		}
		b.resources[key] = rs
		b.order = append(b.order, key)
	}
	rs.ScopeSpans[0].Spans = append(rs.ScopeSpans[0].Spans, span)
}

func (b *ddBuilder) traces() otlp.Traces {
	var t otlp.Traces
	for _, key := range b.order {
		t.ResourceSpans = append(t.ResourceSpans, *b.resources[key])
	}
	return t
}

// ddLang is the telemetry.sdk.language of a tracer's language name.
func ddLang(name string) string {
	switch strings.ToLower(name) {
	case "javascript", "nodejs", "node":
		return "nodejs"
	case "jvm", "java":
		return "java"
	case ".net", "dotnet":
		return "dotnet"
	}
	return strings.ToLower(name)
}

// ddAPMToOTLP turns the spans of one trace payload into OTLP, one resource per service.
func ddAPMToOTLP(traces [][]ddSpan, payload ddPayload) otlp.Traces {
	// The high 64 bits of a 128-bit trace id travel in _dd.p.tid, on the chunk's first span only (the receiver's
	// traceID64to128). A decoder keeps nothing between requests, so a chunk flushed apart from its first span keeps
	// a high half of zero.
	high := map[uint64]uint64{}
	for _, trace := range traces {
		for _, s := range trace {
			if tid, ok := s.Meta["_dd.p.tid"]; ok {
				if h, err := strconv.ParseUint(tid, 16, 64); err == nil {
					high[s.TraceID] = h
				}
			}
		}
	}
	var b ddBuilder
	for _, trace := range traces {
		for _, s := range trace {
			if s.LLMObs == nil && ddEventSentApart(s) {
				// The tracer sends this span's LLM Observability event through the EVP proxy, which carries
				// everything this span has and more, under the same ids: it is decoded from there.
				continue
			}
			service := s.Service
			if base, ok := s.Meta["_dd.base_service"]; ok && base != "" {
				service = base
			}
			lang := payload.lang
			if lang == "" {
				lang = s.Meta["language"]
			}
			key := ddResourceKey{
				service: service, env: s.Meta["env"], version: s.Meta["version"], lang: ddLang(lang),
				langVersion: payload.langVersion, tracerVersion: payload.tracerVersion, containerID: payload.containerID,
			}
			b.add(key, ddAPMSpan(s, high[s.TraceID], service))
		}
	}
	return b.traces()
}

// ddEventSentApart says an APM span's LLM Observability event travels on its own, through the EVP proxy: dd-trace
// marks such a span _dd.llmobs.submitted, and dd-java-agent keeps the event's fields on it as _ml_obs_tag.* (which can
// hold content) while it sends the event itself through the EVP proxy.
func ddEventSentApart(s ddSpan) bool {
	if s.Meta["_dd.llmobs.submitted"] == "1" {
		return true
	}
	for k := range s.Meta {
		if strings.HasPrefix(k, "_ml_obs_tag.") {
			return true
		}
	}
	return false
}

// ddConsumed are meta and metric keys that become the resource or the span's own fields, or are Datadog's
// reading of the LLM Observability event, which is read from the event itself.
var ddConsumed = map[string]bool{
	"env": true, "version": true, "language": true, "service": true, "_dd.p.tid": true, "span.kind": true,
	"_dd.base_service": true, "error.message": true, "error.msg": true, "error.stack": true, "error.stacktrace": true,
}

func ddAPMSpan(s ddSpan, high uint64, service string) otlp.Span {
	span := otlp.Span{
		TraceID:           otlp.TraceIDFromUint64s(high, s.TraceID),
		SpanID:            otlp.SpanIDFromUint64(s.SpanID),
		Name:              s.Name,
		StartTimeUnixNano: otlp.Nanos(max(s.Start, 0)),
		EndTimeUnixNano:   otlp.Nanos(max(s.Start+s.Duration, 0)),
	}
	if s.ParentID != 0 {
		span.ParentSpanID = otlp.SpanIDFromUint64(s.ParentID)
	}
	attrs := ddAttrs{}
	attrs.str("datadog.span.name", s.Name)
	attrs.str("datadog.resource", s.Resource)
	attrs.str("datadog.span.type", s.Type)
	attrs.str("datadog.span.id", strconv.FormatUint(s.SpanID, 10))
	attrs.str("datadog.trace.id", strconv.FormatUint(s.TraceID, 10))
	if service != s.Service {
		attrs.str("datadog.service", s.Service)
	}

	// The status from the error flag; the error's text can hold content.
	if s.Error > 0 {
		span.Status = otlp.Status{Code: otlp.StatusError}
		attrs.str("error.type", s.Meta["error.type"])
	}
	for _, k := range []string{"error.message", "error.msg"} {
		attrs.str("datadog.content.error.message", s.Meta[k])
	}
	for _, k := range []string{"error.stack", "error.stacktrace"} {
		attrs.str("datadog.content.error.stack", s.Meta[k])
	}

	ai := ddAIOf(s)
	for _, k := range sortedKeys(s.Meta) {
		v := s.Meta[k]
		switch {
		case ddConsumed[k] || k == "error.type" || ddCredentialKey(k):
		case strings.HasPrefix(k, "gen_ai."):
			// Datadog's own GenAI tags on APM spans (_dd.llmobs.artificial_gen_ai_tags): read by ddAIOf.
		case strings.HasPrefix(k, "_ml_obs"):
			// LLM Observability's own fields on an APM span; they can hold content.
			attrs.str("datadog.content."+k, v)
		case ai.isAI && ddMaybeContent(k):
			attrs.str("datadog.content."+k, v)
		default:
			if otel, ok := ddKnownKeys[strings.ToLower(k)]; ok {
				attrs.str(otel, v)
			} else if h, ok := strings.CutPrefix(k, "http.response.headers."); ok {
				attrs.str("http.response.header."+h, v)
			} else if h, ok := strings.CutPrefix(k, "http.request.headers."); ok {
				attrs.str("http.request.header."+h, v)
			} else {
				attrs.str("datadog."+k, v)
			}
		}
	}
	for _, k := range sortedKeys(s.Metrics) {
		v := s.Metrics[k]
		switch {
		case strings.HasPrefix(k, "gen_ai."):
		case k == "process_id":
			attrs.int("process.pid", int64(v))
		case ddKnownKeys[k] != "":
			attrs.double(ddKnownKeys[k], v)
		case k == "_sampling_priority_v1":
			attrs.double("datadog.sampling.priority", v)
		default:
			attrs.double("datadog."+k, v)
		}
	}
	for _, l := range s.Links {
		if link, ok := ddLinkOf(l); ok {
			span.Links = append(span.Links, link)
		}
	}
	if events, ok := s.Meta["events"]; ok {
		span.Events = ddEventsOf(events)
		delete(attrs, "datadog.events")
	}

	span.Kind = ddKindOf(s)
	if ai.isAI {
		ai.write(attrs)
		span.Kind = ai.spanKind(span.Kind)
		span.Name = ai.spanName(s.Name)
	} else {
		span.Name = ddNameOf(s)
	}
	span.Attributes = attrs.list()
	return span
}

// ddKindOf is the receiver's span kind: span.kind, else the span type.
func ddKindOf(s ddSpan) otlp.SpanKind {
	switch s.Meta["span.kind"] {
	case "server":
		return otlp.SpanKindServer
	case "client":
		return otlp.SpanKindClient
	case "producer":
		return otlp.SpanKindProducer
	case "consumer":
		return otlp.SpanKindConsumer
	case "internal":
		return otlp.SpanKindInternal
	}
	switch s.Type {
	case "web":
		return otlp.SpanKindServer
	case "http":
		return otlp.SpanKindClient
	}
	return otlp.SpanKindUnspecified
}

// ddNameOf is the receiver's span name for HTTP, database, gRPC and AWS spans; any other keeps Datadog's name.
func ddNameOf(s ddSpan) string {
	m := s.Meta
	switch s.Name {
	case "servlet.request", "http.request", "web.request":
		if method, ok := m["http.method"]; ok {
			if route, ok := m["http.route"]; ok {
				return method + " " + route
			}
			return method
		}
	case "spring.handler":
		return s.Resource
	case "postgresql.query", "redis.query":
		if summary, ok := m["db.query.summary"]; ok {
			return summary
		}
		if op, ok := m["db.operation"]; ok {
			for _, suffix := range []string{m["db.instance"], m["db.namespace"], m["peer.hostname"]} {
				if suffix != "" {
					return op + " " + suffix
				}
			}
			return op
		}
		if t, ok := m["db.type"]; ok {
			return t
		}
	case "grpc.server", "grpc.client":
		if method, ok := m["rpc.method"]; ok {
			if service := m["rpc.service"]; service != "" {
				return service + "/" + method
			}
			return method
		}
		for _, k := range []string{"rpc.grpc.full_method", "grpc.method.name"} {
			if full, ok := m[k]; ok {
				if service, method, ok := strings.Cut(strings.TrimPrefix(full, "/"), "/"); ok {
					return service + "/" + method
				}
			}
		}
		if service, method, ok := strings.Cut(strings.TrimPrefix(s.Resource, "/"), "/"); ok {
			return service + "/" + method
		}
	case "aws.request", "aws.command":
		if service, ok := m["aws.service"]; ok {
			if op, ok := m["aws.operation"]; ok {
				return service + "/" + op
			}
			return service
		}
	}
	return s.Name
}

// ddKnownKeys are the receiver's Datadog tags with an OpenTelemetry semantic convention (tags.go,
// datadogKnownResourceAttributes), less those this decoder puts on the resource.
var ddKnownKeys = map[string]string{
	"container_id": "container.id", "container_name": "container.name", "image_name": "container.image.name",
	"image_tag": "container.image.tags", "runtime": "container.runtime.name",
	"cloud_provider": "cloud.provider", "region": "cloud.region", "zone": "cloud.availability_zone",
	"task_family": "aws.ecs.task.family", "task_arn": "aws.ecs.task.arn", "ecs_cluster_name": "aws.ecs.cluster.arn",
	"task_version": "aws.ecs.task.revision", "ecs_container_name": "aws.ecs.container.arn",
	"kube_container_name": "k8s.container.name", "kube_cluster_name": "k8s.cluster.name",
	"kube_deployment": "k8s.deployment.name", "kube_replica_set": "k8s.replicaset.name",
	"kube_stateful_set": "k8s.statefulset.name", "kube_daemon_set": "k8s.daemonset.name", "kube_job": "k8s.job.name",
	"kube_cronjob": "k8s.cronjob.name", "kube_namespace": "k8s.namespace.name", "pod_name": "k8s.pod.name",
	"kube_app_name": "app.kubernetes.io/name", "kube_app_instance": "app.kubernetes.io/instance",
	"kube_app_version": "app.kubernetes.io/version", "kube_app_component": "app.kubernetes.io/component",
	"kube_app_part_of": "app.kubernetes.io/part-of", "kube_app_managed_by": "app.kubernetes.io/managed-by",
	"http.client_ip": "client.address", "http.response.content_length": "http.response.body.size",
	"http.status_code": "http.response.status_code", "http.request.content_length": "http.request.body.size",
	"http.referer": "http.request.header.referer", "http.method": "http.request.method", "http.route": "http.route",
	"http.version": "network.protocol.version", "http.server_name": "server.address", "http.url": "url.full",
	"http.useragent":     "user_agent.original",
	"aws.s3.bucket_name": "aws.s3.bucket", "aws.response.request_id": "aws.request_id", "aws.service": "rpc.service",
	"aws.operation": "rpc.method",
	"db.type":       "db.system.name", "db.operation": "db.operation.name", "db.instance": "db.namespace",
	"db.sql.table": "db.collection.name", "db.pool.name": "db.client.connection.pool.name",
	"db.statement": "db.query.text",

	// Not in the receiver's table: dd-trace's net integration (Node.js) tags its connections tcp.*, which the network
	// conventions name.
	"tcp.local.address": "network.local.address", "tcp.local.port": "network.local.port",
	"tcp.remote.host": "network.peer.address", "tcp.remote.port": "network.peer.port",
}

// ddCredentialKey says a tag's name holds a key or a password (openai.user.api_key, a forwarded authorization
// header): such a tag is dropped. Token counts are not credentials.
func ddCredentialKey(k string) bool {
	lower := strings.ToLower(k)
	for _, w := range []string{"api_key", "apikey", "api-key", "authorization", "cookie", "password", "passwd", "secret",
		"access_token", "auth_token", "session_token", "security-token", "security_token", "private_key", "bearer"} {
		if strings.Contains(lower, w) {
			return true
		}
	}
	return false
}

// ddMaybeContent says a tag of an AI integration's span can hold content: older tracers and some integrations tag
// prompts, messages and completions on the APM span itself.
func ddMaybeContent(k string) bool {
	lower := strings.ToLower(k)
	for _, w := range []string{"prompt", "completion", "message", "content", "input", "output", "choice", "text",
		"document", "query", "argument", "result", "instruction", "system"} {
		if strings.Contains(lower, w) {
			return true
		}
	}
	return false
}

func ddLinkOf(v any) (otlp.Link, bool) {
	m, ok := plain(v).(map[string]any)
	if !ok {
		return otlp.Link{}, false
	}
	low, _ := asUint64(m["trace_id"])
	high, _ := asUint64(m["trace_id_high"])
	span, _ := asUint64(m["span_id"])
	if low == 0 && high == 0 || span == 0 {
		return otlp.Link{}, false
	}
	link := otlp.Link{TraceID: otlp.TraceIDFromUint64s(high, low), SpanID: otlp.SpanIDFromUint64(span)}
	link.TraceState, _ = m["tracestate"].(string)
	if flags, ok := asUint64(m["flags"]); ok {
		link.Flags = uint32(flags)
	}
	if attrs, ok := m["attributes"].(map[string]any); ok {
		a := ddAttrs{}
		for _, k := range sortedKeys(attrs) {
			if s, ok := attrs[k].(string); ok && !ddCredentialKey(k) {
				a.str("datadog."+k, s)
			}
		}
		link.Attributes = a.list()
	}
	return link, true
}

// ddEventsOf reads the span events a tracer that does not send native span events tags as JSON in meta "events".
func ddEventsOf(raw string) []otlp.Event {
	var events []struct {
		Name         string         `json:"name"`
		TimeUnixNano json.Number    `json:"time_unix_nano"`
		Attributes   map[string]any `json:"attributes"`
	}
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	if dec.Decode(&events) != nil {
		return nil
	}
	out := make([]otlp.Event, 0, len(events))
	for _, e := range events {
		at, _ := strconv.ParseUint(e.TimeUnixNano.String(), 10, 64)
		a := ddAttrs{}
		for _, k := range sortedKeys(e.Attributes) {
			key := "datadog." + k
			if strings.HasPrefix(k, "exception.") {
				// An exception's message and stack can hold content.
				key = "datadog.content." + k
			}
			if k == "exception.type" {
				key = k
			}
			a.any(key, plain(e.Attributes[k]))
		}
		out = append(out, otlp.Event{TimeUnixNano: otlp.Nanos(at), Name: e.Name, Attributes: a.list()})
	}
	return out
}

// ddAttrs collects a span's attributes by key; list writes them sorted.
type ddAttrs map[string]otlp.AnyValue

func (a ddAttrs) str(k, v string) {
	if v != "" {
		a[k] = otlp.StrValue(v)
	}
}

func (a ddAttrs) int(k string, v int64) { a[k] = otlp.IntValue(v) }

func (a ddAttrs) double(k string, v float64) {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return
	}
	a[k] = otlp.DoubleValue(v)
}

// any writes a scalar as itself and anything else as JSON text.
func (a ddAttrs) any(k string, v any) {
	switch x := v.(type) {
	case nil:
	case string:
		a.str(k, x)
	case bool:
		a[k] = otlp.BoolValue(x)
	case int64:
		a.int(k, x)
	case float64:
		a.double(k, x)
	default:
		if text := ddJSON(x); text != "" {
			a[k] = otlp.StrValue(text)
		}
	}
}

func (a ddAttrs) list() otlp.Attributes {
	if len(a) == 0 {
		return nil
	}
	out := make(otlp.Attributes, 0, len(a))
	for _, k := range sortedKeys(a) {
		out = append(out, otlp.KeyValue{Key: k, Value: a[k]})
	}
	return out
}

// ddJSON is v as compact JSON with sorted object keys, the way GenAI's JSON attributes are written.
func ddJSON(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if enc.Encode(v) != nil {
		return ""
	}
	return strings.TrimSuffix(buf.String(), "\n")
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// --- the EVP proxy's LLM Observability events -------------------------------------------------------------------

func (datadog) decodeEVP(req relay.Request) (otlp.Traces, error) {
	if len(bytes.TrimSpace(req.Body)) == 0 {
		return otlp.Traces{}, nil
	}
	var raw any
	switch req.ContentType {
	case "application/msgpack":
		// dd-java-agent sends its events as MessagePack.
		var err error
		if raw, err = decodeMsgpack(req.Body); err != nil {
			return otlp.Traces{}, relay.Unreadable("the LLM Observability payload is not MessagePack", err)
		}
	case "", "application/json":
		dec := json.NewDecoder(bytes.NewReader(req.Body))
		dec.UseNumber()
		if err := dec.Decode(&raw); err != nil {
			return otlp.Traces{}, relay.Unreadable("the LLM Observability payload is not JSON", err)
		}
	default:
		return otlp.Traces{}, relay.Unreadable("the LLM Observability payload is "+safeMediaType(req.ContentType)+
			", not JSON or MessagePack", nil)
	}
	var events []any
	switch v := plain(raw).(type) {
	case []any:
		events = v
	case map[string]any:
		events = []any{v}
	default:
		return otlp.Traces{}, relay.Unreadable("the LLM Observability payload is not a list of events", nil)
	}
	var b ddBuilder
	for _, e := range events {
		event, ok := e.(map[string]any)
		if !ok {
			return otlp.Traces{}, relay.Unreadable("an LLM Observability event is not an object", nil)
		}
		if t, _ := event["event_type"].(string); t != "" && t != "span" {
			continue
		}
		spans, _ := event["spans"].([]any)
		for _, s := range spans {
			m, ok := s.(map[string]any)
			if !ok {
				return otlp.Traces{}, relay.Unreadable("an LLM Observability span is not an object", nil)
			}
			key, span, ok := ddEVPSpan(m)
			if !ok {
				return otlp.Traces{}, relay.Unreadable("an LLM Observability span has no span id or trace id", nil)
			}
			b.add(key, span)
		}
	}
	return b.traces(), nil
}

func ddEVPSpan(m map[string]any) (ddResourceKey, otlp.Span, bool) {
	tags := ddTagsOf(m["tags"])
	internal, _ := m["_dd"].(map[string]any)
	// The APM trace id, so that the span joins the APM spans of the same request; the event's own trace_id is LLM
	// Observability's.
	var traceID otlp.TraceID
	var err error = fmt.Errorf("none")
	for _, candidate := range []any{internal["apm_trace_id"], internal["trace_id"], m["trace_id"]} {
		if s, ok := candidate.(string); ok && len(s) == 32 {
			if traceID, err = otlp.ParseTraceID(s); err == nil {
				break
			}
		}
	}
	spanID, ok := ddDecimalID(m["span_id"])
	if err != nil || !ok || traceID.IsZero() {
		return ddResourceKey{}, otlp.Span{}, false
	}
	start, _ := asInt64(m["start_ns"])
	duration, _ := asInt64(m["duration"])
	span := otlp.Span{
		TraceID:           traceID,
		SpanID:            otlp.SpanIDFromUint64(spanID),
		StartTimeUnixNano: otlp.Nanos(max(start, 0)),
		EndTimeUnixNano:   otlp.Nanos(max(start+duration, 0)),
	}
	if parent, ok := ddDecimalID(m["parent_id"]); ok {
		span.ParentSpanID = otlp.SpanIDFromUint64(parent)
	}
	attrs := ddAttrs{}
	attrs.str("datadog.span.id", strconv.FormatUint(spanID, 10))
	if status, _ := m["status"].(string); status == "error" {
		span.Status = otlp.Status{Code: otlp.StatusError}
	}
	ai := ddAIOfEvent(m, tags)
	ai.write(attrs)
	span.Kind = ai.spanKind(otlp.SpanKindUnspecified)
	span.Name = ai.spanName(ai.name)
	span.Attributes = attrs.list()
	key := ddResourceKey{
		service: tags["service"], env: tags["env"], version: tags["version"], lang: ddLang(tags["language"]),
		tracerVersion: tags["ddtrace.version"],
	}
	return key, span, true
}

func ddDecimalID(v any) (uint64, bool) {
	switch x := v.(type) {
	case string:
		id, err := strconv.ParseUint(x, 10, 64)
		return id, err == nil && id != 0
	case int64, float64:
		id, ok := asUint64(x)
		return id, ok && id != 0
	}
	return 0, false
}

// ddTagsOf reads LLM Observability tags: a map in meta_struct, a list of "key:value" in an EVP event.
func ddTagsOf(v any) map[string]string {
	out := map[string]string{}
	switch x := v.(type) {
	case map[string]any:
		for k, val := range x {
			if s, ok := val.(string); ok {
				out[k] = s
			}
		}
	case []any:
		for _, t := range x {
			if s, ok := t.(string); ok {
				k, val, _ := strings.Cut(s, ":")
				out[k] = val
			}
		}
	}
	return out
}

// --- the AI meaning of a span ------------------------------------------------------------------------------------

// ddAI is what a span says about an AI step, read from its LLM Observability event when it has one and from
// Datadog's GenAI tags on the APM span otherwise.
type ddAI struct {
	isAI      bool
	operation string // the GenAI operation; empty for a kind GenAI has none for
	ddKind    string // Datadog's span kind: llm, embedding, agent, tool, retrieval, workflow, task, ...
	name      string // the step's name in LLM Observability

	requestModel, responseModel, provider string
	tokens                                map[string]float64 // GenAI usage key -> count
	finish                                []string
	input, output                         map[string]any
	metadata                              map[string]any
	metrics                               map[string]float64 // LLM Observability metrics without a GenAI key
	tags                                  map[string]string
	toolDefinitions                       any
	sessionID, mlApp, parentAgent         string
	errorMessage, errorType, errorStack   string
}

// ddOperations are the GenAI operations of Datadog's span kinds; llm is a chat unless the call says otherwise.
var ddOperations = map[string]string{
	"llm": "chat", "embedding": "embeddings", "agent": "invoke_agent", "tool": "execute_tool",
	"retrieval": "retrieval", "workflow": "invoke_workflow", "task": "execute_task",
}

// ddProviders are the GenAI provider names of LLM Observability's model providers.
var ddProviders = map[string]string{
	"openai": "openai", "azure_openai": "azure.ai.openai", "anthropic": "anthropic", "amazon_bedrock": "aws.bedrock",
	"bedrock": "aws.bedrock", "vertexai": "gcp.vertex_ai", "vertex_ai": "gcp.vertex_ai", "gemini": "gcp.gemini",
	"google": "gcp.gemini", "google_genai": "gcp.gen_ai", "mistral": "mistral_ai", "mistralai": "mistral_ai",
	"deepseek": "deepseek", "groq": "groq", "cohere": "cohere", "xai": "x_ai", "perplexity": "perplexity",
	"ibm": "ibm.watsonx.ai",
}

// ddIntegrationProviders name the provider of an integration that only ever calls one, when the event does not.
var ddIntegrationProviders = map[string]string{"anthropic": "anthropic", "bedrock": "aws.bedrock",
	"google_genai": "gcp.gen_ai", "vertexai": "gcp.vertex_ai"}

func ddProvider(name string) string {
	lower := strings.ToLower(strings.TrimSpace(name))
	switch lower {
	case "", "unknown", "custom", "none":
		return ""
	}
	if p, ok := ddProviders[lower]; ok {
		return p
	}
	return lower
}

// ddAIOf reads an APM span: its _llmobs struct, else Datadog's GenAI tags.
func ddAIOf(s ddSpan) ddAI {
	var ai ddAI
	kind := s.Meta["gen_ai.operation.name"]
	if kind == "" {
		kind = s.Meta["_dd.llmobs.span_kind"]
	}
	if s.LLMObs == nil && kind == "" {
		return ai
	}
	ai.isAI = true
	ai.tags = map[string]string{}
	if s.LLMObs != nil {
		ai.readEvent(s.LLMObs, ddTagsOf(s.LLMObs["tags"]))
	}
	if ai.ddKind == "" {
		ai.ddKind = kind
	}
	// What the APM span adds: the requested model, and the tags Datadog derives from the event, which are all there
	// is when LLM Observability is off.
	for _, k := range sortedKeys(s.Meta) {
		// Bedrock's integrations tag the model id without its provider (nova-micro-v1:0), which is not what was asked.
		if strings.HasSuffix(k, ".request.model") && !strings.HasPrefix(k, "gen_ai.") && !strings.Contains(k, "bedrock") &&
			ai.requestModel == "" {
			ai.requestModel = s.Meta[k]
		}
	}
	if ai.responseModel == "" {
		for _, k := range sortedKeys(s.Meta) {
			if strings.HasSuffix(k, ".response.model") && !strings.HasPrefix(k, "gen_ai.") {
				ai.responseModel = s.Meta[k]
				break
			}
		}
	}
	if ai.responseModel == "" {
		ai.responseModel = s.Meta["gen_ai.request.model"] // Datadog writes the event's model_name here
	}
	if ai.provider == "" {
		ai.provider = ddProvider(s.Meta["gen_ai.provider.name"])
	}
	if ai.mlApp == "" {
		ai.mlApp = s.Meta["gen_ai.application.name"]
	}
	if ai.sessionID == "" {
		ai.sessionID = s.Meta["gen_ai.conversation.id"]
	}
	if ai.tokens == nil {
		ai.tokens = map[string]float64{}
		for k, v := range s.Metrics {
			if key, ok := ddUsageKeys[strings.TrimPrefix(k, "gen_ai.usage.")]; ok && strings.HasPrefix(k, "gen_ai.usage.") {
				ai.tokens[key] = v
			} else if strings.HasPrefix(k, "gen_ai.usage.") {
				if ai.metrics == nil {
					ai.metrics = map[string]float64{}
				}
				ai.metrics[strings.TrimPrefix(k, "gen_ai.usage.")] = v
			}
		}
	}
	if ai.name == "" && ai.ddKind != "llm" && ai.ddKind != "embedding" {
		ai.name = s.Name
	}
	ai.operation = ddOperations[ai.ddKind]
	if ai.ddKind == "llm" && ai.input["messages"] == nil &&
		(s.Resource == "createCompletion" || strings.HasSuffix(s.Meta["openai.request.endpoint"], "/completions") &&
			!strings.HasSuffix(s.Meta["openai.request.endpoint"], "/chat/completions")) {
		ai.operation = "text_completion"
	}
	return ai
}

func ddAIOfEvent(m map[string]any, tags map[string]string) ddAI {
	ai := ddAI{isAI: true, tags: map[string]string{}}
	ai.readEvent(m, tags)
	ai.operation = ddOperations[ai.ddKind]
	return ai
}

// ddUsageKeys are the GenAI usage keys of LLM Observability's token metrics, and of Datadog's gen_ai.usage.* tags.
var ddUsageKeys = map[string]string{
	"input_tokens": "gen_ai.usage.input_tokens", "output_tokens": "gen_ai.usage.output_tokens",
	"cache_read_input_tokens": "gen_ai.usage.cache_read.input_tokens",
	"cache_read.input_tokens": "gen_ai.usage.cache_read.input_tokens",
	"reasoning_output_tokens": "gen_ai.usage.reasoning.output_tokens",
	"reasoning.output_tokens": "gen_ai.usage.reasoning.output_tokens",
}

// readEvent reads one LLM Observability span event: meta_struct's _llmobs or an EVP event's span.
func (ai *ddAI) readEvent(e map[string]any, tags map[string]string) {
	meta, _ := e["meta"].(map[string]any)
	if meta == nil {
		meta = map[string]any{}
	}
	if span, ok := meta["span"].(map[string]any); ok {
		ai.ddKind, _ = span["kind"].(string)
	}
	if ai.ddKind == "" {
		ai.ddKind, _ = meta["span.kind"].(string)
	}
	ai.name, _ = e["name"].(string)
	ai.responseModel, _ = meta["model_name"].(string)
	provider, _ := meta["model_provider"].(string)
	ai.provider = ddProvider(provider)
	if ai.provider == "" {
		ai.provider = ddIntegrationProviders[tags["integration"]]
	}
	ai.input, _ = meta["input"].(map[string]any)
	ai.output, _ = meta["output"].(map[string]any)
	ai.metadata, _ = meta["metadata"].(map[string]any)
	ai.toolDefinitions = meta["tool_definitions"]
	if attribution, ok := meta["agent_attribution"].(map[string]any); ok {
		ai.parentAgent, _ = attribution["pagent_name"].(string)
	}
	if errs, ok := meta["error"].(map[string]any); ok {
		ai.errorMessage, _ = errs["message"].(string)
		ai.errorType, _ = errs["type"].(string)
		ai.errorStack, _ = errs["stack"].(string)
	}
	for _, k := range []string{"error.message", "error.type", "error.stack"} {
		if v, ok := meta[k].(string); ok {
			switch k {
			case "error.message":
				ai.errorMessage = v
			case "error.type":
				ai.errorType = v
			default:
				ai.errorStack = v
			}
		}
	}
	ai.mlApp, _ = e["ml_app"].(string)
	if ai.mlApp == "" {
		ai.mlApp = tags["ml_app"]
	}
	ai.sessionID, _ = e["session_id"].(string)
	if ai.sessionID == "" {
		ai.sessionID = tags["session_id"]
	}
	ai.tokens = map[string]float64{}
	if metrics, ok := e["metrics"].(map[string]any); ok {
		for _, k := range sortedKeys(metrics) {
			v, ok := asFloat64(metrics[k])
			if !ok {
				continue
			}
			if key, ok := ddUsageKeys[k]; ok {
				ai.tokens[key] = v
				continue
			}
			if ai.metrics == nil {
				ai.metrics = map[string]float64{}
			}
			ai.metrics[k] = v
		}
	}
	for k, v := range tags {
		switch k {
		case "service", "env", "version", "language", "ddtrace.version", "ml_app", "session_id", "source", "error":
		default:
			ai.tags[k] = v
		}
	}
	for _, k := range []string{"finish_reason", "stop_reason"} {
		if reason, ok := ai.metadata[k].(string); ok && reason != "" {
			ai.finish = []string{reason}
			break
		}
	}
}

// write puts the step into GenAI keys, and what GenAI has no key for under datadog.
func (ai ddAI) write(a ddAttrs) {
	if !ai.isAI {
		return
	}
	a.str("gen_ai.operation.name", ai.operation)
	a.str("datadog.llmobs.span.kind", ai.ddKind)
	a.str("datadog.llmobs.name", ai.name)
	a.str("datadog.llmobs.ml_app", ai.mlApp)
	a.str("datadog.llmobs.parent_agent.name", ai.parentAgent)
	a.str("gen_ai.conversation.id", ai.sessionID)
	for _, k := range sortedKeys(ai.tags) {
		a.str("datadog.llmobs.tag."+k, ai.tags[k])
	}
	if ai.errorType != "" {
		a.str("error.type", ai.errorType)
	}
	a.str("datadog.content.error.message", ai.errorMessage)
	a.str("datadog.content.error.stack", ai.errorStack)

	model := ai.ddKind == "llm" || ai.ddKind == "embedding"
	if model || ai.responseModel != "" {
		a.str("gen_ai.request.model", ai.requestModel)
		a.str("gen_ai.response.model", ai.responseModel)
		a.str("gen_ai.provider.name", ai.provider)
	}
	for _, k := range sortedKeys(ai.tokens) {
		a.int(k, int64(ai.tokens[k]))
	}
	for _, k := range sortedKeys(ai.metrics) {
		a.double("datadog.llmobs.metrics."+k, ai.metrics[k])
	}
	if len(ai.finish) > 0 {
		a["gen_ai.response.finish_reasons"] = otlp.Strs("", ai.finish...).Value
	}
	if ai.toolDefinitions != nil {
		a.any("gen_ai.tool.definitions", ai.toolDefinitions)
	}
	ai.writeMetadata(a)

	switch ai.ddKind {
	case "agent":
		a.str("gen_ai.agent.name", ai.name)
		ai.writeValues(a, "datadog.content.input", ai.input)
		ai.writeValues(a, "datadog.content.output", ai.output)
	case "tool":
		a.str("gen_ai.tool.name", ai.name)
		if v, ok := ai.input["value"]; ok {
			a.any("gen_ai.tool.call.arguments", v)
		}
		if v, ok := ai.output["value"]; ok {
			a.any("gen_ai.tool.call.result", v)
		}
		ai.writeValues(a, "datadog.content.input", without(ai.input, "value"))
		ai.writeValues(a, "datadog.content.output", without(ai.output, "value"))
	case "retrieval":
		a.str("gen_ai.data_source.id", ai.name)
		if v, ok := ai.input["value"].(string); ok {
			a.str("gen_ai.retrieval.query.text", v)
		}
		ai.writeValues(a, "datadog.content.input", without(ai.input, "value"))
		if docs, ok := ai.output["documents"].([]any); ok {
			// The documents' ids and scores are metadata; their text and names are content.
			var refs []any
			for _, d := range docs {
				doc, _ := d.(map[string]any)
				ref := map[string]any{}
				for _, k := range []string{"id", "score"} {
					if v, ok := doc[k]; ok {
						ref[k] = v
					}
				}
				refs = append(refs, ref)
			}
			a.any("gen_ai.retrieval.documents", refs)
			a.any("datadog.content.output.documents", docs)
		}
		ai.writeValues(a, "datadog.content.output", without(ai.output, "documents"))
	case "llm":
		if msgs, ok := ai.input["messages"].([]any); ok {
			a.any("gen_ai.input.messages", ddMessages(msgs, nil))
		}
		if msgs, ok := ai.output["messages"].([]any); ok {
			a.any("gen_ai.output.messages", ddMessages(msgs, ai.finish))
		}
		ai.writeValues(a, "datadog.content.input", without(ai.input, "messages"))
		ai.writeValues(a, "datadog.content.output", without(ai.output, "messages"))
	default:
		ai.writeValues(a, "datadog.content.input", ai.input)
		ai.writeValues(a, "datadog.content.output", ai.output)
	}
}

// writeMetadata writes the call's settings: those GenAI has keys for under them, numbers and flags under
// datadog.llmobs.metadata., anything else, which can be text a person wrote, under datadog.content.
func (ai ddAI) writeMetadata(a ddAttrs) {
	for _, k := range sortedKeys(ai.metadata) {
		v := ai.metadata[k]
		switch {
		case k == "finish_reason" || k == "stop_reason":
		case k == "max_tokens" && (ai.ddKind == "llm"):
			if n, ok := asInt64(v); ok {
				a.int("gen_ai.request.max_tokens", n)
			}
		case k == "top_k" && ai.ddKind == "retrieval":
			if n, ok := asInt64(v); ok {
				a.int("gen_ai.retrieval.top_k", n)
			}
		case k == "top_k" && ai.ddKind == "llm":
			if n, ok := asInt64(v); ok {
				a.int("gen_ai.request.top_k", n)
			}
		default:
			switch x := v.(type) {
			case bool, int64, float64:
				a.any("datadog.llmobs.metadata."+k, x)
			default:
				a.any("datadog.content.llmobs.metadata."+k, x)
			}
		}
	}
}

// writeValues writes what is left of an event's input or output, which can be content, as JSON under prefix.
func (ai ddAI) writeValues(a ddAttrs, prefix string, m map[string]any) {
	for _, k := range sortedKeys(m) {
		a.any(prefix+"."+k, m[k])
	}
}

func without(m map[string]any, key string) map[string]any {
	if _, ok := m[key]; !ok {
		return m
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		if k != key {
			out[k] = v
		}
	}
	return out
}

// spanKind is a model call's CLIENT, any other step's INTERNAL, unless the tracer said.
func (ai ddAI) spanKind(fromTracer otlp.SpanKind) otlp.SpanKind {
	if fromTracer != otlp.SpanKindUnspecified {
		return fromTracer
	}
	if ai.ddKind == "llm" || ai.ddKind == "embedding" {
		return otlp.SpanKindClient
	}
	return otlp.SpanKindInternal
}

// spanName is the GenAI span name, "{operation} {model or name}", or the given name for a kind GenAI has none for.
func (ai ddAI) spanName(fallback string) string {
	if ai.operation == "" {
		if fallback == "" {
			return ai.ddKind
		}
		return fallback
	}
	target := ai.name
	if ai.ddKind == "llm" || ai.ddKind == "embedding" {
		target = ai.requestModel
		if target == "" {
			target = ai.responseModel
		}
	}
	if target == "" {
		return ai.operation
	}
	return ai.operation + " " + target
}

// ddMessages turns LLM Observability messages ({role, content, tool_calls, tool_results}) into GenAI messages
// ({role, parts}); finish, when given, is the reason of each output message.
func ddMessages(msgs []any, finish []string) []any {
	out := make([]any, 0, len(msgs))
	for i, raw := range msgs {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		role, _ := m["role"].(string)
		var parts []any
		if content, ok := m["content"]; ok && content != "" && content != nil {
			if role == "tool" {
				part := map[string]any{"type": "tool_call_response", "response": content}
				if id, ok := m["tool_id"].(string); ok && id != "" {
					part["id"] = id
				}
				parts = append(parts, part)
			} else {
				parts = append(parts, map[string]any{"type": "text", "content": content})
			}
		}
		if calls, ok := m["tool_calls"].([]any); ok {
			for _, c := range calls {
				call, _ := c.(map[string]any)
				part := map[string]any{"type": "tool_call"}
				for from, to := range map[string]string{"tool_id": "id", "name": "name", "arguments": "arguments"} {
					if v, ok := call[from]; ok && v != "" {
						part[to] = v
					}
				}
				parts = append(parts, part)
			}
		}
		if results, ok := m["tool_results"].([]any); ok {
			for _, r := range results {
				result, _ := r.(map[string]any)
				part := map[string]any{"type": "tool_call_response"}
				if v, ok := result["tool_id"]; ok && v != "" {
					part["id"] = v
				}
				if v, ok := result["result"]; ok {
					part["response"] = v
				}
				parts = append(parts, part)
			}
		}
		if role == "" {
			role = "user"
		}
		message := map[string]any{"role": role, "parts": nonNil(parts)}
		if finish != nil && i < len(finish) {
			message["finish_reason"] = finish[i]
		} else if len(finish) == 1 {
			message["finish_reason"] = finish[0]
		}
		out = append(out, message)
	}
	return out
}

func nonNil(parts []any) []any {
	if parts == nil {
		return []any{}
	}
	return slices.Clip(parts)
}
