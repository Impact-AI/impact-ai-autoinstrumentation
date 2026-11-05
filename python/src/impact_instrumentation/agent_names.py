# SPDX-License-Identifier: Apache-2.0

"""Names an agent the application left unnamed after its workload.

LangChain's `create_agent(model, tools=...)` without `name=` gives the instrumentation nothing to call the agent, so its
span is a bare `invoke_agent` with no `gen_ai.agent.name`. Cover your AI collects the agent names of a workload, so such
an agent is given the name of the workload (the service name) when its span starts. A name the application gave is
never replaced: the span is then called `invoke_agent <name>`, and a name the instrumentation sets later replaces this
default.

Imported only where the tracer provider is built, because it needs the OpenTelemetry SDK.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import SpanProcessor

AGENT_NAME = "gen_ai.agent.name"
UNNAMED = "invoke_agent"  # util-genai names an agent's span "invoke_agent <agent name>"; without a name, no suffix


class AgentNames(SpanProcessor):
    def on_start(self, span, parent_context=None) -> None:
        try:
            if span.name != UNNAMED or AGENT_NAME in (span.attributes or {}):
                return
            service = span.resource.attributes.get("service.name")
            if service:
                span.update_name(f"{UNNAMED} {service}")
                span.set_attribute(AGENT_NAME, str(service))
        except Exception:  # noqa: BLE001, S110 - a name is a convenience; the span is what matters
            pass
