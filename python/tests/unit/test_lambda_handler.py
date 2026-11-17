# SPDX-License-Identifier: Apache-2.0

"""The package on AWS Lambda, in the process: the handler's wrap, the bounded flush at the end of an invocation, the
function's resource attributes and the stand-down beside another tracer's Lambda wrapper."""

from __future__ import annotations

import gzip
import json
import threading
import time
from types import SimpleNamespace

import pytest

from helpers import Sink
from impact_instrumentation import aws_lambda, otlp

ARN = "arn:aws:lambda:eu-west-1:123456789012:function:refund-bot"
LAMBDA_ENV = {
    "AWS_LAMBDA_FUNCTION_NAME": "refund-bot",
    "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
    "AWS_REGION": "eu-west-1",
    "LAMBDA_TASK_ROOT": "/var/task",
}


@pytest.fixture(autouse=True)
def forget_late_attributes():
    otlp._LATE.clear()
    yield
    otlp._LATE.clear()


def context(remaining_ms: float = 3000.0, arn: str = ARN + ":live"):
    return SimpleNamespace(invoked_function_arn=arn, get_remaining_time_in_millis=lambda: remaining_ms)


class SlowFlusher:
    """Stands in for the thread that sends: takes `seconds` to send, counts the sends."""

    def __init__(self, seconds: float):
        self.calls = 0
        self.flusher = aws_lambda.Flusher(aws_lambda.Outbox(), flush_spans=self._send)
        self.seconds = seconds

    def _send(self, _timeout_millis):
        self.calls += 1
        time.sleep(self.seconds)


# --- settings and names ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("app.handler", ("app", "handler")),
        ("src/refunds/app.lambda_handler", ("src.refunds.app", "lambda_handler")),
        ("pkg.mod.handler", ("pkg.mod", "handler")),
        ("handler", None),
        ("", None),
        ("app.", None),
    ],
)
def test_the_handler_setting_is_read_as_lambdas_runtime_reads_it(setting, expected):
    assert aws_lambda.split_handler(setting) == expected


@pytest.mark.parametrize(
    ("invoked", "expected"),
    [
        (ARN, ("123456789012", ARN)),
        (ARN + ":live", ("123456789012", ARN)),
        (ARN + ":7", ("123456789012", ARN)),
        ("arn:aws:lambda:eu-west-1:12345:function:refund-bot", None),
        ("arn:aws:s3:::bucket", None),
        ("", None),
    ],
)
def test_the_function_arn_drops_the_alias_or_version_it_was_invoked_with(invoked, expected):
    assert aws_lambda.function_arn(invoked) == expected


def test_the_runtime_puts_the_function_and_the_layers_first_unless_the_function_sets_pythonpath():
    assert aws_lambda.runtime_paths(LAMBDA_ENV, pythonpath_set=False, version=(3, 12)) == [
        "/var/task",
        "/opt/python/lib/python3.12/site-packages",
        "/opt/python",
    ]
    assert aws_lambda.runtime_paths(LAMBDA_ENV, pythonpath_set=True, version=(3, 12)) == ["/var/task"]


@pytest.mark.parametrize(
    ("environ", "detail"),
    [
        ({"_HANDLER": "datadog_lambda.handler.handler", "DD_LAMBDA_HANDLER": "app.handler"}, "datadog-lambda"),
        ({"_HANDLER": "newrelic_lambda_wrapper.handler"}, "newrelic-lambda"),
        ({"_HANDLER": "app.handler", "ORIG_HANDLER": "app.handler"}, "ORIG_HANDLER (set by the workload)"),
        ({"_HANDLER": "app.handler", "DD_LAMBDA_HANDLER": "app.handler"}, "DD_LAMBDA_HANDLER (set by the workload)"),
    ],
)
def test_another_tracers_lambda_wrapper_is_found_by_name(environ, detail):
    assert aws_lambda.detect.describe(aws_lambda.foreign(environ)) == detail


def test_a_function_with_its_own_handler_has_no_other_wrapper():
    assert aws_lambda.foreign({**LAMBDA_ENV, "_HANDLER": "app.handler"}) == []


