# SPDX-License-Identifier: Apache-2.0

"""Who, which session and which use case: the business context the application puts in OpenTelemetry baggage.

The package cannot know the user, the session or the use case of a request; the application can, once it has
authenticated it. It sets them as baggage, one line, and the package copies four keys of that baggage onto every span
it makes: user.id, session.id, gen_ai.conversation.id and impact.use_case. Nothing else of the baggage is read, a value
is at most 256 bytes, and nothing here may ever break the application.
"""

from __future__ import annotations

import pytest
from opentelemetry import baggage, context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from impact_instrumentation import business_context

KEYS = ("user.id", "session.id", "gen_ai.conversation.id", "impact.use_case")


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "returns"}))
    provider.add_span_processor(business_context.BusinessContext())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def with_baggage(entries: dict) -> context.Context:
    ctx = context.get_current()
    for key, value in entries.items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    return ctx


def span_under(tracer, exporter, entries: dict, *, attributes=None):
    token = context.attach(with_baggage(entries))
    try:
        with tracer.start_as_current_span("chat gpt-4o-mini", attributes=attributes):
            pass
    finally:
        context.detach(token)
    (span,) = exporter.get_finished_spans()
    return span


def test_the_keys_are_the_ones_the_platform_reads():
    assert business_context.KEYS == KEYS
    assert business_context.MAX_VALUE_BYTES == 256


def test_the_four_keys_are_copied_from_baggage_onto_the_span(traced):
    tracer, exporter = traced
    values = {
        "user.id": "u-1842",
        "session.id": "s-77",
        "gen_ai.conversation.id": "conv-9",
        "impact.use_case": "returns assistant",
    }
    span = span_under(tracer, exporter, values)
    assert {key: span.attributes[key] for key in KEYS} == values


def test_every_span_of_the_request_gets_them(traced):
    tracer, exporter = traced
    token = context.attach(with_baggage({"user.id": "u-1", "impact.use_case": "refunds"}))
    try:
        with tracer.start_as_current_span("invoke_agent refunds"):
            with tracer.start_as_current_span("chat gpt-4o-mini"):
                pass
            with tracer.start_as_current_span("execute_tool lookup_order"):
                pass
    finally:
        context.detach(token)
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    assert all(span.attributes["user.id"] == "u-1" for span in spans)
    assert all(span.attributes["impact.use_case"] == "refunds" for span in spans)


def test_no_other_baggage_is_copied(traced):
    tracer, exporter = traced
    span = span_under(
        tracer,
        exporter,
        {
            "user.id": "u-1",
            "enduser.id": "e-1",  # deprecated in the semantic conventions: the application uses user.id
            "user.email": "someone@example.com",
            "tenant": "t-1",
            "authorization": "Bearer abc",
            "USER.ID": "upper",
        },
    )
    assert dict(span.attributes) == {"user.id": "u-1"}


def test_a_long_value_is_cut_to_256_bytes(traced):
    tracer, exporter = traced
    span = span_under(tracer, exporter, {"user.id": "a" * 1000})
    assert span.attributes["user.id"] == "a" * 256


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("é" * 200, "é" * 128),  # 2 bytes each: exactly 256
        ("€" * 100, "€" * 85),  # 3 bytes each: 255, the 86th would straddle the cut
        ("a" + "😀" * 70, "a" + "😀" * 63),  # 4 bytes each: 253
        ("ok", "ok"),
        ("€" * 85, "€" * 85),  # 255 bytes: kept whole
    ],
)
def test_a_value_is_cut_on_a_character_boundary(traced, value, expected):
    tracer, exporter = traced
    span = span_under(tracer, exporter, {"session.id": value})
    assert span.attributes["session.id"] == expected
    assert len(span.attributes["session.id"].encode("utf-8")) <= 256


@pytest.mark.parametrize("value", ["", "   ", 42, 1.5, True, None, b"bytes", ["a"], {"a": 1}, "\udc80"])
def test_empty_and_non_text_values_are_dropped(traced, value):
    tracer, exporter = traced
    span = span_under(tracer, exporter, {"user.id": value, "impact.use_case": "refunds"})
    assert "user.id" not in span.attributes
    assert span.attributes["impact.use_case"] == "refunds"  # one bad value costs only itself


def test_without_baggage_nothing_changes(traced):
    tracer, exporter = traced
    with tracer.start_as_current_span("chat gpt-4o-mini", attributes={"gen_ai.request.model": "gpt-4o-mini"}):
        pass
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes) == {"gen_ai.request.model": "gpt-4o-mini"}


