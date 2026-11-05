# SPDX-License-Identifier: Apache-2.0

"""The image itself: layout the Operator needs, non-root, no provider SDK, bytecode for every claimed Python."""

from __future__ import annotations

import json

import pytest
from test_injected_apps import SERVERS, wait_ready

from conftest import APP_PREFIX, IMAGE, MOUNT, app_image, require_images
from containers import http, run, wait_until

FORBIDDEN = {"openai", "anthropic", "boto3", "botocore", "requests", "urllib3", "certifi", "idna", "charset_normalizer"}


def listing(path: str, *args: str) -> list[str]:
    out = run("run", "--rm", IMAGE, "find", path, *args).stdout
    return sorted(out.split())


@pytest.mark.image
def test_the_image_runs_as_a_non_root_user_and_has_cp():
    require_images(IMAGE)
    config = json.loads(run("image", "inspect", IMAGE, "--format", "{{json .Config}}").stdout)
    assert config["User"] == "65532:65532"
    assert run("run", "--rm", IMAGE, "sh", "-c", "command -v cp").stdout.strip().endswith("cp")
    assert config["Labels"]["org.opencontainers.image.version"]


@pytest.mark.image
@pytest.mark.parametrize("variant", ["autoinstrumentation", "autoinstrumentation-musl"])
def test_no_provider_sdk_and_no_shadowing_library_is_in_the_image(variant):
    require_images(IMAGE)
    top = [p.rsplit("/", 1)[1] for p in listing(f"/{variant}", "-maxdepth", "1", "-mindepth", "1")]
    shared = [p.rsplit("/", 1)[1] for p in listing(f"/{variant}/_impact_shared", "-maxdepth", "1", "-mindepth", "1")]
    for name in top + shared:
        stem = name.split("-")[0].split(".")[0].lower()
        assert stem not in FORBIDDEN, name
    assert (
        "sitecustomize.py"
        in run("run", "--rm", IMAGE, "ls", f"/{variant}/opentelemetry/instrumentation/auto_instrumentation").stdout
    )


@pytest.mark.image
def test_bytecode_ships_for_every_claimed_python():
    require_images(IMAGE)
    files = {
        p.rsplit("/", 1)[1]
        for p in listing("/autoinstrumentation/impact_instrumentation/__pycache__", "-name", "bootstrap*")
    }
    assert files == {f"bootstrap.cpython-{v}.pyc" for v in ("311", "312", "313", "314")}
    sdk = listing("/autoinstrumentation/opentelemetry/sdk/trace/__pycache__", "-name", "__init__*")
    assert len(sdk) == 4


@pytest.mark.image
@pytest.mark.parametrize("python", ["3.11", "3.12", "3.13", "3.14"])
def test_the_applications_own_libraries_win_and_wrapt_is_compiled(start_app, python):
    """typing_extensions and packaging come from the application, wrapt and asgiref (it has none) from our fallback;
    wrapt's compiled extension loads on every claimed Python version."""
    require_images(app_image(python))
    app = start_app(app_image(python), SERVERS["uvicorn"][0])
    wait_ready(app)
    status, runtime = http("GET", f"{app.url}/debug/runtime")
    assert status == 200
    assert runtime["python"].startswith(python)
    files = runtime["files"]
    assert "/site-packages/" in files["typing_extensions"] and MOUNT not in files["typing_extensions"]
    assert "/site-packages/" in files["packaging"] and MOUNT not in files["packaging"]
    assert files["wrapt"].startswith(f"{MOUNT}/_impact_shared/wrapt/")
    assert files["asgiref"].startswith(f"{MOUNT}/_impact_shared/asgiref/")
    assert runtime["wrapt_c"] is True


@pytest.mark.image
def test_an_exec_probe_in_an_injected_container_is_not_slowed(start_app):
    """Exec probes start a fresh Python in the container with the same environment and often time out after 1 s."""
    import time

    require_images(app_image("3.12"))
    app = start_app(app_image("3.12"), SERVERS["uvicorn"][0])
    wait_ready(app)
    slowest = 0.0
    for _ in range(5):
        started = time.monotonic()
        run("exec", app.name, "python", "-c", "pass")
        slowest = max(slowest, time.monotonic() - started)
    plain = time.monotonic()
    run("exec", "-e", "PYTHONPATH=", app.name, "python", "-c", "pass")
    baseline = time.monotonic() - plain
    assert slowest < baseline + 0.6, f"probe took {slowest:.2f}s against {baseline:.2f}s without injection"


@pytest.mark.image
def test_uvicorn_workers_started_by_multiprocessing_are_instrumented_too(start_app, mock):
    require_images(app_image("3.12"))
    app = start_app(app_image("3.12"), [*SERVERS["uvicorn"][0], "--workers", "2"])
    wait_ready(app)
    for _ in range(4):
        assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[0] == 200
    wait_until(
        lambda: [s for s in mock.spans() if s["attrs"].get("gen_ai.request.model") == "gpt-4o-mini"],
        timeout=30,
        message="LLM spans from the worker processes",
    )


@pytest.mark.image
def test_musl_python_with_the_platform_payload(start_app, mock):
    require_images(f"{APP_PREFIX}:3.12-alpine")
    app = start_app(f"{APP_PREFIX}:3.12-alpine", SERVERS["uvicorn"][0], variant="musl")
    wait_ready(app)
    assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[1] == {"answer": "Hello from the stub."}
    wait_until(
        lambda: [s for s in mock.spans() if s["attrs"].get("gen_ai.request.model") == "gpt-4o-mini"],
        timeout=30,
        message="an LLM span from the musl workload",
    )
    status, runtime = http("GET", f"{app.url}/debug/runtime")
    assert status == 200 and runtime["wrapt_c"] is True
