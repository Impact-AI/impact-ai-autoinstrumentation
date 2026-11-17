#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Cold starts and invocations of a Python function on AWS Lambda with the layer and without it.

    python3 scripts/lambda_timings.py rie --layers build/lambda --out build/lambda-timings [--cold-starts 30]
    python3 scripts/lambda_timings.py aws --layers build/lambda --out build/lambda-timings --region R --role-arn ARN
    python3 scripts/lambda_timings.py report build/lambda-timings

Each cell is a Python, an architecture and a memory size (512 and 1,024 MB by default). In each cell the same function
runs without the layer and with it (AWS_LAMBDA_EXEC_WRAPPER and the layer, as a function's change adds them), and each
side is started cold --cold-starts times (30 by default) and then invoked --warm times. Every invocation makes one model
call: the function answers Bedrock's Converse API from a stub on 127.0.0.1 it starts at init, and takes OTLP itself over
HTTPS, from a TLS stub on 127.0.0.1 whose CA it adds to the CA bundle of the image. So an export pays what it pays on
Lambda in CPU (the TLS context with the whole CA bundle, the handshake, the request), and the numbers leave out only the
network's round trips to an ingest host, which depend on where the function and the ingest are.

  rie  AWS's Lambda images under their Runtime Interface Emulator, with the container CLI (CONTAINER=docker|podman). A
       cold start is a new container and its first invocation, timed by the client (the emulator starts the runtime when
       the first invocation arrives); memory is the container's peak (cgroup memory.peak). The emulator is not Lambda:
       these numbers show the layer works and how its cost moves, not what a function pays. CI runs a smoke of it.
  aws  Lambda itself, in a test account: the layer is published in it, functions are created, every cold start follows
       a change of a setting (Lambda starts a new environment), and the times are those of Lambda's REPORT line: Init
       Duration, Duration and Max Memory Used. Everything it created is deleted at the end. Needs the AWS CLI with
       credentials for the test account and an execution role (--role-arn) that Lambda can assume.

It writes samples.json (every measurement), results.json and LAMBDA-TIMINGS.md, in the form of the overhead report
(scripts/overhead_report.py): what each cell adds at the median and the 95th percentile of the cold start, the 95th
percentile of an invocation, and memory, held against the budget. The budget is proposed and not decided yet, so the
report says where a cell stands and the exit status does not depend on it unless --enforce is given.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "impact.lambda-timings.v1"
# Proposed for Python, not decided: init added at the median and the 95th percentile of the cold starts, an invocation
# with one model call added at the 95th percentile (the export included), and the memory of the package's overhead
# budget. The gate reports against these; it fails a run only with --enforce.
BUDGET = {"initP50Ms": 300.0, "initP95Ms": 600.0, "durationP95Ms": 25.0, "memoryMiB": 40.0}
MEASURES = {
    "serverless.cold_start_added_ms": "initP95Ms",
    "serverless.invocation_added_ms": "durationP95Ms",
    "serverless.memory_added_mib": "memoryMiB",
}
PYTHONS = ("3.11", "3.12", "3.13", "3.14")
MEMORY_SIZES = (512, 1024)
STUB_PORT = 9901  # Bedrock's Converse API, plain HTTP
OTLP_PORT = 9902  # OTLP over TLS
# Where the function writes, at init, the image's CA bundle with the stub's CA added.
CA_BUNDLE = "/tmp/impact-timings-ca.pem"  # noqa: S108 - inside the function: Lambda's only writable directory
# The settings Lambda keeps for itself: a function's configuration that names one is refused. Only the emulator takes
# them (fake credentials for boto3, which never reach AWS); aws mode never sends them.
LAMBDA_RESERVED = frozenset(
    {
        "_HANDLER", "_X_AMZN_TRACE_ID", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_EXECUTION_ENV",
        "AWS_LAMBDA_FUNCTION_NAME", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "AWS_LAMBDA_FUNCTION_VERSION",
        "AWS_LAMBDA_INITIALIZATION_TYPE", "AWS_LAMBDA_LOG_GROUP_NAME", "AWS_LAMBDA_LOG_STREAM_NAME",
        "AWS_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_LAMBDA_RUNTIME_API", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR",
    }
)  # fmt: skip
SIDES = ("plain", "layer")
LAMBDA_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}

