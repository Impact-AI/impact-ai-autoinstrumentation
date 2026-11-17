# SPDX-License-Identifier: Apache-2.0

"""The wire shape of the setup span's two lists, pinned.

The impact.ai service stores `impact.setup.frameworks` and `impact.setup.untraced` and drops an entry that does not
have exactly this shape, so it is checked on the JSON that is sent, not on the Python that builds it:

* OTLP string arrays: `arrayValue` of `stringValue`, never one joined string;
* an entry is `name` or `name version`, one space between, name <= 64 characters and version <= 64 characters;
* at most 20 entries per list; when more are found the 20 most relevant are kept, agent frameworks first and then
  provider SDKs;
* both lists on every report, stood down or not, and empty when nothing is found: nothing is omitted silently.
"""

from __future__ import annotations

import json
import re

import pytest
from test_bootstrap import (  # noqa: F401 - the real-interpreter fixtures
    PROVEN,
    STATE,
    add_agent_instrumentor,
    layout,
    qualification_table,
    setup_reports,
    sink,
    wait_for,
)

from impact_instrumentation import bootstrap, coexistence, detect, frameworks, qualification, setup_report

LISTS = ("impact.setup.frameworks", "impact.setup.untraced")
IMAGE = frozenset({"openai", "anthropic", "openai_agents", "langchain"})  # what the image traces
QUALIFIED = qualification_table(*PROVEN)  # and what its qualification proves of it, on this Python
ENTRY = re.compile(r"^\S{1,64}( \S{1,64})?$")
MAX_ENTRIES = 20


@pytest.fixture(autouse=True)
def the_image_traces_what_it_ships(monkeypatch):
    # A source tree has no generated registry and no qualification; the payload has both, and they say these.
    monkeypatch.setattr(frameworks, "shipped", lambda: IMAGE)
    monkeypatch.setattr(qualification, "load", lambda: QUALIFIED)


def setup_span(payload: dict) -> dict:
    (resource_spans,) = payload["resourceSpans"]
    ((scope_spans),) = resource_spans["scopeSpans"]
    (span,) = scope_spans["spans"]
    assert span["name"] == "impact.setup"
    return span


def raw_lists(span: dict) -> dict[str, dict]:
    """The attributes of the span that carry the lists, as they are on the wire."""
    return {a["key"]: a["value"] for a in span["attributes"] if a["key"] in LISTS}


def sent(dists: dict, environ: dict | None = None) -> dict[str, dict]:
    """What the package puts on the wire for an environment: its report, as the JSON the gateway receives."""
    env = environ or {}
    payload = setup_report.build("active", "", environ=env, extra=bootstrap._survey(dists, env))
    return raw_lists(setup_span(json.loads(json.dumps(payload))))


def entries(wire: dict) -> list[str]:
    assert set(wire) == {"arrayValue"}
    assert set(wire["arrayValue"]) == {"values"}
    values = wire["arrayValue"]["values"]
    assert all(set(item) == {"stringValue"} for item in values)
    assert all(isinstance(item["stringValue"], str) for item in values)
    return [item["stringValue"] for item in values]


def test_both_lists_are_otlp_string_arrays_not_joined_strings():
    wire = sent({"openai-agents": "0.22.1", "langgraph": "1.2.11", "openai": "3.42.1", "crewai": "1.15.35"})
    assert set(wire) == set(LISTS)
    assert wire["impact.setup.frameworks"] == {
        "arrayValue": {
            "values": [
                {"stringValue": "crewai 1.15.35"},
                {"stringValue": "langgraph 1.2.11"},
                {"stringValue": "openai-agents 0.22.1"},
                {"stringValue": "openai 3.42.1"},
            ]
        }
    }
    assert wire["impact.setup.untraced"] == {"arrayValue": {"values": [{"stringValue": "crewai 1.15.35"}]}}


@pytest.mark.parametrize(
    "dists",
    [
        {},  # nothing found
        {"flask": "3.1.3", "boto3": "1.43.123"},  # no AI library
        {"openai": "3.42.1"},  # nothing untraced
        {"crewai": "1.15.35", "mcp": "2.2.0", "openai-agents": "0.22.3", "langchain": "1.4.7", "openai": "3.42.1"},
    ],
)
def test_both_lists_are_always_there_and_empty_when_nothing_is_found(dists):
    wire = sent(dists)
    assert set(wire) == set(LISTS)  # nothing omitted
    for value in wire.values():
        assert isinstance(entries(value), list)
    if not any(name in frameworks.CATALOG for name in dists):
        assert wire["impact.setup.frameworks"] == {"arrayValue": {"values": []}}
        assert wire["impact.setup.untraced"] == {"arrayValue": {"values": []}}


