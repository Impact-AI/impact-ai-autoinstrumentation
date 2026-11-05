# SPDX-License-Identifier: Apache-2.0

"""Span exporter: OTLP/HTTP JSON to the gateway (or to ingest), standard library only for the transport.

Used with a private TracerProvider, so that the application's own OpenTelemetry state is never touched.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from impact_instrumentation import otlp

# One more try after a failed attempt: with the 2 s an attempt may take inside the kit, a batch for a gateway that does
# not answer costs at most 2 + 0.5 + 2 seconds.
RETRIES = 1
_PERMANENT = frozenset({400, 401, 403, 404, 405, 413, 415})
_WARN_EVERY_SECONDS = 60.0
_PAUSE_SLICE_SECONDS = 0.05
_SPAN_FLAGS_CONTEXT_HAS_IS_REMOTE_MASK = 0x100
_SPAN_FLAGS_CONTEXT_IS_REMOTE_MASK = 0x200

log = logging.getLogger("impact_instrumentation")


def _span_flags(context) -> int:
    flags = _SPAN_FLAGS_CONTEXT_HAS_IS_REMOTE_MASK
    if context is not None and context.is_remote:
        flags |= _SPAN_FLAGS_CONTEXT_IS_REMOTE_MASK
    return flags


def _hex_trace_id(value: int) -> str:
    return format(value, "032x")


def _hex_span_id(value: int) -> str:
    return format(value, "016x")


def _trace_state(state) -> str:
    return ",".join(f"{k}={v}" for k, v in state.items()) if state else ""


def _put(target: dict, key: str, value) -> None:
    """Set a field the way protobuf's JSON mapping does: default values are left out."""
    if value not in (None, "", 0, [], {}):
        target[key] = value


def _event(event) -> dict:
    out = {"timeUnixNano": str(event.timestamp), "name": event.name}
    _put(out, "attributes", otlp.key_values(event.attributes))
    _put(out, "droppedAttributesCount", event.dropped_attributes)
    return out


def _link(link) -> dict:
    ctx = link.context
    out = {"traceId": _hex_trace_id(ctx.trace_id), "spanId": _hex_span_id(ctx.span_id)}
    _put(out, "traceState", _trace_state(ctx.trace_state))
    _put(out, "attributes", otlp.key_values(link.attributes))
    _put(out, "droppedAttributesCount", link.dropped_attributes)
    out["flags"] = _span_flags(ctx)
    return out


def _span(span: ReadableSpan) -> dict:
    ctx = span.get_span_context()
    out: dict = {"traceId": _hex_trace_id(ctx.trace_id), "spanId": _hex_span_id(ctx.span_id)}
    _put(out, "traceState", _trace_state(ctx.trace_state))
    if span.parent is not None:
        out["parentSpanId"] = _hex_span_id(span.parent.span_id)
    out["name"] = span.name
    out["kind"] = span.kind.value + 1  # SDK INTERNAL=0..CONSUMER=4; OTLP INTERNAL=1..CONSUMER=5
    out["startTimeUnixNano"] = str(span.start_time or 0)
    out["endTimeUnixNano"] = str(span.end_time or 0)
    _put(out, "attributes", otlp.key_values(span.attributes))
    _put(out, "droppedAttributesCount", span.dropped_attributes)
    _put(out, "events", [_event(e) for e in span.events])
    _put(out, "droppedEventsCount", span.dropped_events)
    _put(out, "links", [_link(link) for link in span.links])
    _put(out, "droppedLinksCount", span.dropped_links)
    status: dict = {}
    _put(status, "message", span.status.description)
    _put(status, "code", span.status.status_code.value)
    out["status"] = status
    out["flags"] = _span_flags(span.parent)
    return out


def encode_spans(spans: Sequence[ReadableSpan]) -> dict:
    """An ExportTraceServiceRequest in OTLP JSON, grouped by resource and instrumentation scope."""
    grouped: dict = {}
    for span in spans:
        scopes = grouped.setdefault(span.resource, {})
        scopes.setdefault(span.instrumentation_scope or None, []).append(_span(span))
    resource_spans = []
    for resource, scopes in grouped.items():
        scope_spans = []
        for scope, encoded in scopes.items():
            entry: dict = {"scope": {}, "spans": encoded}
            if scope is not None:
                _put(entry["scope"], "name", scope.name)
                _put(entry["scope"], "version", scope.version)
                _put(entry["scope"], "attributes", otlp.key_values(scope.attributes))
                _put(entry, "schemaUrl", scope.schema_url)
            scope_spans.append(entry)
        item: dict = {
            "resource": {"attributes": otlp.key_values(resource.attributes)},
            "scopeSpans": scope_spans,
        }
        _put(item, "schemaUrl", resource.schema_url)
        resource_spans.append(item)
    return {"resourceSpans": resource_spans}


class ImpactSpanExporter(SpanExporter):
    """Exports finished spans; never raises, never blocks the application (the batch processor's thread calls it)."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] | None = None,
        post: Callable[..., otlp.Response] = otlp.post,
    ) -> None:
        env = os.environ if environ is None else environ
        self.url = otlp.traces_endpoint(env)
        self.headers = otlp.exporter_headers(env)
        self._timeout = otlp.timeout_seconds(env)
        self._ca_file = None if otlp.in_kit(env) else env.get("OTEL_EXPORTER_OTLP_CERTIFICATE")
        self._sleep = sleep or self._pause
        self._post = post
        self._stopped = False
        self._exiting = False
        self._last_warning = float("-inf")

    def begin_shutdown(self) -> None:
        """The process is exiting. What is being sent now is the last attempt: one that fails is not tried again."""
        self._exiting = True

    def _pause(self, seconds: float) -> None:
        """The wait before trying again. It ends at once when the process starts to exit; no lock, no event, so nothing
        of it can be held across a fork."""
        deadline = time.monotonic() + seconds
        while not self._exiting:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, _PAUSE_SLICE_SECONDS))

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._stopped:
            return SpanExportResult.FAILURE
        try:
            payload = encode_spans(spans)
        except Exception:  # noqa: BLE001 - a bad span must not take the application down
            self._warn("could not encode %d spans", len(spans), exc_info=True)
            return SpanExportResult.FAILURE
        reason = "no attempt"
        for attempt in range(1 + RETRIES):
            if attempt and self._exiting:
                break  # a process on its way out does not try again
            delay = 0.5 * 2**attempt
            try:
                response = self._post(
                    self.url, payload, headers=self.headers, timeout=self._timeout, ca_file=self._ca_file
                )
            except Exception as exc:  # noqa: BLE001 - connection refused, timeouts, TLS errors, bad URLs
                reason = f"{type(exc).__name__}: {exc}"
            else:
                if response.ok:
                    return SpanExportResult.SUCCESS
                reason = f"HTTP {response.status}"
                if response.status in _PERMANENT:
                    break
                if response.retry_after is not None:
                    delay = min(response.retry_after, 5.0)
            if attempt < RETRIES and not self._exiting:
                self._sleep(delay)
        self._warn("could not export %d spans to %s: %s", len(spans), self.url, reason)
        return SpanExportResult.FAILURE

    def _warn(self, message: str, *args, **kwargs) -> None:
        now = time.monotonic()
        if now - self._last_warning >= _WARN_EVERY_SECONDS:
            self._last_warning = now
            log.warning(message, *args, **kwargs)
        else:
            log.debug(message, *args, **kwargs)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        self._stopped = True
