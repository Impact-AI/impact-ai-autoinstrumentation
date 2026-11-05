# SPDX-License-Identifier: Apache-2.0

"""AE-7 (image side): the app never depends on impact.ai. When the gateway is unreachable, refuses the key, is
overloaded or
never answers, the workload starts and serves as it does without injection, and says little about it. A process that
ends does not wait for a gateway that hangs or whose packets are dropped, does not grow while it waits, and does not
crash on a stderr that is closed."""

from __future__ import annotations

import json
import time

import pytest

from containers import http, wait_until
from nodejs_support import (
    ANSWER,
    app_image,
    ask,
    is_llm_span,
    readable,
    require_images,
    setup_reports,
    wait_ready,
)

NODE = "22"


def start_express(start_app, **kwargs):
    require_images(app_image(NODE))
    app = start_app(NODE, ["node", "express.cjs"], directory="/apps/express", **kwargs)
    wait_ready(app)
    return app


def noise(app) -> list[str]:
    """What the application's log says about us."""
    return [line for line in app.logs().splitlines() if "impact-instrumentation" in line]


def batch_spans(record: dict) -> list[dict]:
    """The spans of one request the mock recorded, whatever it answered."""
    payload = record.get("json") or {"resourceSpans": []}
    return [span for rs in payload["resourceSpans"] for ss in rs["scopeSpans"] for span in ss["spans"]]


def batch_attributes(record: dict) -> list[dict]:
    return [
        {item["key"]: list(item["value"].values())[0] for item in span.get("attributes", [])}
        for span in batch_spans(record)
    ]


def serve(app, calls: int = 5) -> float:
    started = time.monotonic()
    for _ in range(calls):
        assert ask(app, "openai") == (200, {"answer": ANSWER}), app.logs()
    return (time.monotonic() - started) / calls


@pytest.mark.nodejs
def test_a_gateway_nobody_listens_on_does_not_stop_the_workload(start_app):
    app = start_express(start_app, gateway="http://127.0.0.1:9")
    serve(app)
    time.sleep(4)  # the export is tried, and tried again, and given up on
    assert app.is_running()
    assert ask(app, "anthropic") == (200, {"answer": ANSWER})
    log = app.logs()
    assert not [line for line in log.splitlines() if line.lstrip().startswith("at ")], log  # no stack trace in the log
    assert 1 <= len(noise(app)) <= 2, noise(app)
    assert "could not export" in noise(app)[0] and "ECONNREFUSED" in noise(app)[0]


@pytest.mark.nodejs
def test_a_key_the_gateway_refuses_is_given_up_on_at_once_and_said_once(start_app, mock):
    mock.control(require_key="secret")  # the kit's gateway answers 401 to a key that is revoked
    app = start_express(start_app)
    serve(app, calls=8)
    time.sleep(3)
    assert app.is_running()
    assert len(noise(app)) == 1 and "HTTP 401" in noise(app)[0], noise(app)
    sent = [
        r
        for r in mock.requests("/v1/traces")
        if any(is_llm_span({"name": "x", "attrs": a}) for a in batch_attributes(r))
    ]
    assert sent and all(r["status"] == 401 for r in sent), "it tried, and every try was refused"
    # a refusal like this one is permanent: each batch was sent once, not three times
    ids = [span["spanId"] for r in sent for span in batch_spans(r)]
    assert len(ids) == len(set(ids)), "a batch that was refused was sent again"


@pytest.mark.nodejs
def test_a_gateway_that_is_busy_for_a_moment_loses_nothing(start_app, mock):
    app = start_express(start_app)
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    mock.control(rules=[{"path": "/v1/traces", "status": 503, "retry_after": 1, "times": 1}])
    ask(app, "openai")

    def tries_so_far():
        return [
            r
            for r in mock.requests("/v1/traces")
            if any(span["name"] == "OpenAI Chat Completions" for span in batch_spans(r))
        ]

    wait_until(
        lambda: [r for r in tries_so_far() if r["status"] == 200], timeout=45, message="the batch to be accepted"
    )
    tries = tries_so_far()
    assert [r["status"] for r in tries] == [
        503,
        200,
    ]  # refused once, with Retry-After, then accepted: nothing lost
    assert (
        len({span["spanId"] for r in tries for span in batch_spans(r) if span["name"] == "OpenAI Chat Completions"})
        == 1
    )
    assert noise(app) == []


@pytest.mark.nodejs
def test_a_gateway_that_accepts_and_never_answers_does_not_slow_the_workload(start_app, mock):
    baseline = start_express(start_app)
    plain = serve(baseline)
    mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 30}])
    app = start_express(start_app)
    slowed = serve(app)
    assert slowed < plain + 0.5, (slowed, plain)
    assert app.is_running()