def test_an_entry_is_a_name_or_a_name_and_a_version_with_one_space():
    wire = sent({"openai": "3.42.1", "mcp": None, "crewai": "1.15.35"})
    got = entries(wire["impact.setup.frameworks"])
    assert sorted(got) == ["crewai 1.15.35", "mcp", "openai 3.42.1"]
    assert all(ENTRY.match(entry) for entry in got)


def test_no_name_in_the_catalogue_is_too_long_or_has_a_space():
    assert max(len(name) for name in frameworks.CATALOG) <= 64
    assert not [name for name in frameworks.CATALOG if not ENTRY.match(name)]


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.3", "openai 1.2.3"),
        ("9" * 64, "openai " + "9" * 64),  # exactly the limit
        ("9" * 65, "openai " + "9" * 64),  # one over: cut
        ("1.0 beta", "openai 1.0beta"),  # one space between name and version, none inside the version
        ("1.0\tbeta\n", "openai 1.0beta"),
        ("  ", "openai"),  # nothing left of the version: the name alone
        ("", "openai"),
        (None, "openai"),
    ],
)
def test_a_version_is_cut_to_64_characters_and_has_no_whitespace(version, expected):
    (entry,) = entries(sent({"openai": version})["impact.setup.frameworks"])
    assert entry == expected
    assert ENTRY.match(entry)


def test_at_most_twenty_entries_agent_frameworks_before_provider_sdks():
    every = {name: "1.0.0" for name in frameworks.CATALOG}
    wire = sent(every)
    for key in LISTS:
        got = entries(wire[key])
        assert len(got) <= MAX_ENTRIES, key
        assert all(ENTRY.match(entry) for entry in got), key
    kept = entries(wire["impact.setup.frameworks"])
    assert len(kept) == MAX_ENTRIES
    kinds = [frameworks.CATALOG[entry.split()[0]].kind for entry in kept]
    assert kinds == sorted(kinds, key=("agent", "provider").index)  # agent frameworks first, then provider SDKs


def test_when_there_are_more_than_twenty_the_agent_frameworks_win_over_the_provider_sdks():
    agents = [n for n, e in frameworks.CATALOG.items() if e.kind == "agent" and n != "langchain-core"][:12]
    providers = [n for n, e in frameworks.CATALOG.items() if e.kind == "provider"]
    assert len(providers) >= 10
    found = entries(sent({n: "1.0" for n in [*agents, *providers]})["impact.setup.frameworks"])
    assert len(found) == MAX_ENTRIES
    names = [entry.split()[0] for entry in found]
    assert set(agents) <= set(names)  # every agent framework found is kept
    assert len([n for n in names if n in providers]) == MAX_ENTRIES - len(agents)  # the provider SDKs fill the rest


def test_the_untraced_list_is_cut_the_same_way():
    untraced_agents = [
        n for n, e in frameworks.CATALOG.items() if e.kind == "agent" and e.tracer is None and n != "langchain-core"
    ]
    providers = [n for n, e in frameworks.CATALOG.items() if e.kind == "provider" and e.tracer is None]
    wire = sent({n: "1.0" for n in [*untraced_agents, *providers]})
    untraced = entries(wire["impact.setup.untraced"])
    assert len(untraced) == min(MAX_ENTRIES, len(untraced_agents) + len(providers))
    assert [e.split()[0] for e in untraced[: min(MAX_ENTRIES, len(untraced_agents))]] == sorted(untraced_agents)[
        :MAX_ENTRIES
    ]


