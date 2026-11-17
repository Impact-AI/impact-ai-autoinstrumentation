# SPDX-License-Identifier: Apache-2.0

"""What the .NET proofs share: names, the Operator's .NET injection and the questions the tests ask of the mock.

The proofs reproduce the OpenTelemetry Operator's .NET injection (v0.162.0, internal/instrumentation/dotnet.go and
sdk.go) without Kubernetes. The Operator adds an init container running the payload image (`cp -r /autoinstrumentation/.
/otel-auto-instrumentation-dotnet` into an emptyDir), mounts that volume into the application container at the same
path, and sets the application's environment by fixed rules (operator_env below). The fixtures do the same with the
container CLI, so the proofs exercise the real upstream agent, the kit's startup hook and real .NET runtimes.
"""

from __future__ import annotations

import gzip
import io
import os
import platform
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "tests" / "image"))

from containers import CONTAINER, Container, Mock, http, image_exists, run, wait_until  # noqa: E402

IMAGE = os.environ.get("IMPACT_DOTNET_IMAGE", "localhost/impact-instrumentation/dotnet:dev")
APP_PREFIX = os.environ.get("FIXTURE_DOTNET_PREFIX", "localhost/impact-instrumentation/fixture-dotnet")
MOCK_IMAGE = os.environ.get("MOCK_IMAGE", "localhost/impact-instrumentation/mock:dev")
# The .NET versions the fixtures are built for (DOTNET_VERSIONS, as `make images-dotnet` builds them).
DOTNETS = os.environ.get("DOTNET_VERSIONS", "10.0").split()

# A service on .NET 8, which the kit is not proved on (testing/apps/dotnet/Dockerfile, --target older).
OLDER_IMAGE = f"{APP_PREFIX}:older-8.0"
# In the fixture image: the assistant without Microsoft.Extensions.AI, and a startup hook of the workload's own.
LITE = ["dotnet", "/lite/Lite.dll"]
OWN_HOOK = "/own-hook/Own.StartupHook.dll"

HOME = "/otel-auto-instrumentation-dotnet"  # the init container's and the application's mount
KIT_HOOK = f"{HOME}/impact/Impact.StartupHook.dll"

# The Operator's constants (dotnet.go, v0.162.0).
UPSTREAM_PROFILER = "{918728DD-259F-4A6A-AC2B-B85E1B658318}"
PROFILER_PATHS = {
    None: f"{HOME}/linux/OpenTelemetry.AutoInstrumentation.Native.so",
    "linux-x64": f"{HOME}/linux/OpenTelemetry.AutoInstrumentation.Native.so",
    "linux-musl-x64": f"{HOME}/linux-musl/OpenTelemetry.AutoInstrumentation.Native.so",
}
UPSTREAM_HOOK = f"{HOME}/net/OpenTelemetry.AutoInstrumentation.StartupHook.dll"

# The architecture of this machine, as .NET names it in runtime identifiers.
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]

# What the Operator injects from the Instrumentation besides the .NET variables, as for Python and Node.js.
RESOURCE = (
    "k8s.container.name=app,k8s.deployment.name=support-assistant,k8s.namespace.name=shop,"
    "k8s.node.name=node-1,k8s.pod.name=support-assistant-6d9c-x2,k8s.replicaset.name=support-assistant-6d9c,"
    "service.instance.id=shop.support-assistant-6d9c-x2.app,service.namespace=shop,service.version=1.0"
)

# Datadog's .NET tracer as its Linux install guide sets it up, in the fixture's /opt/datadog: an APM vendor's profiler.
DATADOG_PROFILER = "{846F5F1C-F9AE-4B07-969E-05C26BC060D8}"


def datadog_env(libc: str, agent_url: str) -> dict[str, str]:
    rid = f"linux-musl-{ARCH}" if libc == "musl" else f"linux-{ARCH}"
    return {
        "CORECLR_ENABLE_PROFILING": "1",
        "CORECLR_PROFILER": DATADOG_PROFILER,
        "CORECLR_PROFILER_PATH": f"/opt/datadog/{rid}/Datadog.Trace.ClrProfiler.Native.so",
        "DD_DOTNET_TRACER_HOME": "/opt/datadog",
        "DD_TRACE_AGENT_URL": agent_url,
        "DD_TRACE_LOG_DIRECTORY": "/tmp/datadog",  # noqa: S108 - inside the fixture container
        "DD_INSTRUMENTATION_TELEMETRY_ENABLED": "false",
        "DD_SERVICE": "support-assistant",
    }


def kit_env(endpoint: str, *, hook: bool = True) -> dict[str, str]:
    """The Instrumentation's .NET env the proofs propose for the chart: the kit's hook first in DOTNET_STARTUP_HOOKS,
    the kit's endpoint under a name of its own (as for Java), the activity source of Microsoft.Extensions.AI, and
    content as the content mode says. The upstream agent logs to the console so that the proofs can read it."""
    env = {
        "IMPACT_OTLP_ENDPOINT": endpoint,
        "OTEL_DOTNET_AUTO_TRACES_ADDITIONAL_SOURCES": "Experimental.Microsoft.Extensions.AI",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_DOTNET_AUTO_LOGGER": "console",
        "OTEL_LOG_LEVEL": "info",
        "OTEL_BSP_SCHEDULE_DELAY": "300",  # fast export for tests
    }
    if hook:
        env["DOTNET_STARTUP_HOOKS"] = KIT_HOOK
    return env


