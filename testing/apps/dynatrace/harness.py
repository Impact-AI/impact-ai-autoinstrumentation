#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Run an app that exports OTLP to a Dynatrace environment, the way Dynatrace documents it, against the endpoint mock.

Each cell is one language and one open-source instrumentation (the OpenTelemetry GenAI instrumentation, OpenInference
or OpenLLMetry), configured as Dynatrace's own guides configure it. The harness starts the endpoint mock (tenant.py)
and the canonical model provider (provider.py), runs the cell's app with the environment the guide names, and returns
what the mock received. `record` writes it as a recording (testing/mock/recording.py's format), which the tests and the
fixtures of the copy route are made from.

    python3 harness.py record python-otel-genai --python build/dynatrace/python-otel-genai/bin/python \\
        --out testing/fixtures/sources/dynatrace

Only the direct run is here: the app sends to the endpoint itself. Standard library only; the app's interpreter or
Node.js comes from the caller.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
# Planted, fictitious tokens in the two documented schemes. Short on purpose: nothing about them looks like a real one.
CLASSIC_TOKEN = "dt0c01.PLANTED"  # noqa: S105 - planted
PLATFORM_TOKEN = "dt0s16.PLANTED"  # noqa: S105 - planted
ENVIRONMENT_ID = "abc12345"
TRACES_PATHS = ("/api/v2/otlp/v1/traces", f"/e/{ENVIRONMENT_ID}/api/v2/otlp/v1/traces")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"dynatrace_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tenant = _load("tenant", HERE / "tenant.py")
provider = _load("provider", HERE / "provider.py")
recording = _load("recording", ROOT / "testing" / "mock" / "recording.py")


@dataclass(frozen=True)
class Cell:
    """One app, its instrumentation and how its guide tells it where Dynatrace is."""

    name: str
    language: str  # python or nodejs
    app: str  # relative to this folder
    requirements: str  # the locked requirements (python) or the package folder (nodejs), relative to this folder
    scheme: str  # Api-Token or Bearer
    address: str  # saas or activegate
    how: str  # the configuration, in a sentence
    env: dict = field(default_factory=dict)
    pins: tuple = ()  # the packages whose versions decide what it sends, as the requirements name them

    @property
    def token(self) -> str:
        return CLASSIC_TOKEN if self.scheme == "Api-Token" else PLATFORM_TOKEN

    def environment(self, tenant_url: str, provider_url: str) -> dict:
        base = tenant_url if self.address == "saas" else f"{tenant_url}/e/{ENVIRONMENT_ID}"
        values = {
            "{base}": base,
            "{otlp}": f"{base}/api/v2/otlp",
            "{traces}": f"{base}/api/v2/otlp/v1/traces",
            "{token}": self.token,
            "{scheme}": self.scheme,
        }
        env = {"OPENAI_BASE_URL": f"{provider_url}/v1", "OPENAI_API_KEY": "planted-openai-key"}
        for key, value in self.env.items():
            for placeholder, text in values.items():
                value = value.replace(placeholder, text)
            env[key] = value
        return env


CELLS = {
    cell.name: cell
    for cell in (
        Cell(
            "python-otel-genai",
            "python",
            "python/canonical_otel_genai.py",
            "python/requirements-otel-genai.txt",
            "Api-Token",
            "saas",
            "the OpenTelemetry SDK's environment variables (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS), "
            "a Classic access token",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "{otlp}",
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization={scheme}%20{token}",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_SERVICE_NAME": "support-assistant",
            },
            (
                "opentelemetry-sdk",
                "opentelemetry-exporter-otlp-proto-http",
                "opentelemetry-util-genai",
                "opentelemetry-instrumentation-genai-openai",
            ),
        ),
        Cell(
            "python-openinference",
            "python",
            "python/canonical_openinference.py",
            "python/requirements-openinference.txt",
            "Bearer",
            "saas",
            "the exporter of Dynatrace's OpenInference guide: OTEL_EXPORTER_OTLP_ENDPOINT read in code and an "
            "Authorization header made from DT_API_TOKEN, a platform token",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "{traces}",
                "DT_API_TOKEN": "{token}",
                "DT_TOKEN_SCHEME": "{scheme}",
                "OTEL_SERVICE_NAME": "support-assistant",
            },
            (
                "opentelemetry-sdk",
                "opentelemetry-exporter-otlp-proto-http",
                "openinference-instrumentation",
                "openinference-instrumentation-openai",
            ),
        ),
        Cell(
            "python-openllmetry",
            "python",
            "python/canonical_openllmetry.py",
            "python/requirements-openllmetry.txt",
            "Api-Token",
            "activegate",
            "Traceloop.init(api_endpoint=..., headers=...) in code, as Dynatrace's OpenLLMetry guide shows, to an "
            "Environment ActiveGate's address, a Classic access token",
            {"DT_ENDPOINT": "{base}", "DT_API_TOKEN": "{token}", "DT_TOKEN_SCHEME": "{scheme}"},
            (
                "opentelemetry-sdk",
                "opentelemetry-exporter-otlp-proto-http",
                "traceloop-sdk",
                "opentelemetry-instrumentation-openai",
                "opentelemetry-instrumentation-qdrant",
            ),
        ),
        Cell(
            "nodejs-otel-genai",
            "nodejs",
            "nodejs/otel-genai/app.cjs",
            "nodejs/otel-genai",
            "Api-Token",
            "saas",
            "the OpenTelemetry SDK's environment variables (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS), "
            "a Classic access token",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "{otlp}",
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization={scheme}%20{token}",
                "OTEL_SERVICE_NAME": "support-assistant",
            },
            (
                "@opentelemetry/exporter-trace-otlp-proto",
                "@opentelemetry/instrumentation-openai",
                "@opentelemetry/sdk-trace",
                "openai",
            ),
        ),
        Cell(
            "nodejs-openinference",
            "nodejs",
            "nodejs/openinference/app.cjs",
            "nodejs/openinference",
            "Bearer",
            "activegate",
            "the exporter of Dynatrace's OpenInference guide: OTEL_EXPORTER_OTLP_ENDPOINT read in code and an "
            "Authorization header made from DT_API_TOKEN, a platform token, to an Environment ActiveGate's address",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "{traces}",
                "DT_API_TOKEN": "{token}",
                "DT_TOKEN_SCHEME": "{scheme}",
                "OTEL_SERVICE_NAME": "support-assistant",
            },
            (
                "@arizeai/openinference-core",
                "@arizeai/openinference-instrumentation-openai",
                "@opentelemetry/exporter-trace-otlp-proto",
                "@opentelemetry/sdk-trace",
                "openai",
            ),
        ),
    )
}


