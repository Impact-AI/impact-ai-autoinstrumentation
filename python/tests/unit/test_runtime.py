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
    assert runtime.unsupported(spec) == (
        "Impact AI traces langchain-core 0.3.49 to 1; this workload runs langchain-core 0.2.1"
    )


def test_the_reason_says_what_impact_ai_traces_and_then_what_the_workload_runs():
    # In the words of the Node.js package for an older Node, and of the qualification for an unproven version.
    spec = plan({"anthropic": "2.4.0"})["anthropic"]
    assert runtime.unsupported(spec) == "Impact AI traces anthropic 0.51.0 to 1; this workload runs anthropic 2.4.0"
    assert runtime.unsupported(plan({"anthropic": "1.17.0"})["anthropic"]) == ""


GOOGLE_GENAI = ("google-genai", "m_google", "GoogleGenAiSdkInstrumentor", (("google-genai", "<3,>=1.32.0"),), ())


def test_google_genai_is_applied_when_the_application_imports_google_genai(monkeypatch):
    # The package google.genai, inside the namespace package `google` that google-auth and protobuf share too.
    monkeypatch.setattr(runtime, "registry", lambda: (*REGISTRY, GOOGLE_GENAI))
    spec = plan({"google-genai": "2.34.0"})["google-genai"]
    assert spec.libraries == ("google.genai",)
    assert runtime.unsupported(spec) == ""


def test_a_google_genai_older_than_its_instrumentation_says_so(monkeypatch):
    # Inside the series a cell proves, a release can still be older than the instrumentation's floor.
    monkeypatch.setattr(runtime, "registry", lambda: (*REGISTRY, GOOGLE_GENAI))
    spec = plan({"google-genai": "1.20.0"})["google-genai"]
    assert (
        runtime.unsupported(spec) == "Impact AI traces google-genai 1.32.0 to 2; this workload runs google-genai 1.20.0"
    )


def test_google_genai_3_is_beyond_its_instrumentation(monkeypatch):
    monkeypatch.setattr(runtime, "registry", lambda: (*REGISTRY, GOOGLE_GENAI))
    spec = plan({"google-genai": "3.0.0"})["google-genai"]
    assert (
        runtime.unsupported(spec) == "Impact AI traces google-genai 1.32.0 to 2; this workload runs google-genai 3.0.0"
    )


@pytest.mark.parametrize(
    ("specifier", "spoken"),
    [
        ("<3,>=1.32.0", "1.32.0 to 2"),
        ("<4,>=1.26.0", "1.26.0 to 3"),
        ("<2,>=0.3.22", "0.3.22 to 1"),
        ("<5.0.0,>=1.4.1", "1.4.1 to 4"),
        ("~=0.92", "0.92 to 0.x"),
        ("~=1.4.2", "1.4.2 to 1.4.x"),
        ("<1.5,>=1.0", "1.0 to 1.4.x"),
        (">=2.0", "2.0 and later"),
        ("<0.5,>=0.3.3", "0.3.3 to 0.4"),
        ("<1,>=0.3.3", "0.3.3 to 0.x"),
        ("<2.5.1", "below 2.5.1"),
        ("<=2.5", "up to 2.5"),
        (">=1.0,!=1.4.0,<3", "1.0 to 2 except 1.4.0"),
        ("==1.2.3", "1.2.3"),
        ("", "any version"),
    ],
)
def test_a_range_is_said_in_words_lower_bound_first(specifier, spoken):
    assert runtime.spoken_specifier(specifier) == spoken


def test_several_libraries_outside_their_ranges_are_named_with_and_and_alternatives_with_or():
    both = runtime.Spec("x", "m", "C", ("x",), (("alpha", "<2,>=1.0", "0.5.0"), ("beta", ">=3", "2.0.0")), ())
    assert runtime.unsupported(both) == (
        "Impact AI traces alpha 1.0 to 1 and beta 3 and later; this workload runs alpha 0.5.0 and beta 2.0.0"
    )
    spec = plan({"langchain": "0.3.10", "langchain-core": "0.3.40"})["langchain"]
    assert runtime.unsupported(spec) == (
        "Impact AI traces langchain 0.3.22 to 1 or langchain-core 0.3.49 to 1; "
        "this workload runs langchain 0.3.10 and langchain-core 0.3.40"
    )


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


# --- applying an instrumentor ---------------------------------------------------------------------------------------

EMIT_EVENT = "OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"


