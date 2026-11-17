#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The Datadog harness: each language's tracer against the real Datadog Agent, with a placeholder key and no account.

    CONTAINER=docker python3 testing/apps/datadog/cells.py --out build/datadog [--cells python,nodejs,java]

For each cell the pinned tracer runs the canonical interaction (and, for Python and Node.js, one call to each of
OpenAI, Anthropic and Amazon Bedrock) with LLM Observability on (agent mode), with APM tracing alone, and with APM
tracing off (LLM Observability through the Agent's EVP proxy). A recording tap (agent_tap.py) in front of the Agent
writes what the Agent received as recordings, <cell>-<mode>-<app>.recording.json, which the relay's decoder reads
(`go test ./internal/relay/source -run TestDatadogHarness` with DATADOG_HARNESS_RECORDINGS set to --out).

It checks that the Agent starts with a placeholder key, answers every trace intake request 200, and forwards: traces
over HTTP to its configured URL, and LLM Observability over HTTPS to llmobs-intake.<site>, both carrying the Agent's
own key, all to backend.py, which records them. Nothing reaches Datadog: the site is harness.invalid. Standard library
only; needs docker (or podman) and openssl.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "testing" / "mock"))
from recording import recording  # noqa: E402

CONTAINER = os.environ.get("CONTAINER", "docker")
AGENT_IMAGE = "docker.io/datadog/agent:7.84.3@sha256:e0b45d27e7e08abc66097b2c4ce04af02487d6060bbf453268c2825410164b81"
PLACEHOLDER_KEY = "0123456789abcdef0123456789abcdef"
SITE = "harness.invalid"
TRACE_PATHS = ["/v0.3/traces", "/v0.4/traces", "/v0.5/traces", "/v0.7/traces", "/v1.0/traces"]
# dd-trace and ddtrace send LLM Observability through EVP v2, dd-java-agent through v4.
LLMOBS_PATHS = ["/evp_proxy/v2/api/v2/llmobs", "/evp_proxy/v4/api/v2/llmobs"]

PACKAGES = {
    "python": {"ddtrace": "4.16.3", "openai": "2.8.1", "anthropic": "0.75.0", "boto3": "1.41.5"},
    "nodejs": {
        "dd-trace": "6.24.0",
        "openai": "7.53.0",
        "@anthropic-ai/sdk": "0.75.0",
        "@aws-sdk/client-bedrock-runtime": "3.1164.0",
    },
    "java": {"dd-java-agent": "1.68.1", "dd-trace-api": "1.68.1"},
}
COMMANDS = {
    ("python", "canonical"): ["python", "canonical.py"],
    ("python", "providers"): ["python", "providers.py"],
    ("nodejs", "canonical"): ["node", "canonical.cjs"],
    ("nodejs", "providers"): ["node", "providers.cjs"],
    ("java", "canonical"): ["java", "-javaagent:dd-java-agent.jar", "-cp", "dd-trace-api.jar", "Canonical.java"],
}
# The Java cell annotates its calls with LLM Observability's API: with it off, nothing is AI, so APM alone is not run.
MODES = {"python": ["llmobs", "apm", "evp"], "nodejs": ["llmobs", "apm", "evp"], "java": ["llmobs", "evp"]}
MODE_ENV = {
    "llmobs": {
        "DD_LLMOBS_ENABLED": "true",
        "DD_LLMOBS_ML_APP": "support-assistant",
        "DD_LLMOBS_AGENTLESS_ENABLED": "false",
    },
    "apm": {},
    "evp": {
        "DD_LLMOBS_ENABLED": "true",
        "DD_LLMOBS_ML_APP": "support-assistant",
        "DD_LLMOBS_AGENTLESS_ENABLED": "false",
        "DD_APM_TRACING_ENABLED": "false",
    },
}


def run(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(  # noqa: S603 - the container CLI with harness-controlled arguments
        [CONTAINER, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{CONTAINER} {' '.join(args[:4])} ... failed ({result.returncode}):\n{result.stderr[-4000:]}"
        )
    return result


def get_json(url: str) -> list | dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - a local test double
        return json.load(response)


def post(url: str) -> None:
    urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=10).close()  # noqa: S310


def published(name: str, port: int) -> str:
    """The host address of a container's published port."""
    line = run("port", name, f"{port}/tcp").stdout.strip().splitlines()[0]
    host, _, mapped = line.rpartition(":")
    return f"http://127.0.0.1:{mapped}"


def wait_for(url: str, seconds: float) -> None:
    deadline = time.time() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"{url} did not answer within {seconds:.0f} s")
        time.sleep(1)


