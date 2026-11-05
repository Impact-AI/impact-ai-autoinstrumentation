# SPDX-License-Identifier: Apache-2.0

"""A gateway that hangs must not slow a covered process at exit, and must not make it grow.

Real interpreters on the fake payload of test_bootstrap, against a gateway that accepts connections and never answers
(what a NetworkPolicy that drops the packets, or a wedged gateway, looks like to the process), one that refuses and one
that is healthy. The measured time is the one between the application finishing its work and the process ending, which
is what a rollout, a Job or a CronJob waits for.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from test_bootstrap import Layout, assemble

from helpers import Sink

# An application that has just finished 1,200 spans of AI calls, with their content, while the gateway does not answer.
SPANS = 1200
EXIT_APP = """
import fakelib
tracer = fakelib.provider.get_tracer("chat")
for _ in range({spans}):
    with tracer.start_as_current_span("chat gpt-4o") as span:
        span.set_attribute("gen_ai.input.messages", "x" * 2000)
print("WORK_DONE", flush=True)
"""
# 100 kB of prompt and answer per span, as fast as the process can make them: 300 MB offered to a gateway that hangs.
MEMORY_SPANS = 3000
MEMORY_APP = """
import fakelib, json, os, resource, sys

def peak_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)

tracer = fakelib.provider.get_tracer("chat")
before = peak_mb()
for _ in range({spans}):
    with tracer.start_as_current_span("chat gpt-4o") as span:
        span.set_attribute("gen_ai.input.messages", os.urandom(40_000).hex())
        span.set_attribute("gen_ai.output.messages", os.urandom(10_000).hex())
print(json.dumps({{"growth_mb": peak_mb() - before}}), flush=True)
os._exit(0)  # the exit is measured by the tests above, here only the memory
"""
# The kit's fake instrumentor, with a handle on the tracer provider the package built for it.
INSTRUMENTOR = """
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

class FakeInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        import fakelib
        fakelib.provider = kwargs["tracer_provider"]

    def _uninstrument(self, **kwargs):
        pass
"""


class HangingGateway:
    """Accepts connections and never answers."""

    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        try:
            while True:
                self.held.append(self.listener.accept()[0])
        except OSError:
            pass  # closed

    def close(self) -> None:
        self.listener.close()
        for connection in self.held:
            connection.close()


@pytest.fixture
def layout(tmp_path):
    layout = Layout(tmp_path)
    (layout.root / "fake_instr" / "__init__.py").write_text(INSTRUMENTOR)
    (layout.root / "impact_instrumentation" / "_registry.py").write_text(assemble.generate_registry(layout.root))
    return layout


@pytest.fixture
def hanging():
    gateway = HangingGateway()
    yield gateway
    gateway.close()


@pytest.fixture
def healthy():
    sink = Sink()
    yield sink
    sink.close()


def env_for(layout: Layout, url: str) -> dict[str, str]:
    # The SDK's own schedule (5 s): the spans are still buffered when the application ends, as in a short job.
    return layout.env(None, IMPACT_OTLP_ENDPOINT=url, OTEL_BSP_SCHEDULE_DELAY="5000")


def seconds_to_exit(layout: Layout, url: str, spans: int = SPANS) -> float:
    """Seconds from the application's last line to the end of the process."""
    with tempfile.TemporaryFile("w+") as errors:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter, test-owned code
            [sys.executable, "-c", EXIT_APP.format(spans=spans)],
            env=env_for(layout, url),
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "WORK_DONE", errors.read()
            finished = time.monotonic()
            process.wait(timeout=90)
            took = time.monotonic() - finished
        finally:
            process.kill()
        errors.seek(0)
        assert process.returncode == 0, errors.read()
        return took


def test_a_hanging_gateway_delays_the_exit_by_at_most_five_seconds(layout, hanging):
    # Three batches of 512 spans, each tried three times for five seconds: this took 30 s, the SDK's own shutdown cap.
    took = seconds_to_exit(layout, hanging.url)
    assert took <= 5.0, f"the process needed {took:.1f} s to exit with {SPANS} spans buffered"


def test_a_refused_gateway_delays_the_exit_by_at_most_a_second(layout):
    took = seconds_to_exit(layout, "http://127.0.0.1:9")
    assert took <= 1.0, f"the process needed {took:.1f} s to exit with {SPANS} spans buffered"


def test_a_healthy_gateway_costs_the_exit_no_time_and_receives_every_span(layout, healthy):
    took = seconds_to_exit(layout, healthy.url)
    assert took <= 1.0, f"the process needed {took:.1f} s to exit with {SPANS} spans buffered"
    assert len([s for s in healthy.spans() if s["name"] == "chat gpt-4o"]) == SPANS


def test_a_process_that_does_nothing_waits_half_a_second_at_most_for_a_hanging_gateway(layout, hanging):
    # `python -c pass` is the first process of its container: it sends the impact.setup report and waits for it at exit.
    started = time.monotonic()
    result = layout.run("pass", layout.env(None, IMPACT_OTLP_ENDPOINT=hanging.url))
    took = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert took < 1.5, f"`python -c pass` needed {took:.1f} s against a gateway that hangs"


def test_a_gateway_that_hangs_does_not_make_the_process_grow(layout, hanging):
    result = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned code
        [sys.executable, "-c", MEMORY_APP.format(spans=MEMORY_SPANS)],
        env=env_for(layout, hanging.url),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    growth = json.loads(result.stdout.strip().splitlines()[-1])["growth_mb"]
    # The queue held 2,048 spans: 205 MB of content, and the batch being sent and encoded came on top (+380 MB). Now the
    # queue holds 16 MiB and a batch is at most 4 MiB.
    assert growth < 100, f"the process grew by {growth:.0f} MB while the gateway hung"