def test_an_attribute_the_span_already_has_is_kept(traced):
    # An instrumentation that knows the conversation (the OpenAI Agents SDK's, for one) says so at start: it wins.
    tracer, exporter = traced
    span = span_under(
        tracer,
        exporter,
        {"gen_ai.conversation.id": "from-baggage", "user.id": "u-1"},
        attributes={"gen_ai.conversation.id": "from-the-instrumentation"},
    )
    assert span.attributes["gen_ai.conversation.id"] == "from-the-instrumentation"
    assert span.attributes["user.id"] == "u-1"


def test_the_context_the_span_is_started_in_is_read(traced):
    # A span started with an explicit context (a callback, a thread) gets the baggage of that context, not the current.
    tracer, exporter = traced
    tracer.start_span("chat gpt-4o-mini", context=with_baggage({"user.id": "u-explicit"})).end()
    (span,) = exporter.get_finished_spans()
    assert span.attributes["user.id"] == "u-explicit"


def test_a_failure_never_reaches_the_application(traced, monkeypatch):
    tracer, exporter = traced

    def broken(*args, **kwargs):
        raise RuntimeError("baggage is broken")

    monkeypatch.setattr(business_context, "get_all", broken)
    span = span_under(tracer, exporter, {"user.id": "u-1"})
    assert "user.id" not in span.attributes


def test_a_span_that_refuses_attributes_does_not_break_anything():
    class Refusing:
        attributes = {}

        def set_attribute(self, key, value):
            raise ValueError("no")

    business_context.BusinessContext().on_start(Refusing(), parent_context=with_baggage({"user.id": "u-1"}))


@pytest.fixture
def textmap():
    """The process's propagator, put back after the test (the private provider wraps it)."""
    from opentelemetry import propagate

    before = propagate.get_global_textmap()
    yield
    propagate.set_global_textmap(before)


def test_the_processor_is_part_of_the_private_provider(textmap):
    from impact_instrumentation import runtime

    hooks = runtime.Hooks({"OTEL_SERVICE_NAME": "returns"}, lambda exc: None, lambda armed, skipped: None)
    provider = hooks._provider()
    try:
        processors = provider._active_span_processor._span_processors
        assert any(isinstance(p, business_context.BusinessContext) for p in processors)
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        token = context.attach(with_baggage({"user.id": "u-1", "session.id": "s-1"}))
        try:
            provider.get_tracer("test").start_span("chat gpt-4o-mini").end()
        finally:
            context.detach(token)
        (span,) = exporter.get_finished_spans()
        assert span.attributes["user.id"] == "u-1"
        assert span.attributes["session.id"] == "s-1"
    finally:
        provider.shutdown()


# --- a caller's baggage --------------------------------------------------------------------------------------------

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
CALLER = {
    "traceparent": TRACEPARENT,
    "baggage": "user.id=someone-else,session.id=forged,gen_ai.conversation.id=forged,impact.use_case=forged,"
    "tenant.plan=gold,region=eu",
}


def default_propagator():
    """OpenTelemetry's default, as a process without OTEL_PROPAGATORS has it: W3C trace context and baggage."""
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])


def trace_id(ctx) -> int:
    from opentelemetry import trace

    return trace.get_current_span(ctx).get_span_context().trace_id


def test_a_callers_four_keys_are_taken_out_and_the_rest_of_its_baggage_is_kept():
    ctx = business_context.FromCallers(default_propagator()).extract(CALLER, context.Context())
    assert dict(baggage.get_all(ctx)) == {"tenant.plan": "gold", "region": "eu"}
    assert trace_id(ctx) == int("0af7651916cd43dd8448eb211c80319c", 16)  # the trace context is extracted as before


def test_the_four_keys_alone_leave_an_empty_baggage_and_no_header_changes_nothing():
    propagator = business_context.FromCallers(default_propagator())
    assert dict(baggage.get_all(propagator.extract({"baggage": "user.id=x"}, context.Context()))) == {}
    assert dict(baggage.get_all(propagator.extract({}, context.Context()))) == {}


def test_a_value_the_process_already_has_wins_over_the_callers():
    own = with_baggage({"user.id": "u-1842"})
    ctx = business_context.FromCallers(default_propagator()).extract(CALLER, own)
    assert baggage.get_baggage("user.id", ctx) == "u-1842"
    assert baggage.get_baggage("session.id", ctx) is None
    assert baggage.get_baggage("region", ctx) == "eu"


def test_without_a_context_the_current_one_is_the_processs(textmap):
    token = context.attach(with_baggage({"impact.use_case": "returns assistant"}))
    try:
        ctx = business_context.FromCallers(default_propagator()).extract(CALLER)
    finally:
        context.detach(token)
    assert baggage.get_baggage("impact.use_case", ctx) == "returns assistant"
    assert baggage.get_baggage("user.id", ctx) is None


