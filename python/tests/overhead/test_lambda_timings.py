# SPDX-License-Identifier: Apache-2.0

"""The cold start and invocation harness of the Lambda layer (scripts/lambda_timings.py): Lambda's REPORT line, the
percentiles, what a cell adds, the report, the gate that reports and does not fail until the budget is decided, and the
AWS mode's calls against a stand-in for the AWS CLI."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import ssl
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import lambda_timings as timings  # noqa: E402

# As Lambda writes them (tab-separated), the first invocation of an environment with its Init Duration.
COLD = (
    "REPORT RequestId: 3f1c2b8e-5d0a-4c1e-9a7b-0f6e2d4c8a11\tDuration: 812.41 ms\tBilled Duration: 813 ms\t"
    "Memory Size: 512 MB\tMax Memory Used: 98 MB\tInit Duration: 431.07 ms\t"
)
WARM = (
    "REPORT RequestId: 9b2e4f60-1c3d-4e5f-8a9b-7c6d5e4f3a21\tDuration: 41.9 ms\tBilled Duration: 42 ms\t"
    "Memory Size: 512 MB\tMax Memory Used: 99 MB\t"
)
# The emulator's, with the fields in another order and spaces.
RIE = "REPORT RequestId: 0d7c  Init Duration: 0.32 ms  Duration: 120.55 ms  Billed Duration: 121 ms  Memory Size: 3008 MB  Max Memory Used: 3008 MB"  # noqa: E501


def test_a_cold_report_line_gives_init_duration_and_memory():
    assert timings.parse_report(COLD) == {
        "requestId": "3f1c2b8e-5d0a-4c1e-9a7b-0f6e2d4c8a11",
        "durationMs": 812.41,
        "billedMs": 813.0,
        "memorySizeMB": 512,
        "maxMemoryMB": 98,
        "initMs": 431.07,
    }


def test_a_warm_report_line_has_no_init_duration():
    found = timings.parse_report(WARM)
    assert found["durationMs"] == 41.9
    assert "initMs" not in found


def test_the_emulators_report_line_is_read_too():
    found = timings.parse_report("2026-11-10 12:00:00 " + RIE)
    assert found["requestId"] == "0d7c"
    assert found["durationMs"] == 120.55
    assert found["initMs"] == 0.32
    assert found["billedMs"] == 121.0


@pytest.mark.parametrize("line", ["START RequestId: x Version: $LATEST", "END RequestId: x", "", "REPORT nothing"])
def test_other_lines_are_not_reports(line):
    assert timings.parse_report(line) is None


def test_reports_are_found_among_the_other_lines_of_a_log():
    log = f"START RequestId: a\n{COLD}\nEND RequestId: a\nsomething the function printed\n{WARM}\n"
    assert [r["durationMs"] for r in timings.reports(log)] == [812.41, 41.9]


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([1, 2, 3, 4, 5], 50, 3),
        ([1, 2, 3, 4], 50, 2.5),
        ([5, 1, 4, 2, 3], 0, 1),
        ([5, 1, 4, 2, 3], 100, 5),
        (list(range(1, 31)), 95, 28.55),  # 30 cold starts
        ([7.0], 95, 7.0),
    ],
)
def test_percentiles_interpolate_between_ranks(values, q, expected):
    assert timings.percentile(values, q) == pytest.approx(expected)


def test_no_values_have_no_percentile():
    with pytest.raises(ValueError):
        timings.percentile([], 50)


def side(init, durations, memory) -> dict:
    return {"initMs": init, "durationsMs": durations, "memoryMiB": memory}


def cell(added_init=100.0, added_duration=5.0, added_memory=10.0, python="3.12", memory=512) -> dict:
    plain = side([200.0 + i for i in range(30)], [40.0 + i / 10 for i in range(20)], [80.0, 81.0, 82.0])
    layer = side(
        [v + added_init for v in plain["initMs"]],
        [v + added_duration for v in plain["durationsMs"]],
        [v + added_memory for v in plain["memoryMiB"]],
    )
    return {"python": python, "architecture": "x86_64", "memoryMB": memory, "plain": plain, "layer": layer}


def test_a_cell_adds_the_layer_sides_percentile_less_the_plain_sides():
    found = timings.compare(cell(added_init=120.0, added_duration=4.0, added_memory=12.5))
    assert found["id"] == "python3.12-x86_64-512"
    assert found["added"] == {"initP50Ms": 120.0, "initP95Ms": 120.0, "durationP95Ms": 4.0, "memoryMiB": 12.5}
    assert all(found["within"].values())
    assert found["coldStarts"] == 30


def test_a_cell_over_the_budget_is_reported_and_the_gate_does_not_fail_until_the_budget_is_decided():
    samples = {"mode": "rie", "version": "1.2.3", "cells": [cell(), cell(added_init=700.0, memory=1024)]}
    summary = timings.summarize(samples)
    assert summary["within"] is False
    assert summary["budgetDecided"] is False
    assert summary["worst"]["initP95Ms"] == 700.0
    assert summary["measures"]["serverless.cold_start_added_ms"] == 700.0
    assert summary["measures"]["serverless.invocation_added_ms"] == 5.0
    assert summary["measures"]["serverless.memory_added_mib"] == 10.0
    assert timings.gate(summary, enforce=False) == 0
    assert timings.gate(summary, enforce=True) == 1
    text = timings.render(summary)
    assert (
        "| Python 3.12, x86_64, 1024 MB | +700 ms | +700 ms | +5.0 ms | +10.0 MiB | over the proposed budget:" in text
    )
    assert "| Proposed budget | +300 ms | +600 ms | +25.0 ms | +40.0 MiB | not decided" in text
    assert "Runtime Interface Emulator" in text


def test_a_run_that_measured_nothing_fails():
    summary = timings.summarize({"mode": "aws", "cells": []})
    assert timings.gate(summary, enforce=False) == 2


def test_the_report_can_be_rendered_again_from_the_samples(tmp_path):
    samples = {"schema": timings.SCHEMA, "mode": "aws", "version": "1.2.3", "method": {"coldStarts": 30, "warm": 20}}
    samples["cells"] = [cell()]
    (tmp_path / "samples.json").write_text(json.dumps(samples))
    assert timings.main(["report", str(tmp_path)]) == 0
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["schema"] == timings.SCHEMA
    assert results["cells"][0]["added"]["initP95Ms"] == 100.0
    text = (tmp_path / "LAMBDA-TIMINGS.md").read_text()
    assert "started cold 30 times and then invoked 20 times" in text
    assert "Lambda's REPORT line" in text


def test_the_function_both_sides_run_is_valid_python():
    source = timings.function_source()
    compile(source, "app.py", "exec")
    assert "client.converse(" in source


def test_the_layer_exports_over_tls_with_a_ca_bundle_of_the_real_size():
    environment = timings.function_environment(layer=True)
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("https://127.0.0.1:")
    assert environment["OTEL_EXPORTER_OTLP_CERTIFICATE"] == timings.CA_BUNDLE
    assert "get_default_verify_paths()" in timings.function_source()  # the image's own bundle, the stub's CA added


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_the_stub_certificate_is_trusted_by_the_packages_tls_context(tmp_path):
    from impact_instrumentation import otlp

    timings.stub_certificates(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "otlp-cert.pem", tmp_path / "otlp-key.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    errors = []

    def serve():
        connection, _ = listener.accept()
        try:
            with server_context.wrap_socket(connection, server_side=True):
                pass
        except (OSError, ssl.SSLError) as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = otlp.tls_context(str(tmp_path / "otlp-ca.pem"))  # Python's default checks, X.509 strict from 3.13
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            assert tls.version()
    thread.join(5)
    listener.close()
    assert not errors


def test_only_the_layer_side_names_the_wrapper():
    assert timings.function_environment(layer=True)["AWS_LAMBDA_EXEC_WRAPPER"] == "/opt/impact-ai-instrument"
    assert "AWS_LAMBDA_EXEC_WRAPPER" not in timings.function_environment(layer=False)


def test_only_the_emulator_gets_the_settings_lambda_reserves():
    emulator = timings.emulator_environment(layer=True, memory=512)
    assert emulator["AWS_ACCESS_KEY_ID"] and emulator["AWS_REGION"] == "eu-west-1"
    for layer in (True, False):
        assert not set(timings.function_environment(layer=layer)) & RESERVED
    with pytest.raises(ValueError, match="AWS_SESSION_TOKEN"):
        timings.lambda_variables({"AWS_SESSION_TOKEN": "x"})


# --- the AWS mode, against a stand-in for the AWS CLI -----------------------------------------------------------------


# The environment variables Lambda reserves: a create-function or update-function-configuration that sets one is refused
# with InvalidParameterValueException (AWS Lambda Developer Guide, "Defined runtime environment variables").
RESERVED = {
    "_HANDLER", "_X_AMZN_TRACE_ID", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_EXECUTION_ENV", "AWS_LAMBDA_FUNCTION_NAME",
    "AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "AWS_LAMBDA_FUNCTION_VERSION", "AWS_LAMBDA_INITIALIZATION_TYPE",
    "AWS_LAMBDA_LOG_GROUP_NAME", "AWS_LAMBDA_LOG_STREAM_NAME", "AWS_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_LAMBDA_RUNTIME_API", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR",
}  # fmt: skip


class FakeAws:
    """Answers the AWS CLI's calls as Lambda does: a cold REPORT after each change of a setting, warm ones after, and a
    refusal for a function's settings that name a reserved variable."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.cold = False

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        command = args[1]
        if "--environment" in args:
            variables = json.loads(args[args.index("--environment") + 1])["Variables"]
            reserved = sorted(set(variables) & RESERVED)
            if reserved:
                raise RuntimeError(
                    f"aws lambda {command}: InvalidParameterValueException: Lambda was unable to configure your "
                    f"environment variables because the environment variables you have provided contains reserved "
                    f"keys that are currently not supported for modification. Reserved keys used in this request: "
                    f"{','.join(reserved)}"
                )
        if command == "publish-layer-version":
            return json.dumps({"LayerVersionArn": "arn:aws:lambda:eu-west-1:123456789012:layer:x:3", "Version": 3})
        if command == "update-function-configuration":
            self.cold = True
            return "{}"
        if command == "invoke":
            line = COLD if self.cold else WARM
            self.cold = False
            return json.dumps({"StatusCode": 200, "LogResult": base64.b64encode(line.encode()).decode()})
        return "{}"