@pytest.mark.nodejs
def test_a_workload_that_cannot_reach_the_gateway_at_start_is_not_delayed_by_it(start_app):
    started = time.monotonic()
    app = start_express(start_app, gateway="http://192.0.2.1:4318")  # an address nothing answers
    assert time.monotonic() - started < 20
    assert http("GET", f"{app.url}/healthz")[0] == 200
    wait_until(lambda: ask(app, "openai")[0] == 200, timeout=20, message="the workload to answer")


# --- the end of a process, and its memory, while the gateway hangs or its packets are dropped -------------------------

# A covered process of these tests: it asks itself for N paths that are not probes (a server span each) and ends in a
# mode:
#   natural   the loop drains, as a Job or a CronJob ends
#   graceful  SIGTERM to itself; the handler closes the server and the loop drains, as a service does at a rollout
JOB = r"""
const http = require('http');
const n = Number(process.argv[2]);
const mode = process.argv[3] || 'natural';
const server = http.createServer((request, response) => {
  response.setHeader('Connection', 'close');
  response.end('ok');
});
function ask(port) {
  return new Promise((resolve, reject) => {
    const options = { host: '127.0.0.1', port, path: '/work', agent: false, headers: { Connection: 'close' } };
    const request = http.request(options, (response) => {
      response.resume();
      response.on('end', resolve);
    });
    request.on('error', reject);
    request.end();
  });
}
server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  for (let i = 0; i < n; i += 1) await ask(port);
  process.stdout.write('WORK_DONE ' + Date.now() / 1000 + '\n');
  if (mode === 'graceful') {
    process.on('SIGTERM', () => server.close());
    process.kill(process.pid, 'SIGTERM');
    return;
  }
  server.close();
});
"""

# Spans of an AI call's size (100 kB of prompt and answer), made through the tracer provider the preload registered,
# which it does where Anthropic's SDK is installed (/apps/anthropic-1). It says how much its heap held beyond what it
# held at the start, after a full collection.
MEMORY_JOB = r"""
const crypto = require('crypto');
const provider = globalThis[Symbol.for('opentelemetry.js.api.1')].trace;
const tracer = provider.getTracer('chat');
const rate = Number(process.argv[2]);
const size = Number(process.argv[3]);
const seconds = Number(process.argv[4]);
const used = () => {
  global.gc();
  const memory = process.memoryUsage();
  return (memory.heapUsed + memory.external) / 1048576;
};
const before = used();
let peak = 0;
let made = 0;
const started = Date.now();
setInterval(() => {
  const due = Math.floor(((Date.now() - started) / 1000) * rate);
  for (; made < due; made += 1) {
    const span = tracer.startSpan('chat claude');
    span.setAttribute('gen_ai.input.messages', crypto.randomBytes(Math.floor(size * 0.4)).toString('hex'));
    span.setAttribute('gen_ai.output.messages', crypto.randomBytes(Math.floor(size * 0.1)).toString('hex'));
    span.end();
  }
  peak = Math.max(peak, used() - before);
  if (Date.now() - started >= seconds * 1000) {
    process.stdout.write('MEMORY ' + JSON.stringify({ growthMb: Math.round(peak * 10) / 10, spans: made }) + '\n');
    process.exit(0);
  }
}, 100);
"""

ADDRESSES = {"refused": "http://127.0.0.1:9", "blackhole": "http://192.0.2.1:4318"}  # nothing listens; nothing routes


def gateway_for(scenario, mock):
    """The address the covered process sends to, from inside its container."""
    if scenario == "hanging":
        mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 60}])  # accepts, and answers after a minute
        return None
    return ADDRESSES.get(scenario)