def test_a_catalogue_that_fails_still_sends_both_lists(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(frameworks, "survey", broken)
    wire = sent({"openai": "3.42.1"})
    assert wire == {key: {"arrayValue": {"values": []}} for key in LISTS}


def test_a_stood_down_start_sends_both_lists_on_the_wire(layout, sink):  # noqa: F811
    for dist in ("ddtrace-2.9.1", "openai_agents-0.22.1", "crewai-1.15.35"):
        layout.add_installed(dist)
    add_agent_instrumentor(layout, "openai_agents", "openai-agents", ">=0.3.3")
    layout.run(STATE, layout.env(sink))
    assert wait_for(lambda: any(s["name"] == "impact.setup" for s in sink.spans()))
    span = next(s for s in sink.spans() if s["name"] == "impact.setup")
    state = next(a["value"] for a in span["attributes"] if a["key"] == "impact.setup.state")
    assert state == {"stringValue": "pre-existing"}
    wire = raw_lists(span)
    assert entries(wire["impact.setup.frameworks"]) == ["crewai 1.15.35", "openai-agents 0.22.1"]
    assert entries(wire["impact.setup.untraced"]) == ["crewai 1.15.35"]
    assert len(setup_reports(sink)) == 1


def test_a_stood_down_start_with_nothing_to_list_still_sends_both_empty(layout, sink):  # noqa: F811
    layout.add_installed("ddtrace-2.9.1")
    layout.run(STATE, layout.env(sink))
    assert wait_for(lambda: any(s["name"] == "impact.setup" for s in sink.spans()))
    wire = raw_lists(next(s for s in sink.spans() if s["name"] == "impact.setup"))
    assert wire == {key: {"arrayValue": {"values": []}} for key in LISTS}


# --- the tool beside the package -------------------------------------------------------------------------------------
# The service keeps `impact.setup.tool`, `impact.setup.tool.version` and `impact.setup.exporter` only as strings of
# these shapes, and `impact.setup.alongside` only as a boolean (a string "true" says nothing); it drops anything else.

TOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOOL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}$")
EXPORTER_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# The tool ids impact.ai's service knows; a report names no other.
KNOWN_TOOLS = {
    "arize-ax",
    "braintrust",
    "datadog",
    "dynatrace",
    "elastic-edot",
    "grafana-alloy",
    "honeycomb",
    "langfuse",
    "langsmith",
    "litellm",
    "logfire",
    "newrelic",
    "openlit",
    "opentelemetry",
    "phoenix",
    "portkey",
    "splunk-otel",
    "traceloop",
    "zipkin",
}


def wire(tool, started: bool) -> dict[str, dict]:
    """The report's tool attributes as the gateway receives them."""
    payload = setup_report.build("pre-existing", "x", environ={}, extra=coexistence.attributes(tool, started))
    keys = ("impact.setup.tool", "impact.setup.tool.version", "impact.setup.exporter", "impact.setup.alongside")
    return {a["key"]: a["value"] for a in setup_span(json.loads(json.dumps(payload)))["attributes"] if a["key"] in keys}


def test_the_tool_fields_are_strings_of_the_shapes_the_service_keeps_and_alongside_is_a_boolean():
    tool = detect.Tool("langsmith", "0.14.17", "otlp-http")
    assert wire(tool, False) == {
        "impact.setup.tool": {"stringValue": "langsmith"},
        "impact.setup.tool.version": {"stringValue": "0.14.17"},
        "impact.setup.exporter": {"stringValue": "otlp-http"},
        "impact.setup.alongside": {"boolValue": False},
    }
    assert wire(tool, True)["impact.setup.alongside"] == {"boolValue": True}
    # A report that names no tool has none of the four.
    assert wire(None, True) == {}


def test_every_tool_the_package_can_name_is_one_the_service_knows():
    named = set(detect.TOOLS.values()) | set(detect.TOOL_DISTRIBUTIONS)
    assert named <= KNOWN_TOOLS
    assert all(TOOL_ID.match(tool) for tool in named)


def test_every_exporter_kind_the_package_can_name_has_the_shape_the_service_keeps():
    kinds = set(detect.TRACES_EXPORTERS) | set(detect.OTLP_PROTOCOLS.values()) | {detect.LANGSMITH_EXPORTER, "zipkin"}
    assert all(EXPORTER_KIND.match(kind) for kind in kinds)


@pytest.mark.parametrize("version", ["0.14.17", "1.0.0rc1", "2.0.0+local.1", "1!2.0", "4.16.1.post1"])
def test_a_version_the_package_names_has_the_shape_the_service_keeps(version):
    findings = [detect.Finding("distribution", "ddtrace", version)]
    tool = detect.tool(findings, {"ddtrace": version}, {})
    assert tool.version == version
    assert TOOL_VERSION.match(tool.version)
