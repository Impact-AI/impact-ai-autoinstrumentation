# SPDX-License-Identifier: Apache-2.0

"""Copies the business context the application put in OpenTelemetry baggage onto every span the package makes.

Who made a request, in which session and for which use case is known to the application, once it has authenticated the
request, and to nothing outside it. The application says so with one line of OpenTelemetry's API (baggage on the
context of the request) and every span the package makes under that context, the model calls among them, carries it:

* `user.id`, `session.id`, `gen_ai.conversation.id` and `impact.use_case`, the names of the OpenTelemetry semantic
  conventions (impact.ai's own for the use case) that impact.ai reads. No other key of the baggage is read, ever:
  baggage also arrives from callers, through propagation, and holds whatever they put in it;
* text only, at most MAX_VALUE_BYTES bytes of UTF-8, cut on a character boundary. An empty value and one that is not
  text are dropped;
* an attribute the span already has when it starts (the instrumentation's own) is kept.

The baggage is read from the context the span starts in, so it works whichever provider made the span.

Baggage also arrives from callers, in the `baggage` header of a request, and a caller can put any user in it. So by
default the four keys are taken out of what a caller sends (FromCallers): the process keeps the propagators it has
(OTEL_PROPAGATORS, OpenTelemetry's defaults otherwise), every other key of a caller's baggage and the trace context
pass as before, and only values the application sets itself in the process reach the spans. A workload behind a
gateway that sets them, and that trusts it, says so with IMPACT_TRUST_CALLER_BAGGAGE=true (the chart sets it from the
pod's annotation impact.ai/trust-caller-baggage), and then a caller's values pass too. A value the application sets
in the process always wins.

Imported only where the tracer provider is built, because it needs the OpenTelemetry SDK.
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry.baggage import get_all, get_baggage, remove_baggage, set_baggage
from opentelemetry.context import get_current
from opentelemetry.propagators.textmap import TextMapPropagator, default_getter, default_setter
from opentelemetry.sdk.trace import SpanProcessor

KEYS = ("user.id", "session.id", "gen_ai.conversation.id", "impact.use_case")
MAX_VALUE_BYTES = 256
# "true" (any case) keeps a caller's values of the four keys. Anything else, an empty value among them (what Kubernetes
# gives when the pod has no such annotation), takes them out.
TRUST = "IMPACT_TRUST_CALLER_BAGGAGE"


def trusts_callers(environ: Mapping[str, str]) -> bool:
    return (environ.get(TRUST) or "").strip().lower() == "true"


def bounded(value) -> str | None:
    """The value as the span carries it: text of at most MAX_VALUE_BYTES bytes, or None to leave it off."""
    if not isinstance(value, str) or not value.strip():
        return None
    # A character is at least one byte, so the cut lies within the first MAX_VALUE_BYTES of them: a large value is never
    # encoded whole. A lone surrogate cannot be sent: encode() raises, and the caller drops the value.
    encoded = value[:MAX_VALUE_BYTES].encode("utf-8")
    if len(value) <= MAX_VALUE_BYTES and len(encoded) <= MAX_VALUE_BYTES:
        return value
    return encoded[:MAX_VALUE_BYTES].decode("utf-8", "ignore")  # "ignore" drops only the character the cut split


class BusinessContext(SpanProcessor):
    def on_start(self, span, parent_context=None) -> None:
        try:
            entries = get_all(parent_context)
            if not entries:
                return
            present = span.attributes or {}
            for key in KEYS:
                if key in present or key not in entries:
                    continue
                try:
                    value = bounded(entries[key])
                except Exception:  # noqa: BLE001, S112 - one value that cannot be sent costs only itself
                    continue
                if value is not None:
                    span.set_attribute(key, value)
        except Exception:  # noqa: BLE001, S110 - called by the application's span start: nothing may reach it
            pass


def without_callers_keys(extracted, before):
    """The context extract() returned, with the four keys as they were before it: a caller's values are taken out and
    a value the process already had is kept. Every other key, and the trace context, is left as extracted."""
    for key in KEYS:
        own = get_baggage(key, before)
        if get_baggage(key, extracted) == own:
            continue
        extracted = remove_baggage(key, extracted) if own is None else set_baggage(key, own, extracted)
    return extracted


class FromCallers(TextMapPropagator):
    """The process's own propagator, whichever it is, with the four keys taken out of what extract() reads from a
    caller. inject() and fields are the propagator's own: what the process sends on is unchanged."""

    def __init__(self, propagator: TextMapPropagator) -> None:
        self.propagator = propagator

    def extract(self, carrier, context=None, getter=default_getter):
        extracted = self.propagator.extract(carrier, context, getter=getter)  # its failures are its own, as before
        try:
            return without_callers_keys(extracted, get_current() if context is None else context)
        except Exception:  # noqa: BLE001 - called by the application's request handling: nothing may reach it
            return extracted

    def inject(self, carrier, context=None, setter=default_setter) -> None:
        self.propagator.inject(carrier, context, setter=setter)

    @property
    def fields(self):
        return self.propagator.fields

    def __getattr__(self, name):
        # Anything else asked of the process's propagator (a composite's list, say) is answered by it.
        if name == "propagator":
            raise AttributeError(name)
        return getattr(self.propagator, name)


def guard_callers(environ: Mapping[str, str]) -> bool:
    """Wrap the process's propagator in FromCallers, unless the workload trusts its callers; True when it did.

    Called before the first instrumentor is applied, so the requests the package's instrumentations extract go through
    it. A propagator the application sets later (set_global_textmap) is its own and is not wrapped: an application
    that configures OpenTelemetry itself usually has its own tracer, and then the package stands down anyway."""
    if trusts_callers(environ):
        return False
    from opentelemetry import propagate

    current = propagate.get_global_textmap()
    if isinstance(current, FromCallers):
        return False
    propagate.set_global_textmap(FromCallers(current))
    return True