def test_the_value_the_application_sets_after_the_request_arrived_reaches_the_span(traced):
    tracer, exporter = traced
    extracted = business_context.FromCallers(default_propagator()).extract(CALLER, context.Context())
    token = context.attach(baggage.set_baggage("user.id", "u-1842", extracted))
    try:
        with tracer.start_as_current_span("chat gpt-4o-mini"):
            pass
    finally:
        context.detach(token)
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes) == {"user.id": "u-1842"}  # nothing of the caller's


def test_what_the_process_sends_on_is_unchanged():
    inner = default_propagator()
    wrapped = business_context.FromCallers(inner)
    assert wrapped.fields == inner.fields
    ctx = with_baggage({"user.id": "u-1", "tenant.plan": "gold"})
    sent, unwrapped = {}, {}
    wrapped.inject(sent, ctx)
    inner.inject(unwrapped, ctx)
    assert sent == unwrapped
    assert wrapped._propagators is inner._propagators  # the composite's own list, for code that reads it


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_a_workload_that_trusts_its_callers_keeps_their_values(textmap, value):
    from opentelemetry import propagate

    propagate.set_global_textmap(default_propagator())
    assert business_context.guard_callers({business_context.TRUST: value}) is False
    assert not isinstance(propagate.get_global_textmap(), business_context.FromCallers)
    ctx = propagate.extract(CALLER, context.Context())
    assert baggage.get_baggage("user.id", ctx) == "someone-else"
    assert baggage.get_baggage("tenant.plan", ctx) == "gold"


@pytest.mark.parametrize(
    "environ", [{}, {business_context.TRUST: ""}, {business_context.TRUST: "false"}, {business_context.TRUST: "1"}]
)
def test_by_default_the_processs_propagator_is_wrapped_once(textmap, environ):
    # An absent annotation reaches the pod as an empty value: the default.
    from opentelemetry import propagate

    inner = default_propagator()
    propagate.set_global_textmap(inner)
    assert business_context.guard_callers(environ) is True
    assert business_context.guard_callers(environ) is False  # never twice
    current = propagate.get_global_textmap()
    assert isinstance(current, business_context.FromCallers)
    assert current.propagator is inner  # the same propagators run: OTEL_PROPAGATORS is the process's choice
    ctx = propagate.extract(CALLER, context.Context())
    assert baggage.get_baggage("user.id", ctx) is None
    assert baggage.get_baggage("tenant.plan", ctx) == "gold"


def test_the_private_provider_guards_the_processs_propagator(textmap):
    from opentelemetry import propagate

    from impact_instrumentation import runtime

    propagate.set_global_textmap(default_propagator())
    provider = runtime.Hooks({}, lambda exc: None, lambda armed, skipped: None)._provider()
    try:
        assert isinstance(propagate.get_global_textmap(), business_context.FromCallers)
    finally:
        provider.shutdown()

    propagate.set_global_textmap(default_propagator())
    trusting = runtime.Hooks({business_context.TRUST: "true"}, lambda exc: None, lambda armed, skipped: None)
    provider = trusting._provider()
    try:
        assert not isinstance(propagate.get_global_textmap(), business_context.FromCallers)
    finally:
        provider.shutdown()


def test_a_filter_that_fails_returns_what_the_propagator_extracted(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("baggage is broken")

    monkeypatch.setattr(business_context, "get_baggage", broken)
    ctx = business_context.FromCallers(default_propagator()).extract(CALLER, context.Context())
    assert trace_id(ctx) == int("0af7651916cd43dd8448eb211c80319c", 16)


def test_a_guard_that_fails_costs_the_provider_nothing(textmap, monkeypatch):
    from impact_instrumentation import runtime

    def broken(environ):
        raise RuntimeError("no propagate module")

    monkeypatch.setattr(business_context, "guard_callers", broken)
    provider = runtime.Hooks({}, lambda exc: None, lambda armed, skipped: None)._provider()
    try:
        assert any(
            isinstance(p, business_context.BusinessContext) for p in provider._active_span_processor._span_processors
        )
    finally:
        provider.shutdown()


def test_the_package_never_chooses_the_propagators():
    # Which propagators run is the process's choice (OTEL_PROPAGATORS); the package wraps the one it finds and only
    # FromCallers.extract changes anything.
    import inspect

    from impact_instrumentation import runtime

    for module in (business_context, runtime):
        assert '"OTEL_PROPAGATORS"' not in inspect.getsource(module)
