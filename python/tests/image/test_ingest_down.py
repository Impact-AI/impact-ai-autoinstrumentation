# SPDX-License-Identifier: Apache-2.0

"""Fail open (image side): the workload never depends on impact.ai. When the gateway is refused, hangs, answers 503 or
rejects the key, the application starts and serves normally and no exception or noise reaches it. A process that
ends does not wait for a gateway that hangs either (at most five seconds, with 1,200 spans buffered)."""

from __future__ import annotations

import time

import pytest
from test_injected_apps import PROVIDERS, SERVERS, llm_spans, wait_ready

from conftest import app_image, require_images
from containers import http, wait_until

SCENARIOS = ["refused", "hanging", "unavailable", "revoked"]

# A job that makes 1,200 model calls and ends. The package buffers their spans and flushes them when the process exits.
# The container prints the epoch when the work is done, and again when the process has ended.
JOB = """
import os, threading, time
from openai import OpenAI

client = OpenAI(api_key="sk-test", base_url=os.environ["STUB_URL"] + "/v1", max_retries=0)

def calls():
    for _ in range(150):
        client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi there"}])

threads = [threading.Thread(target=calls) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print("WORK_DONE", time.time(), flush=True)
"""
RUN_JOB = 'python -c "$JOB"; echo "EXITED $(date +%s.%N)"'
CALLS = 8 * 150


@pytest.mark.image
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_application_serves_normally_while_ingest_is_down(start_app, mock, scenario):
    require_images(app_image("3.12"))
    gateway = None
    if scenario == "refused":
        gateway = "http://127.0.0.1:9"  # inside the app's own container: nothing listens
    elif scenario == "hanging":
        mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 60}])
    elif scenario == "unavailable":
        mock.control(rules=[{"path": "/v1/traces", "status": 503, "retry_after": 1}])
    elif scenario == "revoked":
        mock.control(require_key="the-key-that-was-revoked-is-not-this-one")

    started = time.monotonic()
    app = start_app(app_image("3.12"), SERVERS["uvicorn"][0], gateway=gateway)
    wait_ready(app)
    assert time.monotonic() - started < 60  # it starts

    def requests_made() -> list[float]:
        took = []
        for _ in range(5):
            for provider in PROVIDERS:
                t0 = time.monotonic()
                status, body = http("POST", f"{app.url}/chat/{provider}", {"prompt": "hi there"})
                took.append(time.monotonic() - t0)
                assert status == 200, (scenario, provider, body)
                assert body == {"answer": "Hello from the stub."}
        return took

    took = requests_made()
    if max(took) >= 3.0:
        # One stalled request on a busy machine (a lost DNS packet costs 5 s) is not the application waiting for
        # ingest: waiting would be there again.
        took = requests_made()
    rounded = [round(t, 2) for t in took]
    assert max(took) < 3.0, f"a request took {max(took):.1f}s while ingest was {scenario}: {rounded}"

    # Give the exporter time to try (and fail) a few times; the application must be unaffected.
    time.sleep(3)
    assert http("GET", f"{app.url}/healthz")[0] == 200
    assert app.is_running()
    logs = app.logs()
    assert "Traceback" not in logs, logs
    wait_until(lambda: app.is_running(), timeout=2)


def gateway_for(scenario, mock):
    """The address the covered process sends to, from inside its container."""
    if scenario == "hanging":
        mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 60}])  # accepts, and answers after a minute
    return "http://127.0.0.1:9" if scenario == "refused" else None


def stamps(app, *names: str) -> dict[str, float]:
    """The epochs the container printed, once the last of them is there."""

    def printed():
        found = {}
        for line in app.logs().splitlines():
            name, _, value = line.partition(" ")
            if name in names:
                found[name] = float(value)
        return found if found.keys() == set(names) else None

    return wait_until(printed, timeout=90, message=lambda: f"{names} in the output of the container\n{app.logs()}")


@pytest.mark.image
@pytest.mark.parametrize(("scenario", "limit"), [("hanging", 5.0), ("refused", 1.0), ("healthy", 1.0)])
def test_a_job_with_buffered_spans_ends_promptly_whatever_the_gateway_does(start_app, mock, scenario, limit):
    require_images(app_image("3.12"))
    gateway = gateway_for(scenario, mock)
    # The package's own schedule (5 s), not the tests' fast one: the spans of a short job are still buffered at exit.
    env = {"JOB": JOB, "OTEL_BSP_SCHEDULE_DELAY": "5000"}
    app = start_app(app_image("3.12"), ["sh", "-c", RUN_JOB], env=env, gateway=gateway)

    done = stamps(app, "WORK_DONE", "EXITED")
    took = done["EXITED"] - done["WORK_DONE"]
    assert took <= limit, f"the process needed {took:.1f} s to end with {CALLS} spans buffered ({scenario} gateway)"
    assert "Traceback" not in app.logs(), app.logs()
    if scenario == "healthy":
        # Nothing the job did is lost at exit.
        assert len([s for s in llm_spans(mock) if s["name"].startswith("chat")]) == CALLS


@pytest.mark.image
def test_a_process_that_does_nothing_waits_half_a_second_at_most_for_a_gateway_that_hangs(start_app, mock):
    # The first Python process of a container sends the impact.setup report and waits for it at exit.
    require_images(app_image("3.12"))
    mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 60}])
    app = start_app(app_image("3.12"), ["sh", "-c", "date +%s.%N; python -c pass; date +%s.%N"])

    def both():
        lines = [line for line in app.logs().splitlines() if line.replace(".", "", 1).isdigit()]
        return lines if len(lines) == 2 else None

    started, ended = (float(line) for line in wait_until(both, timeout=60, message=lambda: app.logs()))
    assert ended - started < 1.5, f"`python -c pass` needed {ended - started:.1f} s against a gateway that hangs"
