# SPDX-License-Identifier: Apache-2.0

"""Fixtures of the .NET proofs (the helpers they use are in dotnet_support.py)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# containers.py, the image tests' wrapper of the container CLI, is in python/tests/image: the helpers import it too.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "python" / "tests" / "image"))

from dotnet_support import (  # noqa: E402
    DOTNETS,
    HOME,
    IMAGE,
    MOCK_IMAGE,
    Container,
    Mock,
    app_image,
    kit_env,
    operator_env,
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
        Container.start(MOCK_IMAGE, network=network, publish=8080, name=f"i019-mock-{uuid.uuid4().hex[:6]}")
        for _ in range(2)
    ]
    yield [Mock(c) for c in started]
    for c in started:
        c.stop()


@pytest.fixture
def mock(_mocks):
    """impact.ai: the model provider stub and the kit's endpoint."""
    _mocks[0].reset()
    return _mocks[0]


@pytest.fixture
def theirs(_mocks):
    """The workload's own backend: Datadog's agent, or an OpenTelemetry collector of its own."""
    _mocks[1].reset()
    return _mocks[1]


@pytest.fixture(scope="session")
def payload():
    """The volume the payload image's init command fills, as the Operator's init container does (one per pod; nothing
    writes to it afterwards, so the session shares one)."""
    require_images(IMAGE)
    name = f"i019-payload-dotnet-{uuid.uuid4().hex[:6]}"
    run("volume", "create", name)
    # A new volume belongs to root and is not writable by the image's user; a Kubernetes emptyDir is (mode 1777).
    run("run", "--rm", "--user", "0", "--entrypoint", "chmod", "-v", f"{name}:{HOME}", IMAGE, "1777", HOME)
    # The init container runs as the image's user (non-root) and runs the Operator's own command.
    run("run", "--rm", "-v", f"{name}:{HOME}", IMAGE, "cp", "-r", "/autoinstrumentation/.", HOME)
    yield name
    run("volume", "rm", "-f", name, check=False)


@pytest.fixture
def start_app(network, payload, mock):
    """Start a fixture workload: its own environment, then the Operator's injection with the kit's Instrumentation env
    (or none, inject=False). `image` and `command` start another workload than the assistant of `dotnet` and `libc`."""
    started: list[Container] = []

    def start(
        libc: str = "glibc",
        *,
        dotnet: str = DOTNETS[0],
        env: dict[str, str] | None = None,
        inject: bool = True,
        hook: bool = True,
        runtime: str | None = None,
        spec: dict[str, str] | None = None,
        image: str | None = None,
        command: list[str] | None = None,
    ) -> Container:
        own = {"STUB_URL": mock.internal_url, "PORT": "8000", **(env or {})}
        if inject:
            instrumentation = {**kit_env(mock.internal_url, hook=hook), **(spec or {})}
            injected = operator_env(own, instrumentation, runtime)
            assert injected is not None, "the Operator would not inject this container"
            own = injected
        container = Container.start(
            image or app_image(dotnet, libc),
            network=network,
            command=command,
            env=own,
            volumes={payload: HOME} if inject else None,
            publish=8000,
            user="10001:10001",  # not root, and not the user that copied the files
        )
        started.append(container)
        return container

    yield start
    for container in started:
        container.stop()