def script(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return readable(path)


def printed(app, *names: str) -> dict[str, str]:
    """The values the container printed after each name, once the last of them is there."""

    def found():
        values = {}
        for line in app.logs().splitlines():
            name, _, value = line.partition(" ")
            if name in names:
                values[name] = value
        return values if values.keys() == set(names) else None

    return wait_until(found, timeout=90, message=lambda: f"{names} in the output of the container\n{app.logs()}")


def seconds_after_work(app) -> float:
    done = printed(app, "WORK_DONE", "EXITED")
    return float(done["EXITED"]) - float(done["WORK_DONE"])


def start_job(start_app, mock, tmp_path, scenario, command, **options):
    require_images(app_image(NODE))
    gateway = gateway_for(scenario, mock)
    job = script(tmp_path, "job.js", JOB)
    return start_app(
        NODE,
        ["sh", "-c", f'{command}; echo "EXITED $(date +%s.%N)"'],
        directory="/apps/express",
        gateway=gateway,
        # the package's own schedule (5 s), not the tests' fast one: the spans of a short job are still buffered at exit
        env={"OTEL_BSP_SCHEDULE_DELAY": "5000"},
        mounts={str(job): "/work/job.js:ro"},
        publish=None,
        **options,
    )


@pytest.mark.nodejs
@pytest.mark.parametrize(
    ("scenario", "spans", "limit"),
    [
        ("hanging", 1, 5.0),
        ("hanging", 1200, 5.0),
        ("blackhole", 1, 5.0),
        ("blackhole", 1200, 5.0),
        ("refused", 1200, 1.0),
        ("healthy", 1200, 1.0),
    ],
)
def test_a_job_with_buffered_spans_ends_promptly_whatever_the_gateway_does(
    start_app, mock, tmp_path, scenario, spans, limit
):
    # It took 19 s for one span and 49 s for 1,200 against a gateway that hangs, 409 and 675 s when packets dropped.
    app = start_job(start_app, mock, tmp_path, scenario, f"node /work/job.js {spans}")
    took = seconds_after_work(app)
    assert took <= limit, f"the process needed {took:.1f} s to end with {spans} spans buffered ({scenario} gateway)"
    ours = ("WORK_DONE", "EXITED", "impact-instrumentation: ")
    strays = [line for line in app.logs().splitlines() if not line.startswith(ours)]
    assert not strays, f"the log holds more than the stamps and the package's own warning: {strays}"
    if scenario == "healthy":
        wait_until(
            lambda: len([s for s in mock.spans() if s.get("kind") == 2]) >= spans, timeout=10, message="every span"
        )
        assert len([s for s in mock.spans() if s.get("kind") == 2]) == spans  # nothing the job did is lost at exit


@pytest.mark.nodejs
@pytest.mark.parametrize("scenario", ["hanging", "blackhole"])
def test_a_graceful_shutdown_ends_promptly_whatever_the_gateway_does(start_app, mock, tmp_path, scenario):
    # A service that closes its server on SIGTERM lets the loop drain, and the flush at the end took 19 s and 409 s.
    app = start_job(start_app, mock, tmp_path, scenario, "node /work/job.js 100 graceful")
    took = seconds_after_work(app)
    assert took <= 5.0, f"the process needed {took:.1f} s to end after SIGTERM ({scenario} gateway)"


@pytest.mark.nodejs
@pytest.mark.parametrize("scenario", ["hanging", "blackhole"])
def test_a_process_that_does_nothing_waits_half_a_second_at_most_for_the_setup_report(start_app, mock, scenario):
    # The first Node process of a container sends the impact.setup report and waits for it at exit: 3.2 s and 5.1 s.
    require_images(app_image(NODE))
    app = start_app(
        NODE,
        ["sh", "-c", "date +%s.%N; node -e 0; date +%s.%N"],
        gateway=gateway_for(scenario, mock),
        publish=None,
    )

    def both():
        lines = [line for line in app.logs().splitlines() if line.replace(".", "", 1).isdigit()]
        return lines if len(lines) == 2 else None

    started, ended = (float(line) for line in wait_until(both, timeout=60, message=lambda: app.logs()))
    assert ended - started < 1.5, (
        f"`node -e 0` needed {ended - started:.1f} s against a gateway that does not answer ({scenario})"
    )


@pytest.mark.nodejs
@pytest.mark.parametrize("scenario", ["hanging", "blackhole"])
def test_a_gateway_that_does_not_answer_does_not_make_the_process_grow(start_app, mock, tmp_path, scenario):
    # 200 spans a second of 100 kB for 15 s, in a pod of 512 MiB: the queue of 2,048 spans alone was 205 MB, and the
    # process aborted with "JavaScript heap out of memory" after 11 to 21 s. Now the queue holds 16 MiB, a batch 4 MiB.
    require_images(app_image(NODE))
    memory = script(tmp_path, "memory.js", MEMORY_JOB)
    app = start_app(
        NODE,
        ["sh", "-c", 'node /work/memory.js 200 100000 15; echo "EXITED $?"'],
        directory="/apps/anthropic-1",
        gateway=gateway_for(scenario, mock),
        node_options="--expose-gc",
        mounts={str(memory): "/work/memory.js:ro"},
        extra_args=["--memory=512m"],
        publish=None,
    )
    result = printed(app, "EXITED")
    assert result["EXITED"] == "0", f"the process ended with {result['EXITED']}\n{app.logs()[-600:]}"
    growth = json.loads(printed(app, "MEMORY")["MEMORY"])["growthMb"]
    assert growth < 16 + 20, f"the process grew by {growth} MB while the gateway did not answer ({scenario})"


@pytest.mark.nodejs
def test_a_closed_stderr_does_not_crash_the_application(start_app, mock, tmp_path):
    # The kit warns once a minute that it could not export. A stderr whose reader is gone (here: a process substitution
    # that has ended) made that write an uncaught EPIPE, and the application exited with 1 (without the preload: 0).
    require_images(app_image(NODE))
    job = script(tmp_path, "job.js", JOB)
    app = start_app(
        NODE,
        ["bash", "-c", 'node /work/job.js 20 2> >(true); echo "EXITED $?"'],
        directory="/apps/express",
        gateway=ADDRESSES["refused"],
        mounts={str(job): "/work/job.js:ro"},
        publish=None,
    )
    result = printed(app, "WORK_DONE", "EXITED")
    assert result["EXITED"] == "0", f"the application exited with {result['EXITED']}"
