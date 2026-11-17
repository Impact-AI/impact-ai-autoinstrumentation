# SPDX-License-Identifier: Apache-2.0

"""The AI libraries and agent frameworks found in the application's environment, for the `impact.setup` span (C8).

They are read from the installed distributions the start already listed (nothing of the application is imported), and
split into those the image traces and those it cannot trace yet.
"""

from __future__ import annotations

import pytest

from impact_instrumentation import frameworks

SHIPPED = {"openai", "anthropic", "openai_agents", "langchain", "bedrock", "fastapi"}  # instrumentations in the image


def survey(dists, *, environ=None, available=SHIPPED, skipped=()):
    return frameworks.survey(dists, environ=environ or {}, available=available, skipped=skipped)


def test_what_is_found_is_listed_with_its_version_by_distribution_name():
    found = survey(
        {
            "openai-agents": "0.22.1",
            "langgraph": "1.2.11",
            "openai": "3.42.1",
            "fastapi": "0.162.0",  # a web framework is not an AI library
            "pydantic": "2.14.2",
        }
    )
    # Agent frameworks first, then provider SDKs, each alphabetically.
    assert found.frameworks == ["langgraph 1.2.11", "openai-agents 0.22.1", "openai 3.42.1"]
    assert found.untraced == []


def test_nothing_found_is_two_empty_lists():
    found = survey({"flask": "3.1.3", "boto3": "1.43.123"})  # boto3 is everywhere: not an AI library by itself
    assert (found.frameworks, found.untraced) == ([], [])


def test_the_ones_the_image_cannot_trace_are_listed_and_marked_untraced():
    found = survey(
        {
            "crewai": "1.15.35",
            "llama-index-core": "0.14.25",
            "mcp": "2.2.0",
            "openai": "3.42.1",
            "mistralai": "2.1.0",
        }
    )
    assert found.frameworks == [
        "crewai 1.15.35",
        "llama-index-core 0.14.25",
        "mcp 2.2.0",
        "mistralai 2.1.0",
        "openai 3.42.1",
    ]
    assert found.untraced == ["crewai 1.15.35", "llama-index-core 0.14.25", "mcp 2.2.0", "mistralai 2.1.0"]


