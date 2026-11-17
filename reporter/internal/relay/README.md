# Relay decoders

A workload that already runs another vendor's tracer keeps it. For a tracer that does not speak OTLP, the kit relays
its protocol to the vendor unchanged and decodes a copy into OpenTelemetry spans for impact.ai. A **source** is one
such tool, with a stable id (`datadog`, `newrelic`); a **decoder** turns its wire format into OTLP. This page is what
every decoder does, so that each can be written on its own and still reads like the others.

| Package | What |
| --- | --- |
| `relay` | `Request`, the `Decoder` interface, `NewRequest`, `Decode`, `Validate` and the errors |
| `relay/otlp` | OTLP/JSON trace types, attribute builders, deterministic ids, provenance and `Marshal` |
| `relay/source` | One file per source and `registry.go`, the list of every source |
| `relay/relaytest` | `Golden`: a decoder against recorded requests and a golden OTLP/JSON file |

## The vendor's stream is never changed

The relay forwards every request first, byte for byte: path, headers and body, and returns the backend's answer
unchanged. Only after the vendor's backend accepted a request, and only when the decoder's `Match(method, path, query)` says it
carries spans, does the relay make a copy with `NewRequest` and decode it with `relay.Decode`. A decoder therefore
works on a copy and cannot change, delay or fail what the vendor receives. When it fails, the vendor still has
everything and the copy is reported as partial:

- `*PayloadError` (made with `relay.Unreadable(reason, err)`): the payload is too large, compressed in a way the relay
  does not read, or not in the format the decoder expects. The reason never quotes the payload.
- A panic in a decoder is caught by `relay.Decode` and reported like an error.

## What a decoder is given

`NewRequest` builds the `Request` a decoder reads, and it never holds a credential:

- **Headers**: only the names the decoder lists in `Headers()`, and never one whose name looks like a credential
  (`Authorization`, `Cookie`, `*-Api-Key`, `*-License-Key`, anything with key, token, secret, auth, session or password
  in its name), even if the decoder lists it. `Validate` refuses a decoder that lists one.
- **Query**: `RawQuery` without any parameter whose name looks like a credential (`license_key`, `api_key`,
  `dd-api-key`, `access_token`, ...).
- **Body**: a copy, decompressed from `gzip` or `deflate`, at most 32 MiB as it arrived and 64 MiB decompressed.
- **ContentType**: the media type alone, lower case.

A decoder never reaches the network, never logs a payload and keeps nothing between requests that could hold a key.

## What a decoder writes

1. **AI meaning only in OpenTelemetry GenAI keys.** Model, operation, provider, tokens, tools, agents and retrieval are
   written in the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/), in their current
   spelling, never one that was replaced (`gen_ai.provider.name`, not `gen_ai.system`; `gen_ai.usage.input_tokens`,
   not `gen_ai.usage.prompt_tokens`). A decoder never invents a `gen_ai.*` key, and never writes another convention's
   keys (`llm.*`, `openinference.*`, `traceloop.*`).
2. **Everything else is kept, under the source's namespace.** A field with no GenAI equivalent stays as an attribute
   named `<source>.<field>` (`datadog.component`, `newrelic.transaction.name`). Keys that already are OpenTelemetry
   semantic conventions outside GenAI (`http.*`, `db.*`, `server.*`, `error.type`) may stay as they are.
3. **Content is moved, not copied.** Prompts, replies, system instructions, tool arguments and results and retrieved
   documents go to the GenAI content keys (`gen_ai.input.messages`, `gen_ai.output.messages`,
   `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`,
   `gen_ai.retrieval.documents`) and are not also kept under the source's name. Any other field that can hold content
   goes under `<source>.content.`, so that it is withheld wherever content is.
4. **A field the source does not have is not written.** A missing token count is absent, never zero.
5. **Provenance on every resource.** `relay.Decode` sets `impact.source.id` (the source id), `impact.source.route`
   (`relay`) and `impact.source.decoder` (the decoder's `Name()`, `<decoder>/<version>`), replacing whatever the payload
   claimed. Bump the version when the output for the same input changes.
6. **Ids.** Keep the source's own trace and span ids. 64-bit ids are widened the way OpenTelemetry's own
   interoperability for that tracer does (`otlp.TraceIDFromUint64s(high, low)`, the high half from the tracer's
   128-bit trace id tag when it sends one). A source without ids derives them from its own record ids with
   `otlp.IDsOf("<source>-<record kind>", id...)`: SHA-256 of the parts joined by newlines, the first 16 bytes the
   trace id, the next 8 the span id, never all zero. A record that is sent again is the same span.
7. **Stable output.** The same input gives the same spans in the same order. `otlp.Marshal` sorts attributes by key.

Where a public, open-source translation of the source's format to OpenTelemetry exists and is the agreed reference, the
decoder follows its rules, says so in its file header and pins the upstream commit it follows. Only formats that are
documented publicly or implemented by open-source tracers are decoded.

## Fixtures

Every decoder is tested against fixtures that are public or recorded, never a customer's:

- **Recorded**: the source's open-source tracer or SDK, at a pinned version, run against the kit's recording mock
  (`testing/mock/server.py`) with the mock providers' fixed answers. `testing/mock/recording.py` turns the mock's
  `GET /_requests` into a recording.
- **From public documentation**: written by hand; `how` says so and links the page.

A recording is JSON: `how` (what ran, against what), `packages` (`{name: version}`, empty for documentation),
`recorded` (`YYYY-MM-DD`) and `requests`, each with `method`, `path`, `query`, `headers` (`{name: value}`),
`contentType`, `contentEncoding` and `bodyBase64`. Planted keys and names are fictitious and allowed by name in
`scripts/check_internal_details.py`.

`relaytest.GoldenDir(t, decoder, "testdata/<source>")` decodes every `<name>.recording.json` there the way the relay
does and compares it with `<name>.otlp.json`; `UPDATE_GOLDEN=1 go test ./...` rewrites the golden files. It also fails
when a span has a zero id, or when the value of a recorded credential header or query parameter appears in the output.

## Adding a source

A source owns only its own files, so that sources can be built side by side:

- `source/<source>.go`: the decoder; helpers in `source/<source>_<what>.go`, tests in `source/<source>_test.go`, fixtures
  in `source/testdata/<source>/`.
- One line in `source/registry.go`'s `All()`, in the order of the ids.

A relay route of `reporter forward` names its source in `decode` (`"decode": "zipkin"`); the forwarder
(`internal/forward`) then does what this page describes, and its tests replay every recording in `source/testdata`
through a route to check that the backend gets each request unchanged and impact.ai exactly what the decoder makes of it.
`zipkin` (Zipkin v2 JSON) is the generic protocol the relay is proven with. Its tags are OpenTelemetry attributes when
an OpenTelemetry SDK exports to Zipkin, so a tag that impact.ai withholds in Metadata only mode keeps its name
(`source/zipkin_content.go` holds that list) and other content goes under `zipkin.content.`.

The relay core, this page and `otlp` are shared: a change there is its own change. Like the rest of the reporter,
decoders use the Go standard library only.