def test_on_lambda_needs_the_function_and_its_code():
    assert aws_lambda.on_lambda(LAMBDA_ENV)
    assert not aws_lambda.on_lambda({"AWS_LAMBDA_FUNCTION_NAME": "refund-bot"})
    assert not aws_lambda.on_lambda({})


# --- the flush's bound -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "remaining", "seconds"),
    [
        ({}, None, 0.2),
        ({}, 3000, 0.2),
        ({"IMPACT_LAMBDA_FLUSH_MS": "50"}, 3000, 0.05),
        ({"IMPACT_LAMBDA_FLUSH_MS": "not a number"}, 3000, 0.2),
        ({"IMPACT_LAMBDA_FLUSH_MS": "-5"}, 3000, 0.0),
        ({}, 120, 0.07),  # never into the last 50 ms of the invocation
        ({}, 30, 0.0),
    ],
)
def test_the_wait_at_the_end_of_an_invocation_is_bounded(environ, remaining, seconds):
    assert aws_lambda.flush_budget(environ, remaining) == pytest.approx(seconds)


def test_an_invocation_waits_for_a_slow_send_no_longer_than_its_bound():
    slow = SlowFlusher(1.0)
    begin = time.monotonic()
    assert slow.flusher.flush(0.1) is False
    assert time.monotonic() - begin < 0.5
    # The send goes on in its thread; the next invocation does not start a second one while it is busy.
    assert slow.flusher.flush(0.05) is False
    time.sleep(1.2)
    assert slow.calls == 1 or slow.calls == 2
    assert slow.flusher.flush(2.0) is True


def test_a_fast_send_ends_the_wait_at_once():
    quick = SlowFlusher(0.0)
    begin = time.monotonic()
    assert quick.flusher.flush(1.0) is True
    assert time.monotonic() - begin < 0.5
    assert quick.calls == 1


def test_a_send_that_raises_does_not_stop_the_sender():
    calls = []

    def broken(_timeout):
        calls.append(1)
        raise RuntimeError("exporter broke")

    flusher = aws_lambda.Flusher(aws_lambda.Outbox(), flush_spans=broken)
    assert flusher.flush(1.0) is True
    assert flusher.flush(1.0) is True
    assert len(calls) == 2


# --- the handler -----------------------------------------------------------------------------------------------------


def test_the_wrapped_handler_returns_what_the_handler_returns_and_keeps_its_name():
    def lambda_handler(event, ctx):
        """Refunds an order."""
        return {"statusCode": 200, "body": event["order"]}

    function = aws_lambda.Function(LAMBDA_ENV, SlowFlusher(0.0).flusher)
    wrapped = function.wrap(lambda_handler)
    assert wrapped({"order": "A-1"}, context()) == {"statusCode": 200, "body": "A-1"}
    assert wrapped.__name__ == "lambda_handler"
    assert wrapped.__doc__ == "Refunds an order."
    assert wrapped.__wrapped__ is lambda_handler
    assert function.wrap(wrapped) is wrapped  # one wrapper, however often the module is loaded


def test_the_handlers_own_error_reaches_the_runtime_unchanged_after_the_flush():
    error = ValueError("order not found")
    slow = SlowFlusher(0.0)

    def lambda_handler(event, ctx):
        raise error

    wrapped = aws_lambda.Function(LAMBDA_ENV, slow.flusher).wrap(lambda_handler)
    with pytest.raises(ValueError) as raised:
        wrapped({}, context())
    assert raised.value is error
    assert slow.calls == 1


def test_an_ingest_that_does_not_answer_costs_an_invocation_at_most_its_bound():
    slow = SlowFlusher(5.0)
    function = aws_lambda.Function({**LAMBDA_ENV, "IMPACT_LAMBDA_FLUSH_MS": "100"}, slow.flusher)
    wrapped = function.wrap(lambda event, ctx: "ok")
    for _ in range(3):
        begin = time.monotonic()
        assert wrapped({}, context()) == "ok"
        assert time.monotonic() - begin < 0.4
    assert function.last_flush_seconds < 0.4


