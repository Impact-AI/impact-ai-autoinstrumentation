# SPDX-License-Identifier: Apache-2.0

"""Fixtures of the Node.js image tests (the helpers they use are in nodejs_support.py)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# containers.py, the image tests' wrapper of the container CLI, is in python/tests/image: the helpers import it too.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "python" / "tests" / "image"))

from containers import Network  # noqa: E402
from nodejs_support import (  # noqa: E402
    IMAGE,
    MOCK_IMAGE,
    MOUNT,
    OPERATOR_ENV,
    PRELOAD,
    Container,
    Mock,
    app_image,
    require_images,
    run,
)


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
    """impact.ai: provider stubs and the ingest/gateway endpoint."""
    _mocks[0].reset()
    return _mocks[0]


@pytest.fixture
def theirs(_mocks):
    """The customer's own backend (collector, Datadog agent, ...)."""
    _mocks[1].reset()
    return _mocks[1]


@pytest.fixture(scope="session")
def payload():
    """The volume the image's own init command fills, as the Operator's init container does."""
    require_images(IMAGE)
    name = f"i019-payload-nodejs-{uuid.uuid4().hex[:6]}"
    run("volume", "create", name)
    # A new volume belongs to root and is not writable by the image's user; a Kubernetes emptyDir is (mode 1777).
    run("run", "--rm", "--user", "0", "-v", f"{name}:{MOUNT}", IMAGE, "chmod", "1777", MOUNT)
    # The init container runs as the image's user (non-root) and copies with plain `cp`, like the Operator's.
    run("run", "--rm", "-v", f"{name}:{MOUNT}", IMAGE, "cp", "-r", "/autoinstrumentation/.", MOUNT)
    yield name
    run("volume", "rm", "-f", name, check=False)


@pytest.fixture
def start_app(network, payload, mock):
    """Start a fixture workload with the payload mounted and the Operator's environment."""
    started: list[Container] = []

    def start(
        node: str,
        command: list[str],
        *,
        directory: str = "/apps/express",
        env: dict[str, str] | None = None,
        inject: bool = True,
        gateway: str | None = None,
        node_options: str = "",
        mounts: dict[str, str] | None = None,
        image: str | None = None,
        extra_args: list[str] | None = None,
        publish: int | None = 8000,
        wait: bool = False,
    ) -> Container:
        base = {"STUB_URL": mock.internal_url}
        if inject:
            base.update(OPERATOR_ENV)
            base["IMPACT_OTLP_ENDPOINT"] = gateway or mock.internal_url
            base["NODE_OPTIONS"] = f"{node_options} --require {PRELOAD}".strip()
        elif node_options:
            base["NODE_OPTIONS"] = node_options
        base.update(env or {})
        volumes = {payload: MOUNT} if inject else {}
        volumes.update(mounts or {})
        container = Container.start(
            image or app_image(node),
            network=network,
            command=command,
            env=base,
            volumes=volumes or None,
            publish=publish,
            user="10001:10001",  # not root, and not the user that copied the files
            extra_args=["-w", directory, *(extra_args or [])],
        )
        started.append(container)
        return container

    yield start
    for container in started:
        container.stop()