# --- Lambda's REPORT line ---------------------------------------------------------------------------------------------

_FIELD = re.compile(
    r"(Init Duration|Restore Duration|Billed Duration|Max Memory Used|Memory Size|Duration):\s*([0-9.]+)\s*(ms|MB)"
)
_NAMES = {
    "Init Duration": "initMs",
    "Restore Duration": "restoreMs",
    "Billed Duration": "billedMs",
    "Max Memory Used": "maxMemoryMB",
    "Memory Size": "memorySizeMB",
    "Duration": "durationMs",
}


def parse_report(line: str) -> dict | None:
    """The numbers of a REPORT line of Lambda's log ('REPORT RequestId: ...\\tDuration: 12.34 ms\\t...'); None for any
    other line. Init Duration is there only for the first invocation of an environment."""
    at = line.find("REPORT RequestId:")
    if at < 0:
        return None
    rest = line[at + len("REPORT RequestId:") :].strip()
    request, _, fields = rest.partition("\t") if "\t" in rest else rest.partition(" ")
    found: dict = {"requestId": request.strip()}
    for name, value, _unit in _FIELD.findall(fields):
        number = float(value)
        found[_NAMES[name]] = int(number) if name in ("Memory Size", "Max Memory Used") else number
    return found if "durationMs" in found else None


def reports(text: str) -> list[dict]:
    return [found for line in text.splitlines() if (found := parse_report(line)) is not None]


# --- numbers ----------------------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """The q-th percentile, linearly between the two nearest ranks (numpy's default)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def side_summary(side: dict) -> dict:
    return {
        "initP50Ms": percentile(side["initMs"], 50),
        "initP95Ms": percentile(side["initMs"], 95),
        "durationP95Ms": percentile(side["durationsMs"], 95),
        "memoryMiB": statistics.median(side["memoryMiB"]),
    }


def compare(cell: dict) -> dict:
    """What the layer adds in one cell: the layer side's figure less the plain side's, per measure."""
    plain, layer = side_summary(cell["plain"]), side_summary(cell["layer"])
    added = {key: round(layer[key] - plain[key], 3) for key in BUDGET}
    return {
        "id": f"python{cell['python']}-{cell['architecture']}-{cell['memoryMB']}",
        "python": cell["python"],
        "architecture": cell["architecture"],
        "memoryMB": cell["memoryMB"],
        "added": added,
        "plain": {key: round(value, 3) for key, value in plain.items()},
        "layer": {key: round(value, 3) for key, value in layer.items()},
        "coldStarts": len(cell["layer"]["initMs"]),
        "within": {key: added[key] <= BUDGET[key] for key in BUDGET},
    }


def summarize(samples: dict) -> dict:
    cells = [compare(cell) for cell in samples["cells"]]
    worst = {key: max(cell["added"][key] for cell in cells) for key in BUDGET} if cells else {}
    return {
        "schema": SCHEMA,
        "version": samples.get("version", "0.0.0-dev"),
        "mode": samples["mode"],
        "budget": BUDGET,
        "budgetDecided": False,
        "method": samples.get("method", {}),
        "worst": worst,
        "measures": {name: worst[key] for name, key in MEASURES.items()} if worst else {},
        "cells": cells,
        "within": all(all(cell["within"].values()) for cell in cells),
    }