class HangingIngest:
    """Stands in for the thread's send to an ingest that accepts and never answers, until `answer` is called."""

    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.result = True
        self.flusher = aws_lambda.Flusher(aws_lambda.Outbox(), flush_spans=self._send)

    def _send(self, _timeout_millis):
        self.calls += 1
        self.release.wait(10)
        return self.result

    def answer(self, *, ok: bool) -> None:
        self.result = ok
        self.release.set()


# What an invocation may add while ingest hangs: well under the proposed per-invocation budget (+25 ms at p95).
NO_WAIT_SECONDS = 0.02


def test_after_a_wait_runs_out_invocations_stop_waiting_until_a_send_succeeds():
    ingest = HangingIngest()
    function = aws_lambda.Function(LAMBDA_ENV, ingest.flusher)  # the default bound, FLUSH_MS
    wrapped = function.wrap(lambda event, ctx: "ok")
    assert wrapped({}, context()) == "ok"
    # The one wait that runs out.
    assert function.last_flush_seconds == pytest.approx(aws_lambda.FLUSH_MS / 1000, abs=0.1)
    for _ in range(20):
        assert wrapped({}, context()) == "ok"
        assert function.last_flush_seconds < NO_WAIT_SECONDS
    assert ingest.flusher.backing_off
    ingest.answer(ok=True)
    assert wait_for(lambda: not ingest.flusher.backing_off)
    assert ingest.flusher.flush(1.0) is True  # a send succeeded: the next invocations wait for theirs again


def test_a_send_that_fails_late_keeps_the_invocations_from_waiting():
    ingest = HangingIngest()
    assert ingest.flusher.flush(0.05) is False
    ingest.answer(ok=False)  # the connection timed out: nothing was sent
    time.sleep(0.1)  # the thread is idle again
    begin = time.monotonic()
    assert ingest.flusher.flush(1.0) is False
    assert time.monotonic() - begin < NO_WAIT_SECONDS


def test_a_wait_ends_with_its_own_request_never_an_earlier_ones():
    gates = [threading.Event(), threading.Event()]
    started = []

    def send(_timeout_millis):
        started.append(len(started))
        gates[min(len(started) - 1, 1)].wait(5)

    flusher = aws_lambda.Flusher(aws_lambda.Outbox(), flush_spans=send)
    assert flusher.flush(0) is False  # an invocation at the end of its time: asks, does not wait, does not back off
    assert wait_for(lambda: started == [0])
    result = []
    waiter = threading.Thread(target=lambda: result.append(flusher.flush(0.5)))
    waiter.start()
    time.sleep(0.05)
    gates[0].set()  # the earlier request is answered while this one waits
    waiter.join(2)
    assert result == [False]  # its own send is still held at the gate
    gates[1].set()


def test_the_flush_says_not_sent_when_the_last_export_failed():
    from impact_instrumentation import runtime

    provider = SimpleNamespace(force_flush=lambda timeout: True)
    failed = SimpleNamespace(provider=provider, processor=SimpleNamespace(last_export_failed=True))
    runtime._ARMED.append(failed)
    try:
        assert runtime.force_flush(100) is False
    finally:
        runtime._ARMED.remove(failed)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_an_invocation_at_the_end_of_its_time_does_not_wait():
    slow = SlowFlusher(5.0)
    wrapped = aws_lambda.Function(LAMBDA_ENV, slow.flusher).wrap(lambda event, ctx: "ok")
    begin = time.monotonic()
    assert wrapped({}, context(remaining_ms=20)) == "ok"
    assert time.monotonic() - begin < 0.1


def test_a_context_without_the_runtimes_methods_does_not_fail_the_invocation():
    wrapped = aws_lambda.Function(LAMBDA_ENV, SlowFlusher(0.0).flusher).wrap(lambda event, ctx: "ok")
    assert wrapped({}, object()) == "ok"
    assert wrapped({}, None) == "ok"


