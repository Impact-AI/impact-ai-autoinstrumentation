# SPDX-License-Identifier: Apache-2.0

"""The package's batch span processor: a queue bounded by bytes as well as by count, and an exit that does not wait for
a gateway that does not answer."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, StaticSampler

from helpers import Sink
from impact_instrumentation import processor as processors
from impact_instrumentation.processor import BoundedBatchSpanProcessor

SRC = Path(__file__).resolve().parents[2] / "src"


class Recorder(SpanExporter):
    """Notes every batch it is given. It can wait for the test (a gateway that hangs) or fail (one that refuses)."""

    def __init__(self, *, result: SpanExportResult = SpanExportResult.SUCCESS, hold: threading.Event | None = None):
        self.batches: list[list[str]] = []
        self.first = threading.Event()  # the first export has begun
        self.hold = hold
        self.result = result
        self.exiting = False
        self.stopped = False

    @property
    def names(self) -> list[str]:
        return [name for batch in list(self.batches) for name in batch]

    def export(self, spans):
        self.batches.append([s.name for s in spans])
        self.first.set()
        if self.hold is not None:
            self.hold.wait(30)
        return self.result

    def begin_shutdown(self) -> None:
        self.exiting = True

    def shutdown(self) -> None:
        self.stopped = True


def finish(provider: TracerProvider, names: list[str], size: int = 100) -> None:
    tracer = provider.get_tracer("test")
    for name in names:
        with tracer.start_as_current_span(name) as span:
            span.set_attribute("content", "x" * size)


def sized_span(size: int, name: str = "s000", content: str = "x"):
    """A finished span of the shape these tests use, with `size` characters of content."""
    memory = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("content", content * size)
    return memory.get_finished_spans()[0]


def one_span_bytes() -> int:
    """What the processor counts for each span of these tests, so that the caps below can be given in spans."""
    return processors.span_bytes(sized_span(100))


def numbered(numbers) -> list[str]:
    """Names of equal length, so that every span is counted the same."""
    return [f"s{i:03d}" for i in numbers]


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def build():
    """Processors made by a test are stopped, and the exports it held released, when the test ends."""
    made: list[tuple[BoundedBatchSpanProcessor, Recorder]] = []

    def make(recorder: Recorder, *, sampler=None, **settings) -> tuple[BoundedBatchSpanProcessor, TracerProvider]:
        # The schedule never comes (60 s) unless a test asks for it.
        settings.setdefault("schedule_delay_millis", 60_000)
        processor = BoundedBatchSpanProcessor(recorder, **settings)
        provider = TracerProvider(shutdown_on_exit=False, **({"sampler": sampler} if sampler else {}))
        provider.add_span_processor(processor)
        made.append((processor, recorder))
        return processor, provider

    yield make
    for processor, recorder in made:
        if recorder.hold is not None:
            recorder.hold.set()
        processor.shutdown()


# --- the byte bound ------------------------------------------------------------------------------------------------


def test_the_queue_never_holds_more_than_the_byte_cap_and_drops_the_oldest_spans_first(build):
    one = one_span_bytes()
    hold = threading.Event()
    recorder = Recorder(hold=hold)
    processor, provider = build(recorder, max_queue_bytes=20 * one, max_export_batch_size=5)
    finish(provider, numbered(range(5)))  # a full batch: the worker takes it, and the gateway never answers
    assert recorder.first.wait(10)

    for name in numbered(range(5, 65)):
        finish(provider, [name])
        assert processor.queued_bytes <= 20 * one
    assert processor.queued_spans == 20

    hold.set()
    assert wait_for(lambda: len(recorder.names) == 25)
    # The first batch was already on its way; of the 60 that queued up behind it only the newest 20 are left.
    assert recorder.names == numbered([*range(5), *range(45, 65)])


def test_the_count_bound_of_the_sdk_still_applies_and_also_drops_the_oldest(build):
    hold = threading.Event()
    recorder = Recorder(hold=hold)
    processor, provider = build(recorder, max_queue_size=10, max_export_batch_size=5)
    finish(provider, numbered(range(5)))
    assert recorder.first.wait(10)
    finish(provider, numbered(range(5, 40)))
    assert processor.queued_spans == 10

    hold.set()
    assert wait_for(lambda: len(recorder.names) == 15)
    assert recorder.names == numbered([*range(5), *range(30, 40)])


def test_a_span_that_alone_exceeds_the_cap_is_dropped_and_the_others_stay(build):
    one = one_span_bytes()
    recorder = Recorder()
    processor, provider = build(recorder, max_queue_bytes=3 * one, max_export_batch_size=100)
    finish(provider, numbered([0, 1]))
    finish(provider, ["huge"], size=4 * one)  # more than the whole cap
    finish(provider, numbered([2]))
    assert processor.queued_spans == 3
    assert processor.force_flush() is True
    assert recorder.names == numbered([0, 1, 2])


def test_a_full_batch_of_bytes_is_sent_without_waiting_for_the_schedule(build):
    # Spans of 100 kB fill the byte cap long before the count of a batch (512): they must not wait for the 5 s schedule
    # while the gateway is healthy, or a busy workload would lose most of them to the cap.
    one = one_span_bytes()
    recorder = Recorder()
    _, provider = build(recorder, max_export_batch_bytes=3 * one, max_queue_bytes=20 * one)
    finish(provider, numbered(range(3)))
    assert recorder.first.wait(10), "the worker did not wake for a batch that is full by bytes"
    assert wait_for(lambda: recorder.names == numbered(range(3)))
    assert len(recorder.batches) == 1


def test_a_batch_holds_at_most_the_batch_bytes_but_at_least_one_span(build):
    one = one_span_bytes()
    recorder = Recorder()
    processor, provider = build(recorder, max_export_batch_bytes=2 * one, max_queue_bytes=50 * one)
    finish(provider, numbered(range(5)))
    finish(provider, ["huge"], size=3 * one)  # bigger than a batch: it goes alone
    finish(provider, numbered([5]))
    processor.shutdown()
    assert recorder.names == [*numbered(range(5)), "huge", *numbered([5])]
    assert ["huge"] in recorder.batches
    assert all(len(batch) <= 2 for batch in recorder.batches)


def test_spans_are_sent_on_the_schedule(build):
    recorder = Recorder()
    _, provider = build(recorder, schedule_delay_millis=100)
    finish(provider, numbered([0, 1]))
    assert wait_for(lambda: recorder.names == numbered([0, 1]))


# --- the size of a span --------------------------------------------------------------------------------------------


def test_the_size_of_a_span_follows_its_content():
    assert 100_000 <= processors.span_bytes(sized_span(100_000)) <= 110_000
    assert processors.span_bytes(sized_span(10)) < 5_000


def test_non_ascii_text_costs_the_memory_it_takes():
    # CPython keeps a string at one, two or four bytes per character, so 50,000 emoji hold 200 kB.
    ascii_span = processors.span_bytes(sized_span(50_000))
    emoji_span = processors.span_bytes(sized_span(50_000, content="\U0001f600"))
    assert emoji_span > 3.5 * ascii_span


# --- the exit ------------------------------------------------------------------------------------------------------


def test_the_exit_sends_what_is_left_to_a_healthy_gateway(build):
    recorder = Recorder()
    processor, provider = build(recorder, max_export_batch_size=5)
    finish(provider, numbered(range(12)))
    processor.shutdown()
    assert recorder.names == numbered(range(12))
    assert recorder.exiting and recorder.stopped


def test_the_exporter_is_told_the_process_is_exiting_while_its_export_is_still_running(build):
    # The worker is inside an export that hangs when the exit begins: the exporter must know at once, so that it ends
    # that attempt without trying again.
    hold = threading.Event()
    recorder = Recorder(hold=hold)
    processor, provider = build(recorder, max_export_batch_size=5)
    finish(provider, numbered(range(5)))
    assert recorder.first.wait(10)
    exit_thread = threading.Thread(target=processor.shutdown)
    exit_thread.start()
    assert wait_for(lambda: recorder.exiting), "the exporter was not told about the exit while its export was running"
    hold.set()
    exit_thread.join(10)
    assert not exit_thread.is_alive()


def test_a_failed_export_at_exit_is_the_last_and_the_rest_is_dropped(build):
    hold = threading.Event()
    recorder = Recorder(result=SpanExportResult.FAILURE, hold=hold)
    processor, provider = build(recorder, max_export_batch_size=5)
    finish(provider, numbered(range(5)))
    assert recorder.first.wait(10)
    finish(provider, numbered(range(5, 30)))  # five more batches wait behind the one that hangs
    exit_thread = threading.Thread(target=processor.shutdown)
    exit_thread.start()
    assert wait_for(lambda: recorder.exiting)
    hold.set()
    exit_thread.join(10)
    assert not exit_thread.is_alive()
    assert len(recorder.batches) == 1, f"the exit went on to {len(recorder.batches) - 1} more batches after a failure"


def test_when_the_last_export_failed_the_exit_does_not_try_again(build):
    recorder = Recorder(result=SpanExportResult.FAILURE)
    processor, provider = build(recorder, max_export_batch_size=3)
    finish(provider, numbered(range(3)))
    assert wait_for(lambda: len(recorder.batches) == 1)
    finish(provider, numbered([3, 4]))  # buffered, not yet a batch
    processor.shutdown()
    assert recorder.names == numbered(range(3))
    assert recorder.stopped


def test_the_exit_waits_for_a_stuck_exporter_only_up_to_the_exit_limit(build, monkeypatch):
    monkeypatch.setattr(processors, "EXIT_FLUSH_SECONDS", 0.3)
    hold = threading.Event()
    recorder = Recorder(hold=hold)
    processor, provider = build(recorder, max_export_batch_size=5)
    finish(provider, numbered(range(5)))
    assert recorder.first.wait(10)
    finish(provider, numbered(range(5, 20)))
    begun = time.monotonic()
    processor.shutdown()
    assert time.monotonic() - begun < 3
    assert recorder.stopped
    hold.set()
    time.sleep(0.3)
    assert len(recorder.batches) == 1, "the worker went on exporting after the process had stopped waiting for it"


def test_spans_that_end_after_the_shutdown_are_ignored(build):
    recorder = Recorder()
    processor, provider = build(recorder)
    processor.shutdown()
    finish(provider, ["late"])
    assert processor.queued_spans == 0
    assert recorder.names == []


def test_shutdown_twice_is_harmless(build):
    recorder = Recorder()
    processor, provider = build(recorder)
    finish(provider, ["a"])
    processor.shutdown()
    processor.shutdown()
    assert recorder.names == ["a"]


# --- never into the application ------------------------------------------------------------------------------------


def test_a_span_the_processor_cannot_measure_never_reaches_the_application(build):
    processor, _ = build(Recorder())
    processor.on_end(object())  # the SDK calls this from span.end(), in the application's thread
    assert processor.queued_spans == 0


def test_a_span_that_is_not_sampled_is_not_queued(build):
    processor, provider = build(Recorder(), sampler=StaticSampler(Decision.RECORD_ONLY))
    finish(provider, ["recorded, not sampled"])
    assert processor.queued_spans == 0


def test_force_flush_sends_what_is_queued_and_says_so(build):
    recorder = Recorder()
    processor, provider = build(recorder)
    finish(provider, ["a", "b"])
    assert processor.force_flush() is True
    assert recorder.names == ["a", "b"]
    assert processor.queued_spans == 0


# --- the settings of the SDK ---------------------------------------------------------------------------------------


def test_the_settings_of_the_sdk_are_honoured():
    assert processors.settings({}) == (2048, 5.0, 512)
    environ = {
        "OTEL_BSP_MAX_QUEUE_SIZE": "100",
        "OTEL_BSP_SCHEDULE_DELAY": "250",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "10",
    }
    assert processors.settings(environ) == (100, 0.25, 10)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"OTEL_BSP_MAX_QUEUE_SIZE": "lots"}, (2048, 5.0, 512)),
        ({"OTEL_BSP_SCHEDULE_DELAY": "-1"}, (2048, 5.0, 512)),
        ({"OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "0"}, (2048, 5.0, 512)),
        # A batch cannot be larger than the queue. The SDK refuses to start then; the package makes the batch fit.
        ({"OTEL_BSP_MAX_QUEUE_SIZE": "100"}, (100, 5.0, 100)),
    ],
)
def test_settings_that_make_no_sense_never_stop_the_package(environ, expected):
    assert processors.settings(environ) == expected


# --- the package's own tracer provider -----------------------------------------------------------------------------


def test_the_tracer_provider_of_the_package_uses_this_processor():
    from impact_instrumentation import runtime
    from impact_instrumentation.exporter import ImpactSpanExporter

    hooks = runtime.Hooks({"IMPACT_OTLP_ENDPOINT": "http://127.0.0.1:9"}, lambda exc: None, lambda armed, skipped: None)
    provider = hooks._provider()
    try:
        used = provider._active_span_processor._span_processors
        (batching,) = [p for p in used if isinstance(p, BoundedBatchSpanProcessor)]
        assert isinstance(batching.exporter, ImpactSpanExporter)
    finally:
        provider.shutdown()


# --- a forked child ------------------------------------------------------------------------------------------------

FORK = """
import os, sys
sys.path.insert(0, {src!r})
from opentelemetry.sdk.trace import TracerProvider
from impact_instrumentation.exporter import ImpactSpanExporter
from impact_instrumentation.processor import BoundedBatchSpanProcessor

provider = TracerProvider(shutdown_on_exit=False)
provider.add_span_processor(BoundedBatchSpanProcessor(ImpactSpanExporter(environ={{"IMPACT_OTLP_ENDPOINT": {url!r}}})))
tracer = provider.get_tracer("t")
with tracer.start_as_current_span("before the fork"):
    pass
pid = os.fork()
if pid == 0:  # a gunicorn worker: the thread of the parent does not exist here, the processor starts its own
    with tracer.start_as_current_span("in the child"):
        pass
    provider.shutdown()
    os._exit(0)
_, status = os.waitpid(pid, 0)
provider.shutdown()
sys.exit(os.waitstatus_to_exitcode(status))
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_exports_with_a_worker_of_its_own():
    sink = Sink()
    try:
        result = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned code
            [sys.executable, "-c", FORK.format(src=str(SRC), url=sink.url)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        # Each span once: the child starts with an empty queue, the span queued before the fork is the parent's to send.
        assert sorted(s["name"] for s in sink.spans()) == ["before the fork", "in the child"]
    finally:
        sink.close()
