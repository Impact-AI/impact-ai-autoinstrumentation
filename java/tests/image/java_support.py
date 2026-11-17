# SPDX-License-Identifier: Apache-2.0

"""What the Java image tests share: names, the Operator's injection and the questions the tests ask of the mock.

The tests reproduce the OpenTelemetry Operator's Java injection without Kubernetes. The Operator adds an init container
running our image (`cp /javaagent.jar /otel-auto-instrumentation-java/javaagent.jar` into an emptyDir), mounts that
volume into the application container at /otel-auto-instrumentation-java-<container> and appends
` -javaagent:/otel-auto-instrumentation-java-<container>/javaagent.jar` to JAVA_TOOL_OPTIONS. The fixtures do the same
with the container CLI, so the tests exercise the real image and real JVMs.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "tests" / "image"))

from containers import Container, Mock, http, image_exists, run, wait_until  # noqa: E402

IMAGE = os.environ.get("IMPACT_JAVA_IMAGE", "localhost/impact-instrumentation/java:dev")
APP_PREFIX = os.environ.get("FIXTURE_JAVA_PREFIX", "localhost/impact-instrumentation/fixture-java")
MOCK_IMAGE = os.environ.get("MOCK_IMAGE", "localhost/impact-instrumentation/mock:dev")
INIT_MOUNT = "/otel-auto-instrumentation-java"
MOUNT = f"{INIT_MOUNT}-app"
AGENT = f"{MOUNT}/javaagent.jar"
JAVA_TOOL_OPTIONS = f" -javaagent:{AGENT}"
DOCKERFILE = ROOT / "java" / "Dockerfile"
SCOPE = ROOT / "java" / "scope.txt"

# The Java feature releases the image is built for (its JAVA_VERSIONS) and one it is not.
JAVAS = ["17", "21", "25"]
OUTSIDE = "11"

# What the Operator injects when the Instrumentation is the chart's, as for Python and Node.js.
OPERATOR_ENV = {
    "OTEL_RESOURCE_ATTRIBUTES": (
        "k8s.container.name=app,k8s.deployment.name=support-assistant,k8s.namespace.name=shop,"
        "k8s.node.name=node-1,k8s.pod.name=support-assistant-6d9c-x2,k8s.replicaset.name=support-assistant-6d9c,"
        "service.instance.id=shop.support-assistant-6d9c-x2.app,service.namespace=shop,service.version=1.0"
    ),
    # Fast export for tests; the default (5 s) is the agent's own.
    "OTEL_BSP_SCHEDULE_DELAY": "300",
    "OTEL_BLRP_SCHEDULE_DELAY": "300",
}


def app_image(java: str) -> str:
    return f"{APP_PREFIX}:{java}"


def require_images(*names: str) -> None:
    import pytest

    missing = [n for n in names if not image_exists(n)]
    if missing:
        pytest.fail(f"missing images {missing}: run `make images-java` first", pytrace=False)


def wait_ready(app: Container, timeout: float = 120) -> None:
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200,
        timeout=timeout,
        message=lambda: f"app to start\n{app.logs()}",
    )


def setup_reports(mock: Mock) -> list[dict]:
    """The `impact.setup` spans: OTLP JSON, which is how the launcher sends them."""
    return [s for s in mock.spans() if s["name"] == "impact.setup"]


def only(items: list[dict], what: str) -> dict:
    assert len(items) == 1, (
        f"expected one {what}, got {len(items)}: {json.dumps([i.get('attrs') for i in items])[:2000]}"
    )
    return items[0]


def agent_spans(mock: Mock) -> list[dict]:
    """Spans the agent exported (OTLP/HTTP protobuf, identity or gzip): the JSON setup reports are left out."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from containers import anyvalue

    out = []
    for record in mock.requests("/v1/traces"):
        if record.get("json"):
            continue
        raw = Mock.body(record)
        if record["headers"].get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        request = ExportTraceServiceRequest()
        request.ParseFromString(raw)
        for resource_spans in request.resource_spans:
            resource = {a.key: anyvalue(a.value) for a in resource_spans.resource.attributes}
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    out.append(
                        {
                            "name": span.name,
                            "kind": span.kind,
                            "scope": scope_spans.scope.name,
                            "attrs": {a.key: anyvalue(a.value) for a in span.attributes},
                            "resource": resource,
                        }
                    )
    return out


def paths(mock: Mock) -> list[str]:
    return [r["path"] for r in mock.requests()]


def pinned_agent_sha256() -> str:
    """The upstream agent's SHA-256 as java/Dockerfile pins it (ADD --checksum), checked equal to AGENT_SHA256."""
    text = DOCKERFILE.read_text()
    added = re.search(r"ADD --checksum=sha256:([0-9a-f]{64}) \S+/opentelemetry-javaagent-[0-9.]+\.jar ", text).group(1)
    argument = re.search(r"^ARG AGENT_SHA256=([0-9a-f]{64})$", text, re.M).group(1)
    assert added == argument, "java/Dockerfile: ADD --checksum and AGENT_SHA256 name different agents"
    return added


def modules(logs: str) -> dict[str, bool]:
    """Every instrumentation module the agent loaded, by class (without its package prefix), and whether it is on: what
    `-Dotel.javaagent.debug=true` logs. A module is named in the log by its first name only, which many modules share
    (every AWS SDK one is `aws-sdk`), so its class tells them apart; "is disabled" follows the line that loads it."""
    out: dict[str, bool] = {}
    last = None
    for line in logs.splitlines():
        loaded = re.search(
            r"Loading instrumentation \S+ \[class io\.opentelemetry\.javaagent\.instrumentation\.(\S+)\]",
            line,
        )
        if loaded:
            last = loaded.group(1)
            out[last] = True
        elif last and re.search(r"Instrumentation \S+ is disabled", line):
            out[last] = False
    return out


def scope() -> list[str]:
    return [line.split("#")[0].strip() for line in SCOPE.read_text().splitlines() if line.split("#")[0].strip()]


def payload_files(volume: str) -> list[str]:
    """What is in the Operator's volume: javaagent.jar, and the unpacked agent once it started."""
    out = run("run", "--rm", "--entrypoint", "ls", "-v", f"{volume}:/x", IMAGE, "-a", "/x").stdout
    return [name for name in out.split() if name not in (".", "..")]
