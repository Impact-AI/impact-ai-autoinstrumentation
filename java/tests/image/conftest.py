# SPDX-License-Identifier: Apache-2.0

"""Fixtures of the Java image tests (the helpers they use are in java_support.py)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# containers.py, the image tests' wrapper of the container CLI, is in python/tests/image: the helpers import it too.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "python" / "tests" / "image"))

from java_support import (  # noqa: E402
    IMAGE,
    INIT_MOUNT,
    JAVA_TOOL_OPTIONS,
    MOCK_IMAGE,
    MOUNT,
    OPERATOR_ENV,
    Container,
    Mock,
    app_image,
    require_images,
    run,
)

from containers import Network  # noqa: E402


@pytest.fixture(scope="session")
def network():
    net = Network.create()
    yield net
    net.remove()


@pytest.fixture(scope="session")
def _mocks(network):
    require_images(MOCK_IMAGE)
    started = [
        Container.start(MOCK_IMAGE, network=network, publish=8080, name=f"kit-test-mock-{uuid.uuid4().hex[:6]}")
        for _ in range(2)
    ]
    yield [Mock(c) for c in started]
    for c in started:
        c.stop()


@pytest.fixture
def mock(_mocks):
    """impact.ai: the provider stub and the gateway endpoint."""
    _mocks[0].reset()
    return _mocks[0]


@pytest.fixture
def theirs(_mocks):
    """The workload's own backend, for an OpenTelemetry agent of its own."""
    _mocks[1].reset()
    return _mocks[1]


@pytest.fixture
def payload():
    """The volume the image's init command fills, as the Operator's init container does: one per test, as one emptyDir
    per pod, because the agent unpacks itself into it."""
    require_images(IMAGE)
    name = f"kit-test-payload-java-{uuid.uuid4().hex[:6]}"
    run("volume", "create", name)
    # A new volume belongs to root and is not writable by the image's user; a Kubernetes emptyDir is (mode 1777).
    run("run", "--rm", "--user", "0", "--entrypoint", "chmod", "-v", f"{name}:{INIT_MOUNT}", IMAGE, "1777", INIT_MOUNT)
    # The init container runs as the image's user (non-root) and runs the Operator's own command.
    run("run", "--rm", "-v", f"{name}:{INIT_MOUNT}", IMAGE, "cp", "/javaagent.jar", f"{INIT_MOUNT}/javaagent.jar")
    yield name
    run("volume", "rm", "-f", name, check=False)


@pytest.fixture
def start_app(network, payload, mock):
    """Start a fixture workload with the payload mounted and the Operator's environment."""
    started: list[Container] = []

    def start(
        java: str = "21",
        *,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        inject: bool = True,
        gateway: str | None = None,
        tool_options: str = "",
        payload_mode: str = "",
        extra_args: list[str] | None = None,
    ) -> Container:
        base = {"STUB_URL": mock.internal_url}
        if inject:
            base.update(OPERATOR_ENV)
            base["IMPACT_OTLP_ENDPOINT"] = gateway or mock.internal_url
            base["JAVA_TOOL_OPTIONS"] = f"{tool_options}{JAVA_TOOL_OPTIONS}".strip()
        elif tool_options:
            base["JAVA_TOOL_OPTIONS"] = tool_options
        base.update(env or {})
        volumes = {payload: f"{MOUNT}{payload_mode}"} if inject else {}
        container = Container.start(
            app_image(java),
            network=network,
            command=command,
            env=base,
            volumes=volumes or None,
            publish=8000,
            user="10001:10001",  # not root, and not the user that copied the file
            extra_args=extra_args,
        )
        started.append(container)
        return container

    yield start
    for container in started:
        container.stop()
