# SPDX-License-Identifier: Apache-2.0

"""Which instrumentations a process arms for the libraries installed in the application's environment."""

from __future__ import annotations

import pytest

from impact_instrumentation import detect, runtime

REGISTRY = (
    ("anthropic", "m_anthropic", "AnthropicInstrumentor", (("anthropic", "<2,>=0.51.0"),), ()),
    ("langchain", "m_langchain", "LangChainInstrumentor", (("langchain", "<2,>=0.3.22"),), ()),
    ("openai_agents", "m_agents", "OpenAIAgentsInstrumentor", (("openai-agents", "<1,>=0.3.3"),), ()),
    ("psycopg", "m_psycopg", "PsycopgInstrumentor", (), (("psycopg", ">=3"), ("psycopg2", ">=2.7.3.1"))),
)


@pytest.fixture(autouse=True)
def the_registry(monkeypatch):
    monkeypatch.setattr(runtime, "registry", lambda: REGISTRY)


def plan(dists: dict[str, str], **environ: str):
    return {spec.name: spec for spec in runtime.plan(scan=detect.Scan(dists, set()), environ=environ)}


def test_a_library_that_is_not_installed_arms_nothing():
    assert plan({"flask": "3.1.3"}) == {}


def test_the_agents_sdk_is_imported_as_agents_and_not_as_its_distribution_name():
    spec = plan({"openai-agents": "0.22.3"})["openai_agents"]
    assert spec.libraries == ("agents",)


def test_langgraph_without_langchain_arms_the_langchain_instrumentation_through_langchain_core():
    spec = plan({"langgraph": "1.2.19", "langchain-core": "1.6.13"})["langchain"]
    assert spec.libraries == ("langchain_core",)
    assert runtime.unsupported(spec) == ""


def test_with_langchain_both_imports_arm_it():
    spec = plan({"langchain": "1.4.7", "langchain-core": "1.6.13"})["langchain"]
    assert spec.libraries == ("langchain", "langchain_core")
    assert runtime.unsupported(spec) == ""


def test_langchain_core_outside_its_range_is_not_supported():
    spec = plan({"langchain-core": "0.2.1"})["langchain"]
    assert "langchain-core 0.2.1 installed, supported <2,>=0.3.49" in runtime.unsupported(spec)


def test_one_langchain_package_inside_its_range_is_enough():
    spec = plan({"langchain": "1.4.7", "langchain-core": "0.2.1"})["langchain"]
    assert runtime.unsupported(spec) == ""


def test_an_instrumentation_with_other_triggers_than_the_alternatives_is_left_alone():
    assert plan({"anthropic": "1.17.0", "langchain-core": "1.6.13"})["anthropic"].libraries == ("anthropic",)
    assert "anthropic" not in plan({"langchain-core": "1.6.13"})


def test_disabled_instrumentations_are_not_armed():
    assert plan({"openai-agents": "0.22.3"}, OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="openai_agents") == {}
    assert plan({"openai-agents": "0.22.3"}, OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="*") == {}


def test_an_any_of_instrumentation_needs_one_of_its_libraries():
    assert plan({"psycopg2": "2.9.9"})["psycopg"].libraries == ("psycopg2",)
    assert plan({}) == {}