class Harness:
    def __init__(self, out: Path):
        self.out = out
        self.tag = uuid.uuid4().hex[:6]
        self.network = f"datadog-harness-{self.tag}"
        self.containers: list[str] = []
        self.problems: list[str] = []

    def image(self, target: str) -> str:
        return f"localhost/impact-instrumentation/datadog-{target}:dev"

    def build(self, targets: list[str]) -> None:
        for target in targets:
            run(
                "build",
                "-f",
                str(HERE / "Dockerfile"),
                "--target",
                target,
                "-t",
                self.image(target),
                str(HERE),
                timeout=900,
            )

    def start(
        self,
        name: str,
        image: str,
        *args: str,
        env: dict[str, str] | None = None,
        publish: int | None = None,
        mounts: list[str] | None = None,
    ) -> str:
        full = f"{name}-{self.tag}"
        command = ["run", "-d", "--name", full, "--network", self.network, "--network-alias", name]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        for mount in mounts or []:
            command += ["-v", mount]
        if publish:
            command += ["-p", f"127.0.0.1::{publish}"]
        run(*command, image, *args)
        self.containers.append(full)
        return full

    def up(self, certs: Path) -> None:
        run("network", "create", self.network)
        tools = self.image("tools")
        self.backend = self.start(
            "backend",
            tools,
            "python",
            "backend.py",
            "--port",
            "8080",
            "--cert",
            "/certs/cert.pem",
            "--key",
            "/certs/key.pem",
            publish=8080,
            mounts=[f"{certs}:/certs:ro"],
        )
        # The provider listens on every interface of its own container, inside the harness's network.
        self.start("provider", tools, "python", "canonical_provider.py", "--host", "0.0.0.0", "--port", "8091")  # noqa: S104
        self.start(
            "mock",
            tools,
            "python",
            "/mock/server.py",
            "--port",
            "8090",
            mounts=[f"{ROOT / 'testing' / 'mock'}:/mock:ro"],
        )
        self.start(
            "agent",
            AGENT_IMAGE,
            env={
                "DD_API_KEY": PLACEHOLDER_KEY,
                "DD_HOSTNAME": "harness-node",
                "DD_SITE": SITE,
                "DD_DD_URL": "http://backend:8080",
                "DD_APM_DD_URL": "http://backend:8080",
                "DD_PROXY_HTTPS": "http://backend:8080",
                "DD_SKIP_SSL_VALIDATION": "true",
                "DD_APM_ENABLED": "true",
                "DD_APM_NON_LOCAL_TRAFFIC": "true",
                "DD_REMOTE_CONFIGURATION_ENABLED": "false",
                "DD_LOGS_ENABLED": "false",
                "DD_PROCESS_AGENT_ENABLED": "false",
                "DD_INVENTORIES_CONFIGURATION_ENABLED": "false",
            },
        )
        self.tap = self.start(
            "tap", tools, "python", "agent_tap.py", "--port", "8126", "--upstream", "agent:8126", publish=8126
        )
        self.tap_url, self.backend_url = published(self.tap, 8126), published(self.backend, 8080)
        wait_for(f"{self.tap_url}/info", 180)

    def down(self) -> None:
        for name in reversed(self.containers):
            run("rm", "-f", name, check=False)
        run("network", "rm", "-f", self.network, check=False)

    def cell(self, language: str, mode: str, app: str) -> None:
        name = f"{language}-{mode}-{app}"
        post(f"{self.tap_url}/_tap/reset")
        env = {
            "DD_TRACE_AGENT_URL": "http://tap:8126",
            "DD_SERVICE": "support-assistant",
            "DD_VERSION": "4.2.0",
            "DD_ENV": "harness",
            "DD_TRACE_GIT_METADATA_ENABLED": "false",
            "DD_RUNTIME_METRICS_ENABLED": "false",
            "OPENAI_BASE_URL": "http://provider:8091/openai/v1",
            "PROVIDER_URL": "http://mock:8090",
            **MODE_ENV[mode],
        }
        command = ["run", "--rm", "--network", self.network]
        for key, value in env.items():
            command += ["-e", f"{key}={value}"]
        result = run(*command, self.image(language), *COMMANDS[(language, app)], check=False, timeout=300)
        if result.returncode != 0:
            self.problems.append(f"{name}: the workload failed ({result.returncode}): {result.stderr[-1500:]}")
            return
        time.sleep(2)
        captured = get_json(f"{self.tap_url}/_tap/requests")
        (self.out / f"{name}.capture.json").write_text(json.dumps(captured, indent=1))
        spans = [r for r in captured if r["path"] in TRACE_PATHS + LLMOBS_PATHS]
        if not spans:
            self.problems.append(f"{name}: the tracer sent the Agent no spans")
        for request in spans:
            status = (request.get("response") or {}).get("status")
            if status not in (200, 202):
                self.problems.append(f"{name}: the Agent answered {request['method']} {request['path']} with {status}")
        how = (
            f"{' '.join(f'{p} {v}' for p, v in PACKAGES[language].items())} ran testing/apps/datadog/"
            f"{COMMANDS[(language, app)][-1]} ({mode}) in the Datadog harness (testing/apps/datadog/cells.py); its "
            "requests to the Datadog Agent 7.84.3 (placeholder key) were captured by agent_tap.py."
        )
        written = recording(captured, how, PACKAGES[language], time.strftime("%Y-%m-%d"), TRACE_PATHS + LLMOBS_PATHS)
        if written["requests"]:
            (self.out / f"{name}.recording.json").write_text(json.dumps(written, indent=2) + "\n")

    def forwarded(self, evp: bool) -> None:
        """The Agent forwarded, with its own key, what its tracers sent: traces over HTTP, LLM events over HTTPS."""
        deadline = time.time() + 60
        while True:
            seen = get_json(f"{self.backend_url}/_requests")
            traces = [r for r in seen if r["tunnel"] is None and r["path"] == "/api/v0.2/traces"]
            events = [
                r
                for r in seen
                if (r["tunnel"] or "").startswith(f"llmobs-intake.{SITE}") and r["path"] == "/api/v2/llmobs"
            ]
            if traces and (events or not evp):
                break
            if time.time() > deadline:
                self.problems.append(
                    f"the Agent forwarded {len(traces)} trace payloads and {len(events)} LLM "
                    "Observability payloads within 60 s"
                )
                return
            time.sleep(2)
        for request in traces + events:
            key = {k.lower(): v for k, v in request["headers"].items()}.get("dd-api-key")
            if key != PLACEHOLDER_KEY:
                self.problems.append(f"{request['path']} reached the backend without the Agent's own key")
        (self.out / "backend.json").write_text(json.dumps(seen, indent=1))