def operator_env(container: dict[str, str], spec: dict[str, str], runtime: str | None = None) -> dict[str, str] | None:
    """The application container's environment after the Operator's .NET injection, or None when it skips the container.

    - A container that sets OTEL_DOTNET_AUTO_HOME is taken as instrumented already, and an annotation other than
      linux-x64 or linux-musl-x64 is refused: the Operator then leaves the pod as it is.
    - The Instrumentation's env is added where the container does not set the variable (appendIfNotSet).
    - The CLR profiler variables and OTEL_DOTNET_AUTO_HOME are added only where the container has none: its own
      value wins. DOTNET_STARTUP_HOOKS, DOTNET_ADDITIONAL_DEPS and DOTNET_SHARED_STORE get the upstream path appended
      with ':'.
    - OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES are added as for every language.
    """
    if "OTEL_DOTNET_AUTO_HOME" in container or "OTEL_DOTNET_AUTO_HOME" in spec:
        return None
    if runtime not in PROFILER_PATHS:
        return None
    env = dict(container)
    for key, value in spec.items():
        env.setdefault(key, value)
    for key, value, concat in [
        ("CORECLR_ENABLE_PROFILING", "1", False),
        ("CORECLR_PROFILER", UPSTREAM_PROFILER, False),
        ("CORECLR_PROFILER_PATH", PROFILER_PATHS[runtime], False),
        ("DOTNET_STARTUP_HOOKS", UPSTREAM_HOOK, True),
        ("DOTNET_ADDITIONAL_DEPS", f"{HOME}/AdditionalDeps", True),
        ("OTEL_DOTNET_AUTO_HOME", HOME, False),
        ("DOTNET_SHARED_STORE", f"{HOME}/store", True),
    ]:
        if key not in env:
            env[key] = value
        elif concat:
            env[key] = f"{env[key]}:{value}"
    env.setdefault("OTEL_SERVICE_NAME", "support-assistant")
    env.setdefault("OTEL_RESOURCE_ATTRIBUTES", RESOURCE)
    return env


def app_image(dotnet: str, libc: str) -> str:
    return f"{APP_PREFIX}:{dotnet}-{libc}"


def require_images(*names: str) -> None:
    import pytest

    missing = [n for n in names if not image_exists(n)]
    if missing:
        pytest.fail(f"missing images {missing}: run `make images-dotnet` first", pytrace=False)


def wait_ready(app: Container, timeout: float = 120) -> None:
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200,
        timeout=timeout,
        message=lambda: f"app to start\n{app.logs()}",
    )


def agent_spans(mock: Mock) -> list[dict]:
    """Spans exported over OTLP/HTTP protobuf (identity or gzip), with their trace and parent."""
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
                            "trace_id": span.trace_id.hex(),
                            "span_id": span.span_id.hex(),
                            "parent_span_id": span.parent_span_id.hex(),
                            "attrs": {a.key: anyvalue(a.value) for a in span.attributes},
                            "events": [e.name for e in span.events],
                            "resource": resource,
                        }
                    )
    return out


def chat_spans(mock: Mock) -> list[dict]:
    """The spans of Microsoft.Extensions.AI's UseOpenTelemetry(): one per model call."""
    return [s for s in agent_spans(mock) if s["attrs"].get("gen_ai.operation.name") == "chat"]


def server_span(mock: Mock, path: str) -> dict | None:
    spans = [s for s in agent_spans(mock) if s["kind"] == 2 and s["attrs"].get("url.path") == path]
    return spans[0] if spans else None


def paths(mock: Mock) -> list[str]:
    return [r["path"] for r in mock.requests()]


def image_files(image: str, platform_: str | None = None) -> dict[str, tarfile.TarInfo | bytes]:
    """The files of an image, read without running it (so an arm64 image is read on an amd64 machine): path ->
    the file's bytes, or its TarInfo for a link or a directory."""
    name = f"i019-files-{uuid.uuid4().hex[:6]}"
    args = ["create", "--name", name]
    if platform_:
        args += ["--platform", platform_]
    run(*args, image, "true")
    try:
        exported = subprocess.run(  # noqa: S603 - the container CLI with test-controlled arguments
            [CONTAINER, "export", name], capture_output=True, check=True, timeout=300
        ).stdout
    finally:
        run("rm", "-f", name, check=False)
    out: dict[str, tarfile.TarInfo | bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
        for member in archive.getmembers():
            path = "/" + member.name.lstrip("./")
            if member.isfile():
                out[path] = archive.extractfile(member).read()
            else:
                out[path] = member
    return out


# ELF e_machine values
ELF_MACHINES = {62: "x64", 183: "arm64"}


def elf_arch(data: bytes) -> str | None:
    if data[:4] != b"\x7fELF":
        return None
    return ELF_MACHINES.get(int.from_bytes(data[18:20], "little"))