def test_google_genai_is_traced_by_the_instrumentation_of_the_same_name():
    # The Gemini API and Vertex AI, through Google's SDK: opentelemetry-instrumentation-google-genai.
    assert survey({"google-genai": "2.34.0"}, available=SHIPPED | {"google-genai"}).untraced == []
    assert survey({"google-genai": "2.34.0"}, available=SHIPPED).untraced == ["google-genai 2.34.0"]
    off = {"OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "google-genai"}
    assert survey({"google-genai": "2.34.0"}, environ=off, available=SHIPPED | {"google-genai"}).untraced == [
        "google-genai 2.34.0"
    ]


def test_the_older_vertex_ai_sdk_stays_untraced_beside_google_genai():
    # google-cloud-aiplatform's vertexai module is another library: google-genai's instrumentation does not see it.
    found = survey({"google-genai": "2.34.0", "google-cloud-aiplatform": "2.7.0"}, available=SHIPPED | {"google-genai"})
    assert found.frameworks == ["google-cloud-aiplatform 2.7.0", "google-genai 2.34.0"]
    assert found.untraced == ["google-cloud-aiplatform 2.7.0"]


def test_what_the_image_traces_is_never_untraced():
    traced = {
        "openai": "3.42.1",
        "anthropic": "1.17.0",
        "openai-agents": "0.22.3",
        "langchain": "1.4.7",
        "langgraph": "1.2.19",
    }
    found = survey(traced)
    assert len(found.frameworks) == 5
    assert found.untraced == []


def test_a_library_outside_the_supported_range_is_untraced():
    # The version check runs when the application imports the library, and the report is sent again with the skip.
    found = survey({"anthropic": "2.4.0", "openai": "3.42.1"}, skipped={"anthropic"})
    assert found.frameworks == ["anthropic 2.4.0", "openai 3.42.1"]
    assert found.untraced == ["anthropic 2.4.0"]


def test_an_instrumentation_the_workload_switched_off_leaves_its_library_untraced():
    found = survey(
        {"openai": "3.42.1", "anthropic": "1.17.0"}, environ={"OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "openai"}
    )
    assert found.untraced == ["openai 3.42.1"]
    everything = survey({"openai": "3.42.1"}, environ={"OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "*"})
    assert everything.untraced == ["openai 3.42.1"]


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", " yes "])
def test_openai_agents_with_the_sdks_tracing_switched_off_has_no_agent_spans(value):
    # The Agents SDK creates no trace objects when its tracing is off: nothing for the image to turn into spans.
    found = survey({"openai-agents": "0.22.3"}, environ={"OPENAI_AGENTS_DISABLE_TRACING": value})
    assert found.untraced == ["openai-agents 0.22.3"]


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no"])
def test_openai_agents_with_the_sdks_tracing_on_is_traced(value):
    found = survey({"openai-agents": "0.22.3"}, environ={"OPENAI_AGENTS_DISABLE_TRACING": value})
    assert found.untraced == []


def test_an_image_without_the_instrumentation_traces_nothing():
    # A source tree, or a payload built without the registry.
    found = survey({"openai": "3.42.1", "langgraph": "1.2.11"}, available=set())
    assert found.untraced == found.frameworks == ["langgraph 1.2.11", "openai 3.42.1"]  # agent framework, provider SDK


def test_langgraph_and_langchain_are_traced_by_the_same_instrumentation():
    without = survey({"langgraph": "1.2.11", "langchain-core": "1.6.13"}, available={"openai"})
    assert without.untraced == ["langgraph 1.2.11"]
    with_it = survey({"langgraph": "1.2.11", "langchain-core": "1.6.13"}, available={"openai", "langchain"})
    assert with_it.untraced == []


def test_langchain_core_is_listed_only_when_no_larger_langchain_package_is_there():
    assert survey({"langchain-core": "1.6.13", "langchain-openai": "1.6.7"}).frameworks == ["langchain-core 1.6.13"]
    both = survey({"langchain-core": "1.6.13", "langchain": "1.4.7", "langgraph": "1.2.19"})
    assert both.frameworks == ["langchain 1.4.7", "langgraph 1.2.19"]


def test_a_distribution_without_a_version_is_listed_by_name():
    assert survey({"openai": None}).frameworks == ["openai"]


def test_the_lists_are_bounded():
    every = {name: "1.0" for name in frameworks.CATALOG}
    found = survey(every, available=set())
    assert 0 < len(found.frameworks) <= frameworks.MAX_ENTRIES
    assert found.untraced == found.frameworks


def test_every_entry_is_a_normalized_distribution_name_and_every_tracer_is_one_the_image_ships():
    for name, entry in frameworks.CATALOG.items():
        assert name == name.lower().replace("_", "-"), name
        assert entry.kind in frameworks.RANK, name
        assert entry.tracer is None or entry.tracer in {
            "openai",
            "anthropic",
            "openai_agents",
            "langchain",
            "google-genai",
        }, name


def test_the_survey_only_reads_the_listing():
    # Nothing is imported: a distribution that is only a name in a dict is enough.
    found = survey({"crewai": "1.0.0"})
    assert found.frameworks == ["crewai 1.0.0"]


def test_the_attributes_for_the_setup_span_are_two_string_arrays():
    found = survey({"openai": "3.42.1", "mcp": "2.2.0"})
    assert found.attributes() == {
        "impact.setup.frameworks": ["mcp 2.2.0", "openai 3.42.1"],
        "impact.setup.untraced": ["mcp 2.2.0"],
    }
    assert survey({}).attributes() == {"impact.setup.frameworks": [], "impact.setup.untraced": []}