def test_the_aws_mode_starts_each_side_cold_by_a_change_of_a_setting_and_deletes_what_it_made(tmp_path):
    fake = FakeAws()
    aws = timings.Aws(region="eu-west-1", role_arn="arn:aws:iam::123456789012:role/timings", prefix="t", run=fake)
    arn = aws.publish_layer(tmp_path / "layer.zip", "arm64")
    aws.create("t-312-arm64-512-layer", python="3.12", architecture="arm64", memory=512, code_zip=tmp_path / "c.zip",
               layer=arn)  # fmt: skip
    measured = aws.side("t-312-arm64-512-layer", layer=True, cold_starts=3, warm=2)
    aws.cleanup()
    assert measured == {"initMs": [431.07] * 3, "durationsMs": [41.9, 41.9], "memoryMiB": [98.0] * 3 + [99.0] * 2}
    commands = [call[1] for call in fake.calls]
    assert commands.count("update-function-configuration") == 3
    assert commands[-2:] == ["delete-function", "delete-layer-version"]
    create = next(call for call in fake.calls if call[1] == "create-function")
    assert create[create.index("--layers") + 1] == arn
    assert create[create.index("--runtime") + 1] == "python3.12"
    assert create[create.index("--memory-size") + 1] == "512"
    variables = json.loads(create[create.index("--environment") + 1])["Variables"]
    assert variables["AWS_LAMBDA_EXEC_WRAPPER"] == "/opt/impact-ai-instrument"
    assert all(call[-4:] == ["--region", "eu-west-1", "--output", "json"] for call in fake.calls)


def test_a_cold_start_without_init_duration_is_an_error():
    aws = timings.Aws(region="eu-west-1", role_arn="r", prefix="t", run=lambda args: json.dumps(
        {"StatusCode": 200, "LogResult": base64.b64encode(WARM.encode()).decode()}
    ) if args[1] == "invoke" else "{}")  # fmt: skip
    with pytest.raises(RuntimeError, match="without Init Duration"):
        aws.side("t", layer=False, cold_starts=1, warm=0)


def test_a_failing_function_is_an_error():
    aws = timings.Aws(region="eu-west-1", role_arn="r", prefix="t", run=lambda args: json.dumps(
        {"StatusCode": 200, "FunctionError": "Unhandled", "LogResult": ""}
    ))  # fmt: skip
    with pytest.raises(RuntimeError, match="the function failed"):
        aws.invoke("t")