class GoogleLikeInstrumentor:
    """Does at instrument() what Google's GenAI instrumentation 1.3b0 does: sets EMIT_EVENT=true in the process's
    environment, and takes the logger provider it is given or else the global one."""

    seen: list[dict] = []

    def instrument(self, **kwargs):
        import os

        from opentelemetry._logs import get_logger_provider

        os.environ[EMIT_EVENT] = "true"
        type(self).seen.append({**kwargs, "logs": kwargs.get("logger_provider") or get_logger_provider()})


class FrameworkInstrumentor:
    """Like FastAPI's: passes what it is given on to the app it instruments later, which fails on what it does not
    know."""

    seen: list[dict] = []

    def instrument(self, **kwargs):
        type(self).seen.append(kwargs)


class FailingInstrumentor:
    def instrument(self, **kwargs):
        import os

        os.environ[EMIT_EVENT] = "true"
        raise RuntimeError("broken")


@pytest.fixture
def applied(monkeypatch):
    """Applies an instrumentor of a throwaway module through the package's hook, as an import would."""
    import sys
    import types

    # Under the name of Google's GenAI instrumentation, which the unit tests' environment does not have.
    module = types.ModuleType("opentelemetry.instrumentation.google_genai")
    module.GoogleLikeInstrumentor = GoogleLikeInstrumentor
    module.FailingInstrumentor = FailingInstrumentor
    framework = types.ModuleType("fake_framework_instrumentation")
    framework.FrameworkInstrumentor = FrameworkInstrumentor
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, framework.__name__, framework)
    monkeypatch.delenv(EMIT_EVENT, raising=False)
    GoogleLikeInstrumentor.seen = []
    FrameworkInstrumentor.seen = []
    made = []

    def apply(cls: str = "GoogleLikeInstrumentor") -> runtime.Hooks:
        handler = runtime.Hooks({"IMPACT_OTLP_ENDPOINT": "http://127.0.0.1:9"}, lambda exc: None, lambda a, s: None)
        made.append(handler)
        where = framework.__name__ if cls == "FrameworkInstrumentor" else module.__name__
        spec = runtime.Spec("google-genai", where, cls, ("google.genai",), (), ())
        handler.apply(spec, None)
        return handler

    yield apply
    for handler in made:
        if handler.provider is not None:
            handler.provider.shutdown()


def test_an_instrumentor_cannot_turn_on_genai_log_events_for_the_process(applied):
    # Left set, every GenAI instrumentation of the process (OpenAI's and Anthropic's too) would send a log event per
    # call to the application's own logger provider, and its child processes would inherit the setting.
    import os

    assert applied().applied == ["google-genai"]
    assert EMIT_EVENT not in os.environ


def test_an_application_that_turned_genai_log_events_off_keeps_them_off(applied, monkeypatch):
    import os

    monkeypatch.setenv(EMIT_EVENT, "false")
    applied()
    assert os.environ[EMIT_EVENT] == "false"


def test_the_environment_is_put_back_when_an_instrumentor_fails(applied):
    import os

    assert applied("FailingInstrumentor").applied == []
    assert EMIT_EVENT not in os.environ


def test_an_instrumentor_logs_to_nothing_never_to_the_applications_global_logger_provider(applied):
    from opentelemetry._logs import NoOpLoggerProvider

    applied()
    (kwargs,) = GoogleLikeInstrumentor.seen
    assert isinstance(kwargs["logs"], NoOpLoggerProvider)
    assert kwargs["tracer_provider"] is not None


def test_a_web_framework_instrumentation_is_given_no_logger_provider(applied):
    # FastAPI's passes its keywords on to instrument_app when the application makes its app, which then fails on one it
    # does not know: the application would not start.
    applied("FrameworkInstrumentor")
    (kwargs,) = FrameworkInstrumentor.seen
    assert set(kwargs) == {"tracer_provider", "skip_dep_check"}


def test_the_genai_instrumentations_of_the_payload_are_the_ones_given_a_logger_provider():
    modules = {
        "opentelemetry.instrumentation.genai.openai": True,
        "opentelemetry.instrumentation.genai.anthropic": True,
        "opentelemetry.instrumentation.genai.openai_agents": True,
        "opentelemetry.instrumentation.genai.langchain": True,
        "opentelemetry.instrumentation.google_genai": True,
        "opentelemetry.instrumentation.bedrock": False,
        "opentelemetry.instrumentation.fastapi": False,
        "opentelemetry.instrumentation.django": False,
    }
    assert {m: m.startswith(runtime.GENAI_UTILITY) for m in modules} == modules
