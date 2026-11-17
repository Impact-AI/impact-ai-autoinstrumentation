# SPDX-License-Identifier: Apache-2.0

"""Proof first: platforms are proved, not assumed.

The Operator's runtime annotation (instrumentation.opentelemetry.io/otel-dotnet-auto-runtime) takes linux-x64 or
linux-musl-x64 and only chooses the directory of CORECLR_PROFILER_PATH: linux/ or linux-musl/. Neither name carries
the architecture, and the Operator's own image puts each architecture's profiler there, so the annotation's "x64"
does not keep arm64 out: an arm64 image of the payload, with arm64 profilers in those directories, is what an arm64
node needs.

  - On this machine's architecture: a glibc workload (no annotation, or linux-x64) and a musl workload (linux-musl-x64)
    are traced, the profiler attached. On an arm64 machine the same tests prove arm64.
  - A workload whose C library the annotation does not match starts and serves as it would without the kit, and the
    kit's hook says why instead of reporting a start.
  - The arm64 payload holds arm64 profilers at the Operator's paths (read without running it).
"""

from __future__ import annotations

import time

import pytest
from dotnet_support import (
    ARCH,
    HOME,
    IMAGE,
    PROFILER_PATHS,
    UPSTREAM_HOOK,
    agent_spans,
    chat_spans,
    elf_arch,
    http,
    image_files,
    server_span,
    wait_ready,
    wait_until,
)

pytestmark = pytest.mark.dotnet

PROFILER_LINE = f"OpenTelemetry CLR Profiler 1.18.0 on Linux ({'amd64' if ARCH == 'x64' else 'arm64'})"


@pytest.mark.parametrize(
    ("libc", "runtime"),
    [("glibc", None), ("glibc", "linux-x64"), ("musl", "linux-musl-x64")],
    ids=["glibc, no annotation", "glibc, linux-x64", "musl, linux-musl-x64"],
)
def test_each_c_library_by_the_annotation(start_app, mock, libc, runtime):
    app = start_app(libc, runtime=runtime)
    wait_ready(app)
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    wait_until(lambda: chat_spans(mock), timeout=60, message=lambda: f"the chat span\n{app.logs()[-5000:]}")
    wait_until(lambda: server_span(mock, "/chat"), timeout=30, message="the request's span")
    logs = app.logs()
    assert PROFILER_LINE in logs, logs[-5000:]
    assert "IsProfilerAttached returned false" not in logs


@pytest.mark.parametrize(
    ("libc", "runtime", "reason"),
    [
        ("musl", None, f"names glibc (linux), the workload runs musl (linux-musl-{ARCH})"),
        ("glibc", "linux-musl-x64", f"names musl (linux-musl), the workload runs glibc (linux-{ARCH})"),
    ],
    ids=["musl, no annotation", "glibc, linux-musl-x64"],
)
def test_a_c_library_the_annotation_does_not_match_starts_without_the_kit(start_app, mock, libc, runtime, reason):
    app = start_app(libc, runtime=runtime)
    wait_ready(app)
    status, answer = http("POST", f"{app.url}/chat", {})
    assert status == 200 and "Hello from the stub." in str(answer)
    time.sleep(2)
    assert agent_spans(mock) == [], "the upstream agent declines when its profiler did not load"
    logs = app.logs()
    assert "Automatic Instrumentation won't be loaded" in logs
    # the kit's hook says why, and wraps nothing that nothing would trace
    assert f"impact.ai: not started: the runtime annotation {reason}" in logs, logs[-5000:]
    assert "impact.ai: started" not in logs
    assert "impact.ai: chat clients wrapped" not in logs


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_the_payload_holds_each_architectures_profiler_at_the_operators_paths(arch):
    files = image_files(f"{IMAGE}-{arch}", {"x64": "linux/amd64", "arm64": "linux/arm64"}[arch])
    for runtime, path in PROFILER_PATHS.items():
        source = "/autoinstrumentation" + path[len(HOME) :]
        assert isinstance(files.get(source), bytes), f"{runtime}: {source} is not in the payload"
        assert elf_arch(files[source]) == arch, f"{runtime}: {source} is not an {arch} profiler"
    glibc = files["/autoinstrumentation/linux/OpenTelemetry.AutoInstrumentation.Native.so"]
    musl = files["/autoinstrumentation/linux-musl/OpenTelemetry.AutoInstrumentation.Native.so"]
    assert glibc != musl
    # the links of the Operator's image, for CORECLR_PROFILER_PATH values written before it went multi-architecture
    for link, target in [(f"linux-{arch}", "linux"), (f"linux-musl-{arch}", "linux-musl")]:
        member = files[f"/autoinstrumentation/{link}"]
        assert not isinstance(member, bytes) and member.issym() and member.linkname == target, link
    assert isinstance(files.get("/autoinstrumentation" + UPSTREAM_HOOK[len(HOME) :]), bytes)
    assert isinstance(files.get("/autoinstrumentation/impact/Impact.StartupHook.dll"), bytes)