def test_the_first_invocation_names_the_functions_account_and_arn():
    wrapped = aws_lambda.Function(LAMBDA_ENV, SlowFlusher(0.0).flusher).wrap(lambda event, ctx: "ok")
    wrapped({}, context(arn=ARN + ":live"))
    attributes = otlp.platform_attributes(LAMBDA_ENV)
    assert attributes == {
        "cloud.provider": "aws",
        "cloud.platform": "aws_lambda",
        "cloud.region": "eu-west-1",
        "faas.name": "refund-bot",
        "faas.version": "$LATEST",
        "cloud.account.id": "123456789012",
        "cloud.resource_id": ARN,
    }


def test_the_handler_is_wrapped_when_the_runtime_imports_its_module(tmp_path, monkeypatch):
    package = tmp_path / "refunds"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "app.py").write_text("def handler(event, context):\n    return 'refunded'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    function = aws_lambda.Function(LAMBDA_ENV, SlowFlusher(0.0).flusher)
    finder = aws_lambda.watch_handler("refunds/app.handler", function.wrap)
    try:
        import importlib

        module = importlib.import_module("refunds.app")
        assert module.handler.__impact_lambda__ is True
        assert module.handler({}, context()) == "refunded"
        assert finder not in __import__("sys").meta_path
    finally:
        import sys

        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        for name in ("refunds.app", "refunds"):
            sys.modules.pop(name, None)


def test_a_module_without_the_handler_is_left_for_the_runtime_to_report(tmp_path, monkeypatch):
    (tmp_path / "nohandler_app.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    function = aws_lambda.Function(LAMBDA_ENV, SlowFlusher(0.0).flusher)
    finder = aws_lambda.watch_handler("nohandler_app.handler", function.wrap)
    import importlib
    import sys

    try:
        module = importlib.import_module("nohandler_app")
        assert not hasattr(module, "handler")
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        sys.modules.pop("nohandler_app", None)


# --- the outbox ------------------------------------------------------------------------------------------------------


def report(state: str) -> dict:
    from impact_instrumentation import setup_report

    return setup_report.build(state, "", environ=LAMBDA_ENV)


def test_a_setup_report_waits_for_the_invocation_and_carries_the_functions_arn():
    sink = Sink()
    try:
        environ = {**LAMBDA_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": sink.url}
        outbox = aws_lambda.Outbox()
        outbox.hold(report("active"), environ=environ)
        assert sink.requests == [] and len(outbox) == 1
        otlp.remember({"cloud.account.id": "123456789012", "cloud.resource_id": ARN})
        outbox.send()
        assert len(outbox) == 0
        payload = json.loads(gzip.decompress(sink.requests[0]["body"]))
        resource = {a["key"]: a["value"]["stringValue"] for a in payload["resourceSpans"][0]["resource"]["attributes"]}
        assert resource["faas.name"] == "refund-bot"
        assert resource["service.name"] == "refund-bot"
        assert resource["cloud.region"] == "eu-west-1"
        assert resource["cloud.account.id"] == "123456789012"
        assert resource["cloud.resource_id"] == ARN
    finally:
        sink.close()


def test_a_report_that_cannot_be_sent_is_tried_with_the_next_invocations_then_dropped():
    sent = []

    def refused(url, payload, **kwargs):
        sent.append(url)
        raise ConnectionRefusedError("no route to ingest")

    environ = {**LAMBDA_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.example.com"}
    outbox = aws_lambda.Outbox()
    outbox.hold(report("failed"), environ=environ)
    for _ in range(aws_lambda.REPORT_ATTEMPTS + 2):
        outbox.send(post=refused)
    assert len(sent) == aws_lambda.REPORT_ATTEMPTS
    assert len(outbox) == 0


def test_a_report_trusts_the_ca_the_exporter_trusts():
    seen = []
    environ = {**LAMBDA_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.example.com",
               "OTEL_EXPORTER_OTLP_CERTIFICATE": "/var/task/ingest-ca.pem"}  # fmt: skip
    outbox = aws_lambda.Outbox()
    outbox.hold(report("active"), environ=environ)
    assert outbox.send(post=lambda url, payload, **kwargs: seen.append(kwargs) or SimpleNamespace(ok=True)) is True
    assert seen[0]["ca_file"] == "/var/task/ingest-ca.pem"


def test_one_tls_context_serves_every_send_until_the_ca_file_changes(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(otlp, "_TLS_CONTEXTS", {})
    monkeypatch.setattr(otlp.ssl, "create_default_context", lambda cafile=None: made.append(cafile) or object())
    assert otlp.tls_context() is otlp.tls_context()
    ca = tmp_path / "ca.pem"
    ca.write_text("one")
    first = otlp.tls_context(str(ca))
    assert otlp.tls_context(str(ca)) is first
    ca.write_text("a rotated CA")
    assert otlp.tls_context(str(ca)) is not first
    assert made == [None, str(ca), str(ca)]


def test_a_stood_down_function_sends_no_report_to_the_other_tracers_endpoint():
    outbox = aws_lambda.Outbox()
    outbox.hold(report("pre-existing"), environ={**LAMBDA_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://other.example"})
    assert len(outbox) == 0


def test_the_setup_report_goes_through_the_function_that_lambda_replaces():
    """aws_lambda holds reports by replacing setup_report.send_in_background; report() must call it by that name."""
    from impact_instrumentation import setup_report

    held = []
    original = setup_report.send_in_background
    setup_report.send_in_background = lambda payload, *, environ: held.append(payload)
    try:
        setup_report.report("active", "", environ={"IMPACT_STATE_DIR": "/nonexistent"}, root="/nonexistent-root")
    finally:
        setup_report.send_in_background = original
    assert len(held) == 1


# --- the resource ----------------------------------------------------------------------------------------------------


def test_a_lambda_function_is_named_after_itself_unless_the_environment_names_it():
    assert otlp.default_service_name(LAMBDA_ENV) == "refund-bot"
    assert otlp.default_service_name({**LAMBDA_ENV, "OTEL_SERVICE_NAME": "refunds"}) is None
    assert otlp.resource_attributes(LAMBDA_ENV)["service.name"] == "refund-bot"


def test_the_environments_resource_attributes_win_over_lambdas():
    attributes = otlp.resource_attributes({**LAMBDA_ENV, "OTEL_RESOURCE_ATTRIBUTES": "cloud.region=us-east-1"})
    assert attributes["cloud.region"] == "us-east-1"
    assert attributes["faas.name"] == "refund-bot"


def test_outside_lambda_nothing_is_added():
    assert otlp.platform_attributes({"OTEL_SERVICE_NAME": "x"}) == {}
    otlp.remember({"cloud.account.id": "123456789012"})
    assert otlp.platform_attributes({}) == {}


def test_facts_learnt_after_start_reach_every_resource_once():
    payload = {
        "resourceSpans": [{"resource": {"attributes": [{"key": "cloud.account.id", "value": {"stringValue": "x"}}]}}]
    }
    assert otlp.with_late_attributes(payload) is payload  # nothing learnt: unchanged
    otlp.remember({"cloud.account.id": "123456789012", "cloud.resource_id": ARN})
    otlp.with_late_attributes(payload)
    otlp.with_late_attributes(payload)
    keys = [a["key"] for a in payload["resourceSpans"][0]["resource"]["attributes"]]
    assert keys == ["cloud.account.id", "cloud.resource_id"]
    assert payload["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"] == "x"


def test_the_flush_reaches_the_tracer_providers_of_the_process():
    from impact_instrumentation import runtime

    flushed = []
    provider = SimpleNamespace(force_flush=lambda timeout: flushed.append(timeout) or True)
    armed = SimpleNamespace(provider=provider)
    idle = SimpleNamespace(provider=None)
    runtime._ARMED.extend([armed, idle])
    try:
        assert runtime.force_flush(1234) is True
    finally:
        runtime._ARMED.remove(armed)
        runtime._ARMED.remove(idle)
    assert flushed == [1234]


def test_the_sender_is_a_daemon_thread():
    slow = SlowFlusher(0.0)
    slow.flusher.flush(1.0)
    threads = [t for t in threading.enumerate() if t.name == "impact-lambda-flush"]
    assert threads and all(t.daemon for t in threads)
