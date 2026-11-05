# SPDX-License-Identifier: Apache-2.0

"""Fixtures that reproduce the OpenTelemetry Operator's Python injection without Kubernetes.

The Operator adds an init container running our image (`cp -r /autoinstrumentation/. /otel-auto-instrumentation-python`
into an emptyDir), mounts that volume into the application container and sets PYTHONPATH and OTEL_* variables. These
fixtures do the same with the container CLI, so the tests exercise the real image and the real application images.
"""

from __future__ import annotations

import os
import uuid

import pytest

from containers import Container, Mock, Network, image_exists, run

IMAGE = os.environ.get("IMPACT_PYTHON_IMAGE", "localhost/impact-instrumentation/python:dev")
APP_PREFIX = os.environ.get("FIXTURE_APP_PREFIX", "localhost/impact-instrumentation/fixture-app")
MOCK_IMAGE = os.environ.get("MOCK_IMAGE", "localhost/impact-instrumentation/mock:dev")
MOUNT = "/otel-auto-instrumentation-python"
PREFIX = f"{MOUNT}/opentelemetry/instrumentation/auto_instrumentation"

# What the Operator injects for Python when the Instrumentation is the chart's: PYTHONPATH, the resource attributes, the
# pod and node addresses and IMPACT_OTLP_ENDPOINT. The chart's Instrumentation defines OTEL_TRACES_EXPORTER,
# OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER, OTEL_EXPORTER_OTLP_PROTOCOL and OTEL_SERVICE_NAME as optional references
# to a ConfigMap key that does not exist, so the Operator's own defaults for them never reach the workload (the
# end-to-end suite checks this on a real cluster). The package derives the service name from the deployment.
OPERATOR_ENV = {
    "PYTHONPATH": f"{PREFIX}:{MOUNT}",
    "OTEL_RESOURCE_ATTRIBUTES": (
        "k8s.container.name=app,k8s.deployment.name=support-assistant,k8s.namespace.name=shop,"
        "k8s.node.name=node-1,k8s.pod.name=support-assistant-6d9c-x2,k8s.replicaset.name=support-assistant-6d9c,"
        "service.instance.id=shop.support-assistant-6d9c-x2.app,service.namespace=shop,service.version=1.0"
    ),
    "OTEL_RESOURCE_ATTRIBUTES_POD_NAME": "support-assistant-6d9c-x2",
    "OTEL_RESOURCE_ATTRIBUTES_NODE_NAME": "node-1",
    "OTEL_POD_IP": "10.244.0.18",
    "OTEL_NODE_IP": "10.89.2.2",
    # Fast export for tests; the default (5 s) is the SDK's own and is covered by the end-to-end suite.
    "OTEL_BSP_SCHEDULE_DELAY": "300",
}


def app_image(python: str, kind: str = "") -> str:
    return f"{APP_PREFIX}{'-' + kind if kind else ''}:{python}"


def require_images(*names: str) -> None:
    missing = [n for n in names if not image_exists(n)]
    if missing:
        pytest.fail(f"missing images {missing}: run `make test-image` (or `make images`) first", pytrace=False)


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
    """Volumes filled by the image's own init command, one per libc variant."""
    require_images(IMAGE)
    volumes = {}
    for variant, source in (("glibc", "/autoinstrumentation/."), ("musl", "/autoinstrumentation-musl/.")):
        name = f"i019-payload-{variant}-{uuid.uuid4().hex[:6]}"
        run("volume", "create", name)
        # A new volume belongs to root and is not writable by the image's user; a Kubernetes emptyDir is (mode 1777).
        run("run", "--rm", "--user", "0", "-v", f"{name}:{MOUNT}", IMAGE, "chmod", "1777", MOUNT)
        # The init container runs as the image's user (non-root) and copies with plain `cp`, like the Operator's.
        run("run", "--rm", "-v", f"{name}:{MOUNT}", IMAGE, "cp", "-r", source, MOUNT)
        volumes[variant] = name
    yield volumes
    for name in volumes.values():
        run("volume", "rm", "-f", name, check=False)


@pytest.fixture
def start_app(network, payload, mock):
    """Start a fixture workload with the payload mounted and the Operator's environment."""
    started: list[Container] = []

    def start(
        image: str,
        command: list[str],
        *,
        variant: str = "glibc",
        env: dict[str, str] | None = None,
        inject: bool = True,
        gateway: str | None = None,
    ) -> Container:
        base = {"STUB_URL": mock.internal_url}
        if inject:
            base.update(OPERATOR_ENV)
            base["IMPACT_OTLP_ENDPOINT"] = gateway or mock.internal_url
        base.update(env or {})
        container = Container.start(
            image,
            network=network,
            command=command,
            env=base,
            volumes={payload[variant]: MOUNT} if inject else None,
            publish=8000,
            user="10001:10001",  # not root, and not the user that copied the files
        )
        started.append(container)
        return container

    yield start
    for container in started:
        container.stop()
