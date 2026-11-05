# SPDX-License-Identifier: Apache-2.0

"""The package's batch span processor: the SDK's, with a queue bounded by bytes and an exit that does not wait.

The SDK's BatchSpanProcessor bounds its queue by the number of spans (2,048). With the kit's defaults a span carries
the prompt and the answer, easily 100 kB, so a gateway that does not answer (a NetworkPolicy that drops the packets, a
wedged pod) let every covered process grow by hundreds of megabytes, until a pod with a 256 Mi limit was killed. And at
exit the SDK waits up to 30 s for the queue to drain, so the same gateway kept every covered process alive for 30 s
after it had finished its work.

This processor does what the SDK's does: the same settings (OTEL_BSP_*), batches in the order the spans ended, a daemon
worker thread, a new worker in a forked child, and no tracing of its own exports. It adds:

* a bound on the bytes the queue holds, MAX_QUEUE_BYTES. When it is passed the oldest spans are dropped, as the SDK
  drops the oldest when its count is passed. A span larger than the whole bound is dropped itself;
* a bound on the bytes of a batch, MAX_BATCH_BYTES, so that a request stays small and encoding it (about three times
  its size, for a moment) does not triple the memory of a full queue. A batch of spans also fills when its bytes do:
  large spans do not wait for the schedule, or a busy workload would lose most of them to the bound;
* an exit that sends what the gateway takes and stops at the first failure, within EXIT_FLUSH_SECONDS, see shutdown().

Imported only where the tracer provider is built, because it needs the OpenTelemetry SDK.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping

from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

log = logging.getLogger("impact_instrumentation")

MAX_QUEUE_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
# The longest an exit waits for the spans left in the queue. A gateway that does not answer ends the wait much sooner:
# an attempt takes 2 s and a failure ends the flush. This is for one that answers, slowly, batch after batch.
EXIT_FLUSH_SECONDS = 4.0

# The SDK's own settings and defaults (OTEL_BSP_*).
_QUEUE_SIZE = ("OTEL_BSP_MAX_QUEUE_SIZE", 2048)
_SCHEDULE_DELAY_MILLIS = ("OTEL_BSP_SCHEDULE_DELAY", 5000)
_BATCH_SIZE = ("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", 512)
_WARN_EVERY_SECONDS = 60.0
_DROPPED = "the span queue is full (%d MiB): dropped %d of the oldest spans, the gateway is too slow or unreachable"

# What a span costs in memory beyond its text: the objects of a span, of an attribute (its slot and a key, which is
# usually a constant the whole process shares), of an event and of a link. Measured with tracemalloc on spans of 10 and
# 25 attributes (4 and 6 kB); the text, which is what grows, is counted exactly.
_SPAN_OVERHEAD = 2048
_ATTRIBUTE_OVERHEAD = 96
_EVENT_OVERHEAD = 256
_NUMBER = 32


def settings(environ: Mapping[str, str]) -> tuple[int, float, int]:
    """(queue size, schedule delay in seconds, batch size) from the SDK's variables; its defaults for a value that makes
    no sense. A batch cannot be larger than the queue (the SDK refuses to start then; here the batch fits)."""

    def number(variable: tuple[str, int]) -> int:
        name, default = variable
        try:
            value = int(environ.get(name, default))
        except ValueError:
            return default
        return value if value > 0 else default

    queue = number(_QUEUE_SIZE)
    return queue, number(_SCHEDULE_DELAY_MILLIS) / 1000, min(number(_BATCH_SIZE), queue)


def _value_bytes(value) -> int:
    if isinstance(value, str | bytes | bytearray):
        return sys.getsizeof(value)  # exact, and O(1): CPython keeps text at 1, 2 or 4 bytes per character
    if isinstance(value, tuple | list):
        return sys.getsizeof(value) + sum(_value_bytes(item) for item in value)
    return _NUMBER


def _attributes_bytes(attributes) -> int:
    return sum(_ATTRIBUTE_OVERHEAD + _value_bytes(value) for value in (attributes or {}).values())


def span_bytes(span: ReadableSpan) -> int:
    """About what a finished span holds in memory: its text and its objects. Cheap enough for span.end()."""
    total = _SPAN_OVERHEAD + sys.getsizeof(span.name) + _attributes_bytes(span.attributes)
    for event in span.events:
        total += _EVENT_OVERHEAD + sys.getsizeof(event.name) + _attributes_bytes(event.attributes)
    for link in span.links:
        total += _EVENT_OVERHEAD + _attributes_bytes(link.attributes)
    return total


class BoundedBatchSpanProcessor(SpanProcessor):
    """Queues finished spans and hands them to the exporter in batches from a thread of its own."""

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_queue_bytes: int = MAX_QUEUE_BYTES,
        max_export_batch_bytes: int = MAX_BATCH_BYTES,
        max_queue_size: int | None = None,
        schedule_delay_millis: float | None = None,
        max_export_batch_size: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        queue, delay, batch = settings(os.environ if environ is None else environ)
        self._max_size = max_queue_size or queue
        self._delay = delay if schedule_delay_millis is None else schedule_delay_millis / 1000
        self._batch_size = min(max_export_batch_size or batch, self._max_size)
        self._max_bytes = max_queue_bytes
        self._batch_bytes = max_export_batch_bytes
        self._exporter = exporter
        self._queue: deque[tuple[ReadableSpan, int]] = deque()
        self._queued_bytes = 0
        self._lock = threading.Lock()  # the queue and its byte count; never held while exporting
        self._export_lock = threading.Lock()  # one export at a time: the worker's, or a force_flush
        self._wake = threading.Event()
        self._closed = False  # shutdown() was called
        self._abandoned = False  # ... and the process stopped waiting for the worker
        self._last_export_failed = False
        self._last_warning = float("-inf")
        self._start_worker()
        if hasattr(os, "register_at_fork"):
            reinit = weakref.WeakMethod(self._after_fork_in_child)

            def after_fork() -> None:
                method = reinit()
                if method is not None:
                    method()

            os.register_at_fork(after_in_child=after_fork)

    @property
    def exporter(self) -> SpanExporter:
        return self._exporter

    @property
    def queued_spans(self) -> int:
        return len(self._queue)

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    # --- the application's side: span.end() -------------------------------------------------------------------------

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if not (span.context and span.context.trace_flags.sampled):
                return
            self._add(span, span_bytes(span))
        except Exception:  # noqa: BLE001 - called by span.end() in the application's thread: nothing may reach it
            log.debug("could not queue a span", exc_info=True)

    def _add(self, span: ReadableSpan, size: int) -> None:
        dropped = 0
        with self._lock:
            if self._closed:
                return
            if size > self._max_bytes:
                dropped = 1  # it would push out every other span and not fit itself
            else:
                self._queue.append((span, size))
                self._queued_bytes += size
                while self._queued_bytes > self._max_bytes or len(self._queue) > self._max_size:
                    _, oldest = self._queue.popleft()
                    self._queued_bytes -= oldest
                    dropped += 1
            full = len(self._queue) >= self._batch_size or self._queued_bytes >= self._batch_bytes
        if dropped:
            self._warn_dropped(dropped)
        if full and not self._wake.is_set():
            self._wake.set()

    def _warn_dropped(self, count: int) -> None:
        now = time.monotonic()
        if now - self._last_warning >= _WARN_EVERY_SECONDS:
            self._last_warning = now
            log.warning(_DROPPED, self._max_bytes // (1024 * 1024), count)
        else:
            log.debug(_DROPPED, self._max_bytes // (1024 * 1024), count)

    # --- the worker's side ------------------------------------------------------------------------------------------

    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._work, name="impact-span-export", daemon=True)
        self._worker.start()

    def _work(self) -> None:
        while True:
            woken = self._wake.wait(self._delay)
            if self._closed:
                break
            self._wake.clear()
            self._export(first=not woken)  # on the schedule, whatever is there; when woken, only full batches
        self._export(everything=True)

    def _take(self, *, force: bool) -> list[ReadableSpan]:
        """The oldest spans, up to a batch by count and by bytes (and at least one). Empty if the batch is not full and
        not forced."""
        with self._lock:
            if not self._queue or self._abandoned:
                return []
            full = len(self._queue) >= self._batch_size or self._queued_bytes >= self._batch_bytes
            if not (force or full):
                return []
            batch: list[ReadableSpan] = []
            total = 0
            while self._queue and len(batch) < self._batch_size:
                span, size = self._queue[0]
                if batch and total + size > self._batch_bytes:
                    break
                self._queue.popleft()
                self._queued_bytes -= size
                batch.append(span)
                total += size
            return batch

    def _send(self, batch: list[ReadableSpan]) -> None:
        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))  # our own requests are not worth a span
        try:
            result = self._exporter.export(batch)
        except Exception:  # noqa: BLE001 - an exporter that raises must not end the worker
            log.debug("the exporter raised", exc_info=True)
            result = SpanExportResult.FAILURE
        finally:
            detach(token)
        self._last_export_failed = result is SpanExportResult.FAILURE

    def _export(self, *, first: bool = False, everything: bool = False, deadline: float | None = None) -> bool:
        """Send full batches (the first one whatever its size when `first`; every one when `everything`).

        Once the process is exiting, the gateway failing is the end of it: what is left is dropped, not tried. True when
        nothing is left to send, False when the deadline came first.
        """
        with self._export_lock:
            force = first or everything
            while True:
                if self._closed and self._last_export_failed:
                    with self._lock:
                        self._queue.clear()
                        self._queued_bytes = 0
                    return True
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                batch = self._take(force=force)
                if not batch:
                    return True
                self._send(batch)
                force = everything

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._closed:
            return False
        return self._export(everything=True, deadline=time.monotonic() + timeout_millis / 1000)

    def shutdown(self) -> None:
        """The process is exiting.

        The exporter is told at once, so that the batch it is sending ends with its attempt instead of trying again. The
        worker sends what is left and stops at the first batch that fails; if the last export before the exit had failed
        it sends nothing (the gateway is not answering, and asking again costs the exit its seconds). The wait for the
        worker is bounded by EXIT_FLUSH_SECONDS.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        begin = getattr(self._exporter, "begin_shutdown", None)
        if begin is not None:
            try:
                begin()
            except Exception:  # noqa: BLE001 - the exit goes on
                log.debug("the exporter could not be told about the exit", exc_info=True)
        self._wake.set()
        self._worker.join(EXIT_FLUSH_SECONDS)
        self._abandoned = True  # a worker still busy with a slow export starts no other
        self._exporter.shutdown()

    # --- a forked child ---------------------------------------------------------------------------------------------

    def _after_fork_in_child(self) -> None:
        """The thread of the parent does not exist in the child, and a lock or an event it held would stay held. The
        spans queued at the fork are the parent's to send."""
        self._lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._wake = threading.Event()
        self._queue.clear()
        self._queued_bytes = 0
        if not self._closed:
            self._start_worker()