def render(summary: dict) -> str:
    budget, method = summary["budget"], summary["method"]

    def row(name: str, numbers: dict, mark: str = "") -> str:
        return (
            f"| {name} | {numbers['initP50Ms']:+.0f} ms | {numbers['initP95Ms']:+.0f} ms | "
            f"{numbers['durationP95Ms']:+.1f} ms | {numbers['memoryMiB']:+.1f} MiB | {mark} |"
        )

    rows = []
    for cell in summary["cells"]:
        over = [key for key, ok in cell["within"].items() if not ok]
        mark = "over the proposed budget: " + ", ".join(over) if over else ""
        rows.append(row(f"Python {cell['python']}, {cell['architecture']}, {cell['memoryMB']} MB", cell["added"], mark))
    if summary["worst"]:
        rows.append(row("Worst of these", summary["worst"]))
    rows.append(row("Proposed budget", budget, "not decided: reported, not enforced"))
    where = {
        "rie": (
            "AWS's Lambda images under their Runtime Interface Emulator. A cold start is a new container and its first "
            "invocation, timed by the client; memory is the container's peak. The emulator is not Lambda: these "
            "numbers show that the layer works and how its cost moves from one build to the next, not what a function "
            "on Lambda pays."
        ),
        "aws": (
            "Lambda, in a test account. A cold start follows a change of a setting, which makes Lambda start a new "
            "environment; the times are those of Lambda's REPORT line (Init Duration, Duration, Max Memory Used)."
        ),
    }[summary["mode"]]
    return (
        f"# Cold start and invocation of the Python layer for AWS Lambda, {summary['version']}\n\n"
        "What a Python function costs more with the layer than the same function without it. Each invocation makes "
        "one model call to a stub in the function (Bedrock's Converse API), and the layer sends its spans over HTTPS "
        "to an OTLP stub in the function before the invocation returns.\n\n"
        "| Cell | Init added, median | Init added, 95th percentile | Invocation added, 95th percentile "
        "| Memory added | |\n"
        "|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n## How it is measured\n\n"
        f"{where} Each side of a cell was started cold {method.get('coldStarts', '?')} times and then invoked "
        f"{method.get('warm', '?')} times; a figure added is the layer side's percentile less the plain side's. "
        f"The spans went to {method.get('export', 'an OTLP stub in the function')}: the figures leave out the round "
        "trips to an ingest host.\n"
    )


def gate(summary: dict, *, enforce: bool) -> int:
    """0 when the run measured something; with --enforce, 1 when a cell is over the budget."""
    if not summary["cells"]:
        return 2
    return 1 if enforce and not summary["within"] else 0


# --- the function -----------------------------------------------------------------------------------------------------

FUNCTION = """
# The function the timings run: one model call per invocation, to a stub that it starts at init, and a TLS stub that
# takes the layer's spans. Both sides of a cell run this same code.
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

PORT = int(os.environ.get("STUB_PORT", "{port}"))
OTLP_PORT = {otlp_port}
HERE = os.path.dirname(os.path.abspath(__file__))
ANSWER = {{
    "output": {{"message": {{"role": "assistant", "content": [{{"text": "Refund approved."}}]}}}},
    "stopReason": "end_turn",
    "usage": {{"inputTokens": 7, "outputTokens": 5, "totalTokens": 12}},
    "metrics": {{"latencyMs": 1}},
}}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps(ANSWER if self.path.endswith("/converse") else {{}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", PORT), Stub)
threading.Thread(target=server.serve_forever, daemon=True).start()

# The image's own CA bundle and the stub's CA: the package builds its TLS context from a bundle of the real size.
paths = ssl.get_default_verify_paths()
with open({ca_bundle!r}, "wb") as bundle:
    for path in (paths.cafile, paths.openssl_cafile):
        if path and os.path.isfile(path):
            with open(path, "rb") as found:
                bundle.write(found.read() + b"\\n")
            break
    with open(os.path.join(HERE, "otlp-ca.pem"), "rb") as found:
        bundle.write(found.read())
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(os.path.join(HERE, "otlp-cert.pem"), os.path.join(HERE, "otlp-key.pem"))
otlp = ThreadingHTTPServer(("127.0.0.1", OTLP_PORT), Stub)
otlp.socket = tls.wrap_socket(otlp.socket, server_side=True)
threading.Thread(target=otlp.serve_forever, daemon=True).start()
client = boto3.client(
    "bedrock-runtime", endpoint_url=f"http://127.0.0.1:{{PORT}}", region_name=os.environ["AWS_REGION"]
)


def handler(event, context):
    response = client.converse(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        messages=[{{"role": "user", "content": [{{"text": "Refund order A-1"}}]}}],
    )
    return {{"answer": response["output"]["message"]["content"][0]["text"]}}
"""