def certificate(directory: Path) -> None:
    subprocess.run(  # noqa: S603 - openssl from the PATH, with fixed arguments
        [  # noqa: S607
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={SITE}",
            "-addext",
            f"subjectAltName=DNS:*.{SITE},DNS:{SITE}",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    for name in ("key.pem", "cert.pem"):
        (directory / name).chmod(0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cells", default="python,nodejs,java")
    args = parser.parse_args()
    cells = [c for c in args.cells.split(",") if c]
    args.out.mkdir(parents=True, exist_ok=True)
    harness = Harness(args.out)
    harness.build(["tools", *cells])
    run("pull", AGENT_IMAGE, timeout=900)
    with tempfile.TemporaryDirectory() as certs:
        certificate(Path(certs))
        try:
            harness.up(Path(certs))
            for language in cells:
                for mode in MODES[language]:
                    for app in ("canonical", "providers"):
                        if (language, app) in COMMANDS:
                            harness.cell(language, mode, app)
            harness.forwarded(evp=True)
        finally:
            logs = run("logs", f"agent-{harness.tag}", check=False)
            (args.out / "agent.log").write_text(logs.stdout + logs.stderr)
            harness.down()
    for problem in harness.problems:
        print(f"datadog harness: {problem}", file=sys.stderr)
    print(f"datadog harness: {len(list(args.out.glob('*.recording.json')))} recordings in {args.out}")
    return 1 if harness.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
