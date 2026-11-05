# SPDX-License-Identifier: Apache-2.0

"""What the Node.js image tests share: names, the Operator's environment and the questions the tests ask of the mock.

The image tests reproduce the OpenTelemetry Operator's Node.js injection without Kubernetes. The Operator adds an init
container running our image (`cp -r /autoinstrumentation/. /otel-auto-instrumentation-nodejs` into an emptyDir), mounts
that volume into the application container, and sets NODE_OPTIONS=--require
/otel-auto-instrumentation-nodejs/autoinstrumentation.js and the OTEL_* variables. The fixtures do the same with the
container CLI, so the tests exercise the real image and the real application images.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "tests" / "image"))

from containers import Container, Mock, http, image_exists, run, wait_until  # noqa: E402

IMAGE = os.environ.get("IMPACT_NODEJS_IMAGE", "localhost/impact-instrumentation/nodejs:dev")
APP_PREFIX = os.environ.get("FIXTURE_NODE_PREFIX", "localhost/impact-instrumentation/fixture-node")
MOCK_IMAGE = os.environ.get("MOCK_IMAGE", "localhost/impact-instrumentation/mock:dev")
MOUNT = "/otel-auto-instrumentation-nodejs"
PRELOAD = f"{MOUNT}/autoinstrumentation.js"
QUALIFICATION = ROOT / "nodejs" / "qualification.json"

# Node majors of the matrix and of the fixture images; the majors below the floor (Node 22.15), which only the "never
# throws" test runs on, are pulled by digest. Node 20 reached its end of life on 2026-04-30.
NODES = ["22", "24", "26"]
OLD_NODES = {
    "16": "docker.io/library/node:16-slim@sha256:3ebf2875c188d22939c6ab080cfb1a4a6248cc86bae600ea8e2326aa03acdb8f",
    "18": "docker.io/library/node:18-slim@sha256:f9ab18e354e6855ae56ef2b290dd225c1e51a564f87584b9bd21dd651838830e",
    "20": "docker.io/library/node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0",
}

# What the Operator injects for Node.js when the Instrumentation is the chart's: NODE_OPTIONS, the resource attributes,
# the pod and node addresses and IMPACT_OTLP_ENDPOINT.
OPERATOR_ENV = {
    "OTEL_RESOURCE_ATTRIBUTES": (
        "k8s.container.name=app,k8s.deployment.name=support-assistant,k8s.namespace.name=shop,"
        "k8s.node.name=node-1,k8s.pod.name=support-assistant-6d9c-x2,k8s.replicaset.name=support-assistant-6d9c,"
        "service.instance.id=shop.support-assistant-6d9c-x2.app,service.namespace=shop,service.version=1.0"
    ),
    "OTEL_RESOURCE_ATTRIBUTES_POD_NAME": "support-assistant-6d9c-x2",
    "OTEL_RESOURCE_ATTRIBUTES_NODE_NAME": "node-1",
    "OTEL_POD_IP": "10.244.0.18",
    "OTEL_NODE_IP": "10.89.2.2",
    # Fast export for tests; the default (5 s) is the SDK's own.
    "OTEL_BSP_SCHEDULE_DELAY": "300",
}

PROMPT, ANSWER = "hi there", "Hello from the stub."
# A model call, as each route reports it: the span's name and the attributes that hold the model, the tokens and the
# content.
OPENAI_MODEL, ANTHROPIC_MODEL = "gpt-4o-mini", "claude-stub"


def app_image(node: str) -> str:
    return f"{APP_PREFIX}:{node}"


def require_images(*names: str) -> None:
    import pytest

    missing = [n for n in names if not image_exists(n)]
    if missing:
        pytest.fail(f"missing images {missing}: run `make test-nodejs` (or `make images`) first", pytrace=False)


def ensure_image(reference: str) -> str:
    """A public image by digest: pulled when it is not there yet."""
    if not image_exists(reference):
        run("pull", "-q", reference, timeout=900)
    return reference


def wait_ready(app: Container, timeout: float = 90) -> None:
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200,
        timeout=timeout,
        message=lambda: f"app to start\n{app.logs()}",
    )


def is_llm_span(span: dict) -> bool:
    return span["name"] != "impact.setup" and any(key.startswith(("gen_ai.", "llm.")) for key in span["attrs"])


def llm_spans(mock: Mock) -> list[dict]:
    return [s for s in mock.spans() if is_llm_span(s)]


def setup_reports(mock: Mock) -> list[dict]:
    return [s for s in mock.spans() if s["name"] == "impact.setup"]


def only(items: list[dict], what: str) -> dict:
    """The one `what` the test expects. An unpacking that finds more says only how many; this says which, so that a late
    arrival from another run shows as one (a different span, or a different pod and time) and not as a bare count."""
    assert len(items) == 1, f"expected one {what}, got {len(items)}: " + json.dumps(
        [
            {
                "name": s.get("name"),
                "spanId": s.get("spanId"),
                "start": s.get("startTimeUnixNano"),
                "pod": s.get("resource", {}).get("k8s.pod.name"),
                "state": s.get("attrs", {}).get("impact.setup.state"),
                "detail": str(s.get("attrs", {}).get("impact.setup.detail", ""))[:80],
            }
            for s in items
        ]
    )
    return items[0]


def text_of(span: dict) -> str:
    return json.dumps(span["attrs"])


def has_content(span: dict) -> bool:
    """The prompt and the answer are on the span itself, as the route writes them."""
    text = text_of(span)
    return PROMPT in text and ANSWER in text


def ask(app: Container, provider: str, prompt: str = PROMPT) -> tuple[int, object]:
    return http("POST", f"{app.url}/chat/{provider}", {"prompt": prompt})


def settle(mock: Mock, expected: int, *, quiet: float = 1.2, timeout: float = 30) -> list[dict]:
    """Wait for `expected` model-call spans, then for a quiet moment in which no further one arrives; return them all.

    A duplicate of a span arrives in a later batch than the span itself, so a count taken the moment the expected
    number is in would miss it.
    """
    wait_until(
        lambda: len(llm_spans(mock)) >= expected,
        timeout=timeout,
        message=lambda: f"{expected} model-call spans, got {[s['name'] for s in mock.spans()]}",
    )
    time.sleep(quiet)
    return llm_spans(mock)


def installed_version(app_container_image: str, directory: str, package: str) -> str:
    out = run(
        "run",
        "--rm",
        "--entrypoint",
        "node",
        app_container_image,
        "-p",
        f"require('fs').readFileSync('{directory}/node_modules/{package}/package.json','utf8')",
    ).stdout
    return json.loads(out)["version"]


def run_once(
    image: str,
    command: list[str],
    *,
    network,
    payload: str | None,
    env: dict[str, str] | None = None,
    node_options: str = "",
    extra_args: list[str] | None = None,
    readonly_payload: bool = False,
    timeout: float = 60,
):
    """A short-lived Node process in a container, started the way the Operator starts an application: the
    CompletedProcess."""
    args = ["run", "--rm", "--network", network.name, "--user", "10001:10001"]
    environment = {"IMPACT_OTLP_ENDPOINT": "", **(env or {})}
    if payload:
        args += ["-v", f"{payload}:{MOUNT}{':ro' if readonly_payload else ''}"]
        environment["NODE_OPTIONS"] = f"{node_options} --require {PRELOAD}".strip()
    for key, value in environment.items():
        args += ["-e", f"{key}={value}"]
    args += [*(extra_args or []), image, *command]
    started = time.monotonic()
    result = run(*args, check=False, timeout=timeout)
    result.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    return result


def readable(path: Path) -> Path:
    """Files made on the host, readable by the user a container runs as (which is not the user that made them)."""
    for item in [path, *(path.rglob("*") if path.is_dir() else [])]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    return path