def function_source() -> str:
    return FUNCTION.format(port=STUB_PORT, otlp_port=OTLP_PORT, ca_bundle=CA_BUNDLE)


CODE_FILES = ("app.py", "otlp-ca.pem", "otlp-cert.pem", "otlp-key.pem")


def stub_certificates(directory: Path, *, openssl: str = "openssl") -> None:
    """A CA made for this run and a certificate for 127.0.0.1 signed by it (otlp-ca.pem, otlp-cert.pem, otlp-key.pem);
    the CA's key is thrown away. With the openssl CLI."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        (work / "leaf.ext").write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
        )
        steps = [
            ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=lambda-timings-ca",
             "-keyout", str(work / "ca.key"), "-out", str(directory / "otlp-ca.pem"),
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            ["req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
             "-keyout", str(directory / "otlp-key.pem"), "-out", str(work / "leaf.csr")],
            ["x509", "-req", "-in", str(work / "leaf.csr"), "-CA", str(directory / "otlp-ca.pem"),
             "-CAkey", str(work / "ca.key"), "-CAcreateserial", "-days", "2", "-extfile", str(work / "leaf.ext"),
             "-out", str(directory / "otlp-cert.pem")],
        ]  # fmt: skip
        for step in steps:
            subprocess.run(  # noqa: S603 - the openssl CLI with our arguments
                [openssl, *step], capture_output=True, text=True, timeout=60, check=True
            )


def function_code(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "app.py").write_text(function_source())
    stub_certificates(directory)
    for name in CODE_FILES:
        (directory / name).chmod(0o644)
    directory.chmod(0o755)
    return directory


def function_environment(*, layer: bool) -> dict[str, str]:
    """The function's own settings, as aws mode sends them to Lambda: none of Lambda's reserved names."""
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://127.0.0.1:{OTLP_PORT}",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20timings",
        "OTEL_EXPORTER_OTLP_CERTIFICATE": CA_BUNDLE,
    }
    if layer:
        env["AWS_LAMBDA_EXEC_WRAPPER"] = "/opt/impact-ai-instrument"
    return env


def emulator_environment(*, layer: bool, memory: int) -> dict[str, str]:
    """The function's settings and what Lambda itself would set, for the emulator: the function's name, memory and
    region, and fake credentials for boto3 (the Bedrock stub does not check them)."""
    return {
        **function_environment(layer=layer),
        "AWS_LAMBDA_FUNCTION_NAME": "timings",
        "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(memory),
        "AWS_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "fake-access-key",
        "AWS_SECRET_ACCESS_KEY": "fake-secret-key",
    }


def lambda_variables(variables: dict[str, str]) -> str:
    """The --environment argument of the AWS CLI; a reserved name is a bug here, not a request for Lambda to refuse."""
    reserved = sorted(set(variables) & LAMBDA_RESERVED)
    if reserved:
        raise ValueError(f"Lambda reserves {', '.join(reserved)}")
    return json.dumps({"Variables": variables})


def unpack(archive: Path, target: Path) -> Path:
    """A layer archive unpacked as Lambda unpacks it into /opt, with the modes of its entries."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as layer:
        for info in layer.infolist():
            Path(layer.extract(info, target)).chmod((info.external_attr >> 16) & 0o777 or 0o644)
    for path in [target, *target.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
    return target


# --- rie --------------------------------------------------------------------------------------------------------------


class Rie:
    """Lambda's images under their emulator, through the container CLI."""

    def __init__(self, images: dict[str, str], opt: Path, code: Path, container: str | None = None):
        self.images = images
        self.opt = opt
        self.code = code
        self.container = container or os.environ.get("CONTAINER") or ("docker" if shutil.which("docker") else "podman")

    def _run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(  # noqa: S603 - the container CLI with our arguments
            [self.container, *args], capture_output=True, text=True, timeout=120, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"{self.container} {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def _logs(self, name: str) -> str:
        """What the function's container wrote, both streams: the emulator writes Lambda's REPORT lines."""
        result = subprocess.run(  # noqa: S603
            [self.container, "logs", name], capture_output=True, text=True, timeout=60, check=False
        )
        return result.stdout + "\n" + result.stderr

    def _invoke(self, port: int) -> float:
        url = f"http://127.0.0.1:{port}/2015-03-31/functions/function/invocations"
        request = urllib.request.Request(url, data=b"{}", method="POST")  # noqa: S310 - the local emulator
        begin = time.monotonic()
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            body = response.read()
        elapsed = (time.monotonic() - begin) * 1000
        if b"Refund approved." not in body:
            raise RuntimeError(f"the function did not answer: {body[:300]!r}")
        return elapsed

    def _ready(self, port: int) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)  # noqa: S310
                return
            except urllib.error.HTTPError:
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("the emulator did not listen")

    def _memory_mib(self, name: str) -> float:
        for file in ("memory.peak", "memory.current"):
            out = self._run("exec", name, "cat", f"/sys/fs/cgroup/{file}", check=False).strip()
            if out.isdigit():
                return int(out) / (1024 * 1024)
        return float("nan")

    def cold_start(self, *, python: str, memory: int, layer: bool, warm: int) -> dict:
        name = f"impact-timings-{uuid.uuid4().hex[:10]}"
        args = ["run", "-d", "--name", name, "-p", "127.0.0.1::8080", "--memory", f"{memory}m"]
        env = emulator_environment(layer=layer, memory=memory)
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += ["-v", f"{self.code}:/var/task:ro"]
        if layer:
            args += ["-v", f"{self.opt}:/opt:ro"]
        args += [self.images[python], "app.handler"]
        self._run(*args)
        try:
            port = int(self._run("port", name, "8080/tcp").strip().splitlines()[0].rsplit(":", 1)[1])
            self._ready(port)
            cold = self._invoke(port)
            client = [self._invoke(port) for _ in range(warm)]
            memory_mib = self._memory_mib(name)
            lines = reports(self._logs(name))
        finally:
            self._run("rm", "-f", name, check=False)
        seen, durations = set(), []
        for found in lines:
            if found["requestId"] not in seen:
                seen.add(found["requestId"])
                durations.append(found["durationMs"])
        warm_durations = durations[1:] if len(durations) == warm + 1 else client
        return {"initMs": cold, "durationsMs": warm_durations, "memoryMiB": memory_mib}


# --- aws --------------------------------------------------------------------------------------------------------------


class Aws:
    """Lambda in a test account, through the AWS CLI. Everything it creates carries the prefix and is deleted at the
    end."""

    def __init__(self, *, region: str, role_arn: str, prefix: str, run: Callable[[list[str]], str] | None = None):
        self.region = region
        self.role_arn = role_arn
        self.prefix = prefix
        self._run = run or self._aws
        self.functions: list[str] = []
        self.layers: list[tuple[str, int]] = []

    @staticmethod
    def _aws(args: list[str]) -> str:
        result = subprocess.run(  # noqa: S603 - the AWS CLI with our arguments
            ["aws", *args],  # noqa: S607 - the AWS CLI on the PATH, as its own documentation runs it
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"aws {' '.join(args[:3])}: {result.stderr.strip()}")
        return result.stdout

    def call(self, *args: str) -> dict:
        out = self._run([*args, "--region", self.region, "--output", "json"])
        return json.loads(out) if out.strip() else {}

    def publish_layer(self, archive: Path, architecture: str) -> str:
        name = f"{self.prefix}-layer-{architecture}"
        found = self.call(
            "lambda", "publish-layer-version", "--layer-name", name, "--zip-file", f"fileb://{archive}",
            "--compatible-architectures", architecture,
        )  # fmt: skip
        self.layers.append((name, int(found["Version"])))
        return found["LayerVersionArn"]

    def create(self, name: str, *, python: str, architecture: str, memory: int, code_zip: Path, layer: str | None):
        args = [
            "lambda", "create-function", "--function-name", name, "--runtime", f"python{python}",
            "--role", self.role_arn, "--handler", "app.handler", "--zip-file", f"fileb://{code_zip}",
            "--architectures", architecture, "--memory-size", str(memory), "--timeout", "30",
            "--environment", lambda_variables(function_environment(layer=layer is not None)),
        ]  # fmt: skip
        if layer:
            args += ["--layers", layer]
        self.call(*args)
        self.functions.append(name)
        self.call("lambda", "wait", "function-active-v2", "--function-name", name)

    def invoke(self, name: str) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".json") as out:
            found = self.call(
                "lambda", "invoke", "--function-name", name, "--log-type", "Tail", "--payload", "{}",
                "--cli-binary-format", "raw-in-base64-out", out.name,
            )  # fmt: skip
        if found.get("FunctionError"):
            raise RuntimeError(f"{name}: the function failed ({found['FunctionError']})")
        log = base64.b64decode(found.get("LogResult", "")).decode(errors="replace")
        lines = reports(log)
        if not lines:
            raise RuntimeError(f"{name}: no REPORT line in the log tail")
        return lines[-1]

    def cold_start(self, name: str, *, layer: bool, round_number: int) -> dict:
        variables = {**function_environment(layer=layer), "IMPACT_TIMINGS_ROUND": str(round_number)}
        self.call(
            "lambda", "update-function-configuration", "--function-name", name,
            "--environment", lambda_variables(variables),
        )  # fmt: skip
        self.call("lambda", "wait", "function-updated-v2", "--function-name", name)
        return self.invoke(name)

    def side(self, name: str, *, layer: bool, cold_starts: int, warm: int) -> dict:
        init, memory, durations = [], [], []
        for number in range(cold_starts):
            cold = self.cold_start(name, layer=layer, round_number=number)
            if "initMs" not in cold:
                raise RuntimeError(f"{name}: a cold start without Init Duration")
            init.append(cold["initMs"])
            memory.append(float(cold["maxMemoryMB"]))  # Lambda's MB are MiB (1,024 of them make its GB)
        for _ in range(warm):
            found = self.invoke(name)
            durations.append(found["durationMs"])
            memory.append(float(found["maxMemoryMB"]))
        return {"initMs": init, "durationsMs": durations, "memoryMiB": memory}

    def cleanup(self) -> None:
        for name in self.functions:
            try:
                self.call("lambda", "delete-function", "--function-name", name)
            except Exception as error:  # noqa: BLE001 - delete what can be deleted, say what could not
                print(f"could not delete {name}: {error}", file=sys.stderr)
        for name, version in self.layers:
            try:
                self.call("lambda", "delete-layer-version", "--layer-name", name, "--version-number", str(version))
            except Exception as error:  # noqa: BLE001
                print(f"could not delete {name}:{version}: {error}", file=sys.stderr)


