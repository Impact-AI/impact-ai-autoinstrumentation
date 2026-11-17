# SPDX-License-Identifier: Apache-2.0

"""Node.js coverage (image side): a Node.js workload that calls OpenAI or Anthropic is covered by the payload alone, on
Node 22, 24 and 26. Prompt and answer arrive on the span (mock providers), exactly one span per model call, with the
placement of the workload on the resource and the span under the request that made it."""

from __future__ import annotations

import json

import pytest

from containers import http, wait_until
from nodejs_support import (
    ANSWER,
    ANTHROPIC_MODEL,
    NODES,
    OPENAI_MODEL,
    PROMPT,
    app_image,
    ask,
    has_content,
    only,
    readable,
    require_images,
    settle,
    setup_reports,
    text_of,
    wait_ready,
)

CALLS = 3


def start_express(start_app, node, **kwargs):
    require_images(app_image(node))
    app = start_app(node, ["node", "express.cjs"], directory="/apps/express", **kwargs)
    wait_ready(app)
    return app


@pytest.mark.nodejs
@pytest.mark.parametrize("node", NODES)
def test_an_openai_call_arrives_as_one_span_with_content_model_tokens_and_placement(start_app, mock, node):
    app = start_express(start_app, node)
    for _ in range(CALLS):
        status, body = ask(app, "openai")
        assert status == 200 and body == {"answer": ANSWER}, (
            body,
            app.logs(),
        )  # the application behaves as without injection
    spans = settle(mock, CALLS)
    assert len(spans) == CALLS, [s["name"] for s in spans]  # exactly one span per model call
    for span in spans:
        attrs = span["attrs"]
        assert span["name"] == "OpenAI Chat Completions"
        assert attrs["openinference.span.kind"] == "LLM"
        assert attrs["llm.model_name"] == OPENAI_MODEL
        assert attrs["llm.token_count.prompt"] == 7 and attrs["llm.token_count.completion"] == 5
        # content is on the span itself: the messages in the OpenInference form, and the whole request and response
        assert attrs["llm.input_messages.0.message.content"] == "be brief"
        assert attrs["llm.input_messages.1.message.content"] == PROMPT
        assert attrs["llm.output_messages.0.message.content"] == ANSWER
        assert PROMPT in attrs["input.value"] and ANSWER in attrs["output.value"]
        assert has_content(span)
        # placement: the Operator's k8s.* attributes ride on the resource, and the service is named after the workload
        assert span["resource"]["k8s.namespace.name"] == "shop"
        assert span["resource"]["k8s.deployment.name"] == "support-assistant"
        assert span["resource"]["k8s.pod.name"] == "support-assistant-6d9c-x2"
        assert span["resource"]["service.name"] == "support-assistant"
        assert span["resource"]["telemetry.sdk.language"] == "nodejs"
    # nothing else claims to be a model call: no second instrumentation wrote a span of its own for the same call
    assert not [s for s in mock.spans() if any(k.startswith("gen_ai.") for k in s["attrs"])]


@pytest.mark.nodejs
@pytest.mark.parametrize("node", NODES)
def test_an_anthropic_call_arrives_as_one_span_with_content_model_tokens_and_placement(start_app, mock, node):
    app = start_express(start_app, node)
    for _ in range(CALLS):
        status, body = ask(app, "anthropic")
        assert status == 200 and body == {"answer": ANSWER}, (body, app.logs())
    spans = settle(mock, CALLS)
    assert len(spans) == CALLS, [s["name"] for s in spans]
    for span in spans:
        attrs = span["attrs"]
        assert span["name"] == "anthropic.messages.create"
        assert span["scope"].startswith("com.anthropic.sdk")
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert attrs["gen_ai.request.model"] == ANTHROPIC_MODEL
        assert attrs["gen_ai.usage.input_tokens"] == 7 and attrs["gen_ai.usage.output_tokens"] == 5
        assert PROMPT in attrs["gen_ai.input.messages"]
        assert ANSWER in attrs["gen_ai.output.messages"]
        assert "be brief" in attrs["gen_ai.system_instructions"]
        assert span["resource"]["k8s.namespace.name"] == "shop"
        assert span["resource"]["k8s.deployment.name"] == "support-assistant"
        assert span["resource"]["service.name"] == "support-assistant"


