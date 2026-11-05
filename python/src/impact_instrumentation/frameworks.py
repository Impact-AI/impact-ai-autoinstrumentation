# SPDX-License-Identifier: Apache-2.0

"""The AI libraries and agent frameworks found in the application's environment, for the `impact.setup` span (C8).

They are read from the installed distributions that `detect.scan` already listed, a directory listing: nothing of the
application is imported or run. Each one is either traced by an instrumentation this image ships or it is not;
`untraced` names the ones the image cannot trace (yet), so that Cover your AI can say what is missing rather than show
the workload as covered.

Names are distribution names (PEP 503), as `pip list` prints them: `openai-agents`, not the module `agents`.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Collection, Mapping

from impact_instrumentation import defaults

# The wire shape is fixed by the monorepo, which drops an entry that does not fit it: at most 20 entries per list,
# each "name" or "name version", one space between, name and version at most 64 characters.
MAX_ENTRIES = 20
MAX_NAME = 64
MAX_VERSION = 64

Entry = namedtuple("Entry", ["kind", "tracer"])
# kind: "agent" (agent and orchestration frameworks) or "provider" (model provider SDKs). When more are found than a
# list holds, agent frameworks are kept before provider SDKs.
RANK = {"agent": 0, "provider": 1}

# distribution -> its kind, and the instrumentation of the payload that traces it (its name in the registry), or None
# when the image has none.
CATALOG: dict[str, Entry] = {
    "openai-agents": Entry("agent", "openai_agents"),
    # LangGraph runs on langchain-core, whose callbacks the LangChain instrumentation listens to.
    "langchain": Entry("agent", "langchain"),
    "langgraph": Entry("agent", "langchain"),
    "langchain-core": Entry("agent", "langchain"),
    "crewai": Entry("agent", None),
    "llama-index-core": Entry("agent", None),
    "mcp": Entry("agent", None),
    "pydantic-ai-slim": Entry("agent", None),
    "autogen-agentchat": Entry("agent", None),
    "pyautogen": Entry("agent", None),
    "ag2": Entry("agent", None),
    "agno": Entry("agent", None),
    "google-adk": Entry("agent", None),
    "semantic-kernel": Entry("agent", None),
    "smolagents": Entry("agent", None),
    "haystack-ai": Entry("agent", None),
    "dspy": Entry("agent", None),
    "dspy-ai": Entry("agent", None),
    "strands-agents": Entry("agent", None),
    "agent-framework-core": Entry("agent", None),
    "claude-agent-sdk": Entry("agent", None),
    "letta": Entry("agent", None),
    "griptape": Entry("agent", None),
    "phidata": Entry("agent", None),
    "openai": Entry("provider", "openai"),
    "anthropic": Entry("provider", "anthropic"),
    "google-genai": Entry("provider", None),
    "google-generativeai": Entry("provider", None),
    "google-cloud-aiplatform": Entry("provider", None),
    "cohere": Entry("provider", None),
    "mistralai": Entry("provider", None),
    "groq": Entry("provider", None),
    "ollama": Entry("provider", None),
    "together": Entry("provider", None),
    "replicate": Entry("provider", None),
    "litellm": Entry("provider", None),
}

# The Agents SDK creates no trace objects when its tracing is off, and the instrumentation listens to those.
_AGENTS_TRACING_OFF = "OPENAI_AGENTS_DISABLE_TRACING"
_TRUE = {"1", "true", "yes", "on"}


class Survey(namedtuple("Survey", ["frameworks", "untraced"])):
    """frameworks: every catalogued library found, as "name version"; untraced: those of them the image cannot trace."""

    __slots__ = ()

    def attributes(self) -> dict[str, list[str]]:
        return {"impact.setup.frameworks": list(self.frameworks), "impact.setup.untraced": list(self.untraced)}


def shipped() -> frozenset[str]:
    """The instrumentations this payload ships; none in a source tree (the registry is built with the image)."""
    try:
        from impact_instrumentation import _registry
    except ImportError:
        return frozenset()
    return frozenset(entry[0] for entry in _registry.INSTRUMENTATIONS)


def label(name: str, version: str | None) -> str:
    """`name` or `name version`: one space, and no whitespace inside the version, which is cut to 64 characters."""
    version = "".join((version or "").split())[:MAX_VERSION]
    return f"{name} {version}" if version else name


def survey(
    dists: Mapping[str, str | None],
    *,
    environ: Mapping[str, str],
    available: Collection[str] | None = None,
    skipped: Collection[str] = (),
) -> Survey:
    """The catalogued libraries among `dists` ({normalized name: version}), and which of them the image cannot trace.

    available: the instrumentations the payload ships (default: its registry). skipped: those whose library turned
    out to be outside the supported range, found when the application imported it. Agent frameworks come before provider
    SDKs, each alphabetically; a list holds at most MAX_ENTRIES of them.
    """
    shipped_here = shipped() if available is None else available
    off = defaults.disabled_instrumentations(environ)
    agents_off = (environ.get(_AGENTS_TRACING_OFF) or "").strip().lower() in _TRUE

    present = {name for name in CATALOG if name in dists}
    if present & {"langchain", "langgraph"}:
        present.discard("langchain-core")  # the larger package says it, once

    found, untraced = [], []
    for name in sorted(present, key=lambda n: (RANK[CATALOG[n].kind], n)):
        entry = label(name, dists[name])
        found.append(entry)
        tracer = CATALOG[name].tracer
        traced = (
            tracer is not None
            and tracer in shipped_here
            and tracer not in skipped
            and "*" not in off
            and tracer not in off
            and not (name == "openai-agents" and agents_off)
        )
        if not traced:
            untraced.append(entry)
    return Survey(found[:MAX_ENTRIES], untraced[:MAX_ENTRIES])