def code_archive(code: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for name in CODE_FILES:
            out.write(code / name, name)
    return target


# --- runs -------------------------------------------------------------------------------------------------------------


def run_rie(args, scratch: Path) -> dict:
    images = json.loads((ROOT / "lambda" / "rie-images.json").read_text())["images"]
    architecture = LAMBDA_ARCH[platform.machine().lower()]
    opt = unpack(args.layers / f"impact-ai-python-{architecture}.zip", scratch / "opt")
    rie = Rie(images, opt, function_code(scratch / "code"))
    cells = []
    for python in args.pythons:
        for memory in args.memory:
            cell = {"python": python, "architecture": architecture, "memoryMB": memory}
            for side in SIDES:
                starts = [
                    rie.cold_start(python=python, memory=memory, layer=side == "layer", warm=args.warm)
                    for _ in range(args.cold_starts)
                ]
                cell[side] = {
                    "initMs": [s["initMs"] for s in starts],
                    "durationsMs": [d for s in starts for d in s["durationsMs"]],
                    "memoryMiB": [s["memoryMiB"] for s in starts],
                }
            cells.append(cell)
            print(f"python {python} {architecture} {memory} MB: {compare(cell)['added']}", file=sys.stderr)
    return {"cells": cells}


def run_aws(args, scratch: Path) -> dict:
    aws = Aws(region=args.region, role_arn=args.role_arn, prefix=args.prefix)
    code_zip = code_archive(function_code(scratch / "code"), scratch / "code.zip")
    cells = []
    try:
        for architecture in args.architectures:
            layer_arn = aws.publish_layer(args.layers / f"impact-ai-python-{architecture}.zip", architecture)
            for python in args.pythons:
                for memory in args.memory:
                    cell = {"python": python, "architecture": architecture, "memoryMB": memory}
                    for side in SIDES:
                        name = f"{args.prefix}-{python.replace('.', '')}-{architecture}-{memory}-{side}"
                        layer = layer_arn if side == "layer" else None
                        aws.create(
                            name,
                            python=python,
                            architecture=architecture,
                            memory=memory,
                            code_zip=code_zip,
                            layer=layer,
                        )
                        cell[side] = aws.side(name, layer=side == "layer", cold_starts=args.cold_starts, warm=args.warm)
                    cells.append(cell)
                    print(f"python {python} {architecture} {memory} MB: {compare(cell)['added']}", file=sys.stderr)
    finally:
        aws.cleanup()
    return {"cells": cells}


def write(out: Path, samples: dict, *, enforce: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    (out / "samples.json").write_text(json.dumps(samples, indent=2) + "\n")
    summary = summarize(samples)
    (out / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "LAMBDA-TIMINGS.md").write_text(render(summary))
    print(render(summary))
    return gate(summary, enforce=enforce)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("rie", "aws"):
        sub = modes.add_parser(mode)
        sub.add_argument("--layers", type=Path, default=ROOT / "build" / "lambda")
        sub.add_argument("--out", type=Path, default=ROOT / "build" / "lambda-timings")
        sub.add_argument("--pythons", type=lambda v: v.split(","), default=list(PYTHONS))
        sub.add_argument("--memory", type=lambda v: [int(x) for x in v.split(",")], default=list(MEMORY_SIZES))
        sub.add_argument("--cold-starts", type=int, default=30)
        sub.add_argument("--warm", type=int, default=20)
        sub.add_argument("--version", default=os.environ.get("VERSION", "0.0.0-dev"))
        sub.add_argument("--enforce", action="store_true", help="fail when a cell is over the budget")
        if mode == "aws":
            sub.add_argument("--region", required=True)
            sub.add_argument("--role-arn", required=True, help="the execution role of the functions it creates")
            sub.add_argument("--architectures", type=lambda v: v.split(","), default=["x86_64", "arm64"])
            sub.add_argument("--prefix", default=f"impact-ai-timings-{uuid.uuid4().hex[:6]}")
    report = modes.add_parser("report", help="render the report again from samples.json")
    report.add_argument("directory", type=Path)
    report.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "report":
        samples = json.loads((args.directory / "samples.json").read_text())
        return write(args.directory, samples, enforce=args.enforce)
    with tempfile.TemporaryDirectory() as scratch:
        measured = (run_rie if args.mode == "rie" else run_aws)(args, Path(scratch))
    samples = {
        "schema": SCHEMA,
        "mode": args.mode,
        "version": args.version,
        "method": {
            "coldStarts": args.cold_starts,
            "warm": args.warm,
            "memoryMB": args.memory,
            "export": f"https://127.0.0.1:{OTLP_PORT} in the function (TLS with the image's CA bundle; no network)",
        },
        **measured,
    }
    return write(args.out, samples, enforce=args.enforce)


if __name__ == "__main__":
    raise SystemExit(main())
