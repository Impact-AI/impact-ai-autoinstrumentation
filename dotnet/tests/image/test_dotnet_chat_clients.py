# SPDX-License-Identifier: Apache-2.0

"""Proof first: can a startup hook make the UseOpenTelemetry() call that Microsoft.Extensions.AI and Microsoft
Agent Framework ask for in code?

The upstream agent (1.18.0) has no instrumentation of its own for either library: Microsoft.Extensions.AI records
its GenAI spans only on a client wrapped with UseOpenTelemetry(). The kit's startup hook, named first in
DOTNET_STARTUP_HOOKS by the Instrumentation's env, wraps every IChatClient the application registers in its services
while the host is built. What it reaches and what it cannot:

  - a client registered with AddChatClient (the common way, and the way ASP.NET Core apps get one): traced, with
    provider, model, tokens and, as the content setting says, content, inside the request's trace;
  - an Agent Framework agent over that client: its model calls are traced; the agent's own span (invoke_agent) is not,
    because the agent is built in code;
  - a client the code builds outside the services: not reached; its calls show as HTTP client spans only;
  - a client registered by type under a key: left as it is, because its constructor may take the key, which only the
    container can give; the hook says so;
  - an application without Microsoft.Extensions.AI, which registers a client with the library's abstractions and a
    provider's package alone: nothing is wrapped, because UseOpenTelemetry() is not in them; the hook says so, and
    the application answers as without the kit.
"""

from __future__ import annotations

import json
import time

import pytest
from dotnet_support import DOTNETS, LITE, agent_spans, chat_spans, http, server_span, wait_ready, wait_until

pytestmark = pytest.mark.dotnet

QUESTION = "Where is my order?"
ANSWER = "Hello from the stub."


def call(app, path: str) -> None:
    status, answer = http("POST", f"{app.url}{path}", {})
    assert status == 200 and ANSWER in str(answer), (status, answer, app.logs()[-3000:])


def wait_for_chat(app, mock) -> dict:
    found = wait_until(lambda: chat_spans(mock), timeout=60, message=lambda: f"the chat span\n{app.logs()[-5000:]}")
    return found[0]


@pytest.mark.parametrize("dotnet", DOTNETS)
def test_a_registered_chat_client_is_traced_without_code(start_app, mock, dotnet):
    app = start_app(dotnet=dotnet)
    wait_ready(app)
    call(app, "/chat")
    span = wait_for_chat(app, mock)
    assert span["scope"] == "Experimental.Microsoft.Extensions.AI"
    assert span["name"] == "chat gpt-4o-mini"
    attrs = span["attrs"]
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert attrs["gen_ai.usage.output_tokens"] == 5
    # content, because the Instrumentation sets OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
    assert QUESTION in json.dumps(attrs["gen_ai.input.messages"])
    assert ANSWER in json.dumps(attrs["gen_ai.output.messages"])
    # inside the request's trace, under the upstream agent's ASP.NET Core span, with the workload's name
    server = wait_until(lambda: server_span(mock, "/chat"), timeout=30, message="the request's span")
    assert span["trace_id"] == server["trace_id"]
    assert span["resource"]["service.name"] == "support-assistant"
    assert "impact.ai: chat clients wrapped: 1" in app.logs()


def test_content_follows_the_setting(start_app, mock):
    app = start_app(spec={"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false"})
    wait_ready(app)
    call(app, "/chat")
    attrs = wait_for_chat(app, mock)["attrs"]
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs


def test_without_the_hook_the_upstream_agent_records_no_model_call(start_app, mock):
    """The baseline: the same Instrumentation without the kit's hook. The request and its HTTP call arrive, the model
    call does not, although the activity source is named: nothing calls UseOpenTelemetry()."""
    app = start_app(hook=False, spec={"OTEL_EXPORTER_OTLP_ENDPOINT": mock.internal_url})
    wait_ready(app)
    call(app, "/chat")
    wait_until(lambda: server_span(mock, "/chat"), timeout=60, message=lambda: f"the request's span\n{app.logs()}")
    time.sleep(1.5)
    assert chat_spans(mock) == []
    assert "impact.ai:" not in app.logs()


def test_an_agent_framework_agent_over_a_registered_client(start_app, mock):
    app = start_app()
    wait_ready(app)
    call(app, "/agent")
    span = wait_for_chat(app, mock)
    assert span["attrs"]["gen_ai.request.model"] == "gpt-4o-mini"
    assert "You answer questions about orders." in json.dumps(span["attrs"].get("gen_ai.system_instructions"))
    time.sleep(1.5)
    # The agent's own span needs AIAgent.AsBuilder().UseOpenTelemetry() on an agent the code builds: not reached.
    assert not [s for s in agent_spans(mock) if s["name"].startswith("invoke_agent")]


def test_a_client_built_in_code_is_not_reached(start_app, mock):
    app = start_app()
    wait_ready(app)
    call(app, "/chat-in-code")
    wait_until(lambda: server_span(mock, "/chat-in-code"), timeout=60, message=lambda: f"the span\n{app.logs()}")
    time.sleep(1.5)
    assert chat_spans(mock) == []
    calls = [s for s in agent_spans(mock) if "/v1/chat/completions" in str(s["attrs"].get("url.full", ""))]
    assert calls, "the call to the provider shows as an HTTP client span"


def test_a_client_registered_by_type_under_a_key_is_left_as_it_is(start_app, mock):
    """The container gives the client its key ([ServiceKey]); a factory of the hook's could not, and the request would
    fail. The hook leaves the registration and names what it left."""
    app = start_app()
    wait_ready(app)
    call(app, "/chat-keyed")
    call(app, "/chat-keyed")
    assert "impact.ai: chat clients wrapped: 1; not wrapped: 1 registered by type under a key" in app.logs()


def test_an_application_without_microsoft_extensions_ai_answers_as_without_the_kit(start_app, mock):
    """The hook finds a registered IChatClient but no Microsoft.Extensions.AI to wrap it with: it leaves the services as
    they are and says why. The request is still traced by the upstream agent; the model call is not."""
    app = start_app(command=LITE)
    wait_ready(app)
    call(app, "/chat")
    call(app, "/chat")
    wait_until(lambda: server_span(mock, "/chat"), timeout=60, message=lambda: f"the request's span\n{app.logs()}")
    time.sleep(1.5)
    assert chat_spans(mock) == []
    logs = app.logs()
    assert "impact.ai: chat clients not wrapped: Microsoft.Extensions.AI not found" in logs, logs[-5000:]
    assert "chat client not wrapped:" not in logs