@pytest.mark.nodejs
def test_a_request_to_anthropic_carries_one_traceparent_not_two(start_app, mock, tmp_path):
    """The SDK sends the trace context of the call to Anthropic with the propagator the package registers, and nothing
    else in the process does (an undici instrumentation would send it again, which makes it invalid: Anthropic's
    documentation says so, and it is why the package has none). The provider here is a Node server that keeps the
    headers
    as they came, duplicates included."""
    stub = tmp_path / "stub.cjs"
    stub.write_text(
        "const http = require('node:http');\n"
        "const seen = [];\n"
        "http.createServer((req, res) => {\n"
        "  if (req.url === '/_raw') return res.end(JSON.stringify(seen));\n"
        "  seen.push(req.rawHeaders.filter((v, i) => i % 2 === 0).map((h) => h.toLowerCase()));\n"
        "  res.setHeader('content-type', 'application/json');\n"
        "  res.end(JSON.stringify({ id: 'msg_stub', type: 'message', role: 'assistant', model: 'claude-stub',\n"
        "    content: [{ type: 'text', text: 'Hello from the stub.' }], stop_reason: 'end_turn', stop_sequence: null,\n"
        "    usage: { input_tokens: 7, output_tokens: 5 } }));\n"
        "}).listen(8080);\n"
    )
    readable(stub)
    provider = start_app(
        "22",
        ["node", "/work/stub.cjs"],
        directory="/apps/express",
        inject=False,
        mounts={str(stub): "/work/stub.cjs:ro"},
        publish=8080,
    )
    wait_until(lambda: http("GET", f"{provider.url}/_raw")[0] == 200, timeout=60, message="the stub provider")
    provider.extra["internal_port"] = 8080
    app = start_express(start_app, "22", env={"STUB_URL": provider.internal_url})
    assert ask(app, "anthropic") == (200, {"answer": ANSWER})
    settle(mock, 1)
    (headers,) = http("GET", f"{provider.url}/_raw")[1]
    assert headers.count("traceparent") == 1, headers


@pytest.mark.nodejs
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_the_call_hangs_under_the_request_that_made_it_and_the_probe_is_not_traced(start_app, mock, provider):
    app = start_express(start_app, "22")
    for _ in range(3):
        http("GET", f"{app.url}/healthz")
    ask(app, provider)
    llm = only(settle(mock, 1), "model-call span")
    wait_until(lambda: [s for s in mock.spans() if s.get("kind") == 2], timeout=30, message="the request span")
    spans = mock.spans()
    by_id = {s["spanId"]: s for s in spans}
    root = llm
    while root.get("parentSpanId") in by_id:  # climb to the request span: the one that has no parent in this process
        root = by_id[root["parentSpanId"]]
    assert root.get("kind") == 2 and root["traceId"] == llm["traceId"], [
        (s["name"], s.get("kind")) for s in spans
    ]  # SERVER
    assert root["attrs"]["http.route"] == f"/chat/{provider}"  # Express names the route on the request span
    # health probes are not traced
    assert not [s for s in spans if "/healthz" in json.dumps(s["attrs"]) or "/healthz" in s["name"]]


@pytest.mark.nodejs
def test_the_setup_report_names_what_is_armed_and_the_libraries_found(start_app, mock):
    app = start_express(start_app, "22")
    ask(app, "openai")
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    report = only(setup_reports(mock), "setup report")
    attrs = report["attrs"]
    assert attrs["impact.setup.state"] == "active"
    assert set(attrs["impact.setup.instrumentations"].split(",")) == {"anthropic", "express", "http", "openai"}
    assert [e.split(" ")[0] for e in attrs["impact.setup.frameworks"]] == ["anthropic-ai-sdk", "openai"]
    assert attrs["impact.setup.untraced"] == []
    assert attrs["impact.setup.node.version"].startswith("22.") and attrs["impact.setup.node.tree"] == "node22"
    assert "impact.setup.skipped" not in attrs
    # the wire shape: arrays are arrays of strings, in the span and not joined. The report is looked for by name, in
    # whichever export carried it: the exports of a process are not ordered against each other.
    on_the_wire = [
        span
        for request in mock.requests("/v1/traces")
        for resource_spans in (request.get("json") or {}).get("resourceSpans", [])
        for scope_spans in resource_spans["scopeSpans"]
        for span in scope_spans["spans"]
        if span["name"] == "impact.setup"
    ]
    by_key = {a["key"]: a["value"] for a in only(on_the_wire, "setup span on the wire")["attributes"]}
    assert "arrayValue" in by_key["impact.setup.frameworks"] and "arrayValue" in by_key["impact.setup.untraced"]


@pytest.mark.nodejs
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_content_stays_off_the_span_when_the_workload_asks_for_no_content(start_app, mock, provider):
    app = start_express(start_app, "22", env={"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT"})
    ask(app, provider)
    span = only(settle(mock, 1), "model-call span")
    assert PROMPT not in text_of(span) and ANSWER not in text_of(span), span["attrs"]
    # what is not content is still there
    assert span["attrs"].get("llm.token_count.prompt", span["attrs"].get("gen_ai.usage.input_tokens")) == 7