def packages_of(cell: Cell) -> dict[str, str]:
    """The versions of the packages that decide what a cell sends, from its locked requirements or package lock."""
    pinned = {}
    if cell.language == "nodejs":
        lock = json.loads((HERE / cell.requirements / "package-lock.json").read_text())["packages"]
        for name in cell.pins:
            if f"node_modules/{name}" in lock:
                pinned[name] = lock[f"node_modules/{name}"]["version"]
    else:
        for line in (HERE / cell.requirements).read_text().splitlines():
            name, equals, version = line.strip().partition("==")
            if equals and name in cell.pins:
                pinned[name] = version.split(" ")[0].split(";")[0].strip()
    missing = set(cell.pins) - set(pinned)
    if missing:
        raise ValueError(f"{cell.name}: {sorted(missing)} not pinned in {cell.requirements}")
    return dict(sorted(pinned.items()))


def run(cell: Cell, interpreter: str, timeout: float = 180) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """Run the cell against fresh mocks; the app's result and every request the endpoint mock received."""
    endpoint = tenant.server("127.0.0.1", 0, [CLASSIC_TOKEN, PLATFORM_TOKEN])
    model = provider.server("127.0.0.1", 0)
    for server in (endpoint, model):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "TRACELOOP_", "DT_"))}
        env.update(
            cell.environment(
                f"http://127.0.0.1:{endpoint.server_address[1]}", f"http://127.0.0.1:{model.server_address[1]}"
            )
        )
        result = subprocess.run(  # noqa: S603 - the cell's own app, with the interpreter the caller names
            [os.path.abspath(interpreter) if os.sep in interpreter else interpreter, str(HERE / cell.app)],
            env=env,
            cwd=(HERE / cell.app).parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        with tenant.state_lock:
            captured = [dict(record) for record in tenant.requests_seen]
        return result, captured
    finally:
        endpoint.shutdown()
        model.shutdown()


def record(cell: Cell, captured: list[dict], recorded: str) -> dict:
    """The capture as a recording of the trace requests, saying what ran against what."""
    how = (
        f"testing/apps/dynatrace/{cell.app}, {cell.language}, configured by {cell.how}, run against "
        "testing/apps/dynatrace/tenant.py (a mock of the OTLP trace endpoint Dynatrace documents, "
        "https://docs.dynatrace.com/docs/dynatrace-api/environment-api/opentelemetry/post-traces) with the "
        "canonical provider testing/apps/dynatrace/provider.py"
    )
    return recording.recording(captured, how, packages_of(cell), recorded, list(TRACES_PATHS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="run a cell and write its recording")
    rec.add_argument("cell", choices=sorted(CELLS))
    rec.add_argument("--python", default=sys.executable, help="the interpreter with the cell's requirements")
    rec.add_argument("--node", default="node", help="Node.js, for a Node.js cell")
    rec.add_argument("--out", type=Path, required=True, help="the folder of the recordings")
    rec.add_argument("--recorded", default=datetime.date.today().isoformat())
    sub.add_parser("cells", help="list the cells as JSON")
    args = parser.parse_args(argv)
    if args.command == "cells":
        json.dump({name: cell.__dict__ for name, cell in CELLS.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    cell = CELLS[args.cell]
    result, captured = run(cell, args.python if cell.language == "python" else args.node)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        return result.returncode
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{cell.name}.recording.json"
    target.write_text(json.dumps(record(cell, captured, args.recorded), indent=2) + "\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