@pytest.mark.nodejs
def test_the_workloads_own_settings_win_over_the_defaults(start_app, mock):
    app = start_express(
        start_app,
        "22",
        env={"ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE": "metadata_only", "OTEL_LOGS_EXPORTER": "otlp"},
    )
    status, runtime = http("GET", f"{app.url}/debug/runtime")
    assert status == 200
    assert runtime["environment"]["ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE"] == "metadata_only"
    assert runtime["environment"]["OTEL_LOGS_EXPORTER"] == "otlp"
    ask(app, "anthropic")
    span = only(settle(mock, 1), "model-call span")
    assert "gen_ai.input.messages" not in span["attrs"]


@pytest.mark.nodejs
def test_the_application_is_not_changed(start_app, mock):
    """The same image, same code, without the payload: identical answers, and nothing reaches impact.ai."""
    require_images(app_image("22"))
    plain = start_app("22", ["node", "express.cjs"], directory="/apps/express", inject=False)
    wait_ready(plain)
    assert ask(plain, "openai") == (200, {"answer": ANSWER})
    assert ask(plain, "anthropic") == (200, {"answer": ANSWER})
    assert mock.spans() == []
    status, runtime = http("GET", f"{plain.url}/debug/runtime")
    assert runtime["nodeOptions"] is None and runtime["environment"] == {}


@pytest.mark.nodejs
def test_nodes_options_of_the_application_are_kept_and_ours_is_added(start_app, mock):
    app = start_express(start_app, "22", node_options="--max-old-space-size=200")
    status, runtime = http("GET", f"{app.url}/debug/runtime")
    assert runtime["nodeOptions"].startswith("--max-old-space-size=200 --require ")
    assert runtime["nodeOptions"].endswith("/otel-auto-instrumentation-nodejs/autoinstrumentation.js")


@pytest.mark.nodejs
def test_workers_started_by_the_application_are_instrumented_too(start_app, mock, tmp_path):
    """node:cluster forks workers that inherit NODE_OPTIONS; each starts the package, one report is made for the
    container."""
    script = tmp_path / "cluster.cjs"
    script.write_text(
        "const cluster = require('node:cluster');\n"
        "if (cluster.isPrimary) { cluster.fork(); cluster.fork(); } else { require('/apps/express/express.cjs'); }\n"
    )
    readable(script)
    require_images(app_image("22"))
    app = start_app(
        "22", ["node", "/work/cluster.cjs"], directory="/apps/express", mounts={str(script): "/work/cluster.cjs:ro"}
    )
    wait_ready(app)
    for _ in range(4):
        assert ask(app, "openai")[0] == 200
    settle(mock, 4)
    assert len(setup_reports(mock)) == 1


STREAMING_STUB = """\
const http = require('node:http');
http.createServer((req, res) => {
  if (req.url === '/healthz') return res.end('ok');
  let body = '';
  req.on('data', (chunk) => (body += chunk));
  req.on('end', () => {
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const base = { id: 'chatcmpl-stub', object: 'chat.completion.chunk', created: 1, model: 'gpt-4o-mini' };
    const send = (delta, finish) => {
      const chunk = { ...base, choices: [{ index: 0, delta, finish_reason: finish }] };
      res.write('data: ' + JSON.stringify(chunk) + '\\n\\n');
    };
    send({ role: 'assistant', content: '' }, null);
    for (const word of ['Hello', ' from', ' the', ' stub.']) send({ content: word }, null);
    send({}, 'stop');
    res.end('data: [DONE]\\n\\n');
  });
}).listen(8080);
"""


@pytest.mark.nodejs
def test_a_streamed_openai_answer_reaches_the_application_whole_and_is_traced_once(start_app, mock, tmp_path):
    """Streaming is how most chat applications call a model. The instrumentation reads the stream as well, from a copy
    of it
    (OpenInference tees it): the application must get every chunk, and the call must be one span."""
    stub = tmp_path / "stream.cjs"
    stub.write_text(STREAMING_STUB)
    readable(stub)
    provider = start_app(
        "22", ["node", "/work/stream.cjs"], inject=False, mounts={str(stub): "/work/stream.cjs:ro"}, publish=8080
    )
    wait_until(lambda: http("GET", f"{provider.url}/healthz")[0] == 200, timeout=60, message="the streaming provider")
    provider.extra["internal_port"] = 8080
    app = start_express(start_app, "22", env={"STUB_URL": provider.internal_url})
    status, body = http("POST", f"{app.url}/chat/openai", {"prompt": PROMPT, "stream": True})
    assert (status, body) == (200, {"answer": ANSWER}), app.logs()
    span = only(settle(mock, 1), "model-call span")
    assert span["name"] == "OpenAI Chat Completions"
    assert ANSWER in span["attrs"].get("output.value", "") + span["attrs"].get(
        "llm.output_messages.0.message.content", ""
    )
