# SPDX-License-Identifier: Apache-2.0

"""The preload never throws and never stops the application: on every Node from 16 to 26, and in the conditions that go
wrong.

Node 22.15 and later start; older Node, Node 20 (end of life since 2026-04-30) included, says why it does not (a
`failed` report) and the application starts as it would have.
"""

from __future__ import annotations

import time

import pytest

from containers import http, wait_until
from nodejs_support import (
    NODES,
    OLD_NODES,
    app_image,
    ensure_image,
    only,
    readable,
    require_images,
    run_once,
    setup_reports,
    wait_ready,
)

SCRIPT = "process.stdout.write('ok')"
# What the setup report says of a Node below the floor: names and versions only, and the version the workload runs last.
FLOOR_MESSAGE = "Impact AI traces Node.js 22.15 and later; this workload runs Node.js "


def image_for(node: str) -> str:
    if node in OLD_NODES:
        return ensure_image(OLD_NODES[node])
    require_images(app_image(node))
    return app_image(node)


@pytest.mark.nodejs
@pytest.mark.parametrize("node", [*OLD_NODES, *NODES])
@pytest.mark.parametrize("how", ["-e", "script"])
def test_the_preload_never_throws_on_any_node_from_16_to_26(network, payload, mock, node, how, tmp_path):
    image = image_for(node)
    extra = None
    if how == "-e":
        command = ["node", "-e", SCRIPT]
    else:
        script = tmp_path / "hello.js"
        script.write_text(SCRIPT)
        readable(script)
        command, extra = ["node", "/work/hello.js"], ["-v", f"{script}:/work/hello.js:ro"]
    result = run_once(
        image,
        command,
        network=network,
        payload=payload,
        env={"IMPACT_OTLP_ENDPOINT": mock.internal_url},
        extra_args=extra,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("ok")
    assert result.stderr == "", result.stderr
    # every Node says where it stands: the supported ones start, the others say why they do not
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    report = only(setup_reports(mock), "setup report")
    if node in OLD_NODES:
        assert report["attrs"]["impact.setup.state"] == "failed"
        assert report["attrs"]["impact.setup.detail"].startswith(f"{FLOOR_MESSAGE}{node}.")
        assert report["attrs"]["impact.setup.frameworks"] == [] and report["attrs"]["impact.setup.untraced"] == []
    else:
        assert report["attrs"]["impact.setup.state"] == "active"
        assert report["attrs"]["impact.setup.node.version"].startswith(f"{node}.")
        assert report["attrs"]["impact.setup.node.tree"] == "node22"
    assert report["attrs"]["impact.setup.package.version"]
    assert report["resource"]["telemetry.sdk.language"] == "nodejs"


SERVER = """
const http = require('node:http');
http
  .createServer((request, response) => {
    if (request.url === '/healthz') return response.end('ok');
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({ answer: 'served by the application', node: process.version }));
  })
  .listen(8000, '0.0.0.0');
"""


@pytest.mark.nodejs
def test_a_node_20_application_starts_and_serves_as_without_the_kit_and_the_report_names_the_reason(
    start_app, mock, tmp_path
):
    """Node 20 is below the floor of the one payload tree (Node 22.15): the workload is not touched. It starts, serves
    and logs exactly as it would without the kit, no span comes from it, and the setup report says why."""
    server = tmp_path / "server.js"
    server.write_text(SERVER)
    readable(server)
    app = start_app(
        "20",
        ["node", "/work/server.js"],
        directory="/work",
        image=ensure_image(OLD_NODES["20"]),
        mounts={str(server): "/work/server.js:ro"},
    )
    wait_ready(app, timeout=40)
    status, body = http("GET", f"{app.url}/anything")
    assert status == 200 and body["answer"] == "served by the application" and body["node"].startswith("v20.")
    wait_until(lambda: setup_reports(mock), timeout=30, message=lambda: f"the setup report\n{app.logs()}")
    time.sleep(1.5)
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "failed"
    assert report["attrs"]["impact.setup.detail"].startswith(f"{FLOOR_MESSAGE}20.")
    assert report["attrs"]["impact.setup.node.version"].startswith("20.")
    assert report["attrs"]["impact.setup.frameworks"] == [] and report["attrs"]["impact.setup.untraced"] == []
    assert "impact.setup.node.tree" not in report["attrs"]
    # nothing else came from the workload, and the kit wrote nothing to its output
    assert [s["name"] for s in mock.spans()] == ["impact.setup"]
    assert app.logs().strip() == ""


HOSTILE = {
    "an endpoint that is nonsense": {"env": {"IMPACT_OTLP_ENDPOINT": "ht!tp://[::"}},
    "an endpoint nobody listens on": {"env": {"IMPACT_OTLP_ENDPOINT": "http://127.0.0.1:9"}},
    "resource attributes that are junk": {"env": {"OTEL_RESOURCE_ATTRIBUTES": "%%%,=,,k8s.namespace.name"}},
    "a service name with control characters and right-to-left marks": {
        "env": {"OTEL_SERVICE_NAME": "\u540d\u524d \x1b[31m \u202e" + "x" * 5000}
    },
    "no writable place for the report's marker": {"extra_args": ["--read-only"], "readonly_payload": True},
    "a working directory without a node_modules": {"extra_args": ["-w", "/"]},
    "options of the application's own": {"node_options": "--max-old-space-size=64 --no-warnings"},
    "the opt-out": {"env": {"OTEL_SDK_DISABLED": "true"}},
    "every instrumentation switched off": {"env": {"OTEL_NODE_DISABLED_INSTRUMENTATIONS": "all"}},
    "a state directory that is not there": {"env": {"IMPACT_STATE_DIR": "/does/not/exist"}},
}


@pytest.mark.nodejs
@pytest.mark.parametrize("name", HOSTILE)
def test_the_application_starts_and_ends_normally_whatever_goes_wrong_around_the_preload(network, payload, mock, name):
    node = "22"
    require_images(app_image(node))
    case = HOSTILE[name]
    env = {"IMPACT_OTLP_ENDPOINT": mock.internal_url, **case.get("env", {})}
    result = run_once(
        app_image(node),
        ["node", "-e", SCRIPT],
        network=network,
        payload=payload,
        env=env,
        node_options=case.get("node_options", ""),
        extra_args=case.get("extra_args"),
        readonly_payload=case.get("readonly_payload", False),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert result.stderr == "", result.stderr
    # a process that waits for a gateway nobody listens on does not hold its exit for more than the grace period
    assert result.elapsed < 15, result.elapsed


@pytest.mark.nodejs
def test_a_short_lived_job_still_delivers_its_report(network, payload, mock):
    """The report is the one thing that may delay a process at exit, and only until it has been delivered."""
    require_images(app_image("22"))
    result = run_once(
        app_image("22"),
        ["node", "-e", SCRIPT],
        network=network,
        payload=payload,
        env={"IMPACT_OTLP_ENDPOINT": mock.internal_url},
    )
    assert result.returncode == 0
    wait_until(lambda: setup_reports(mock), timeout=10, message="the report of a job that ended at once")
    assert result.elapsed < 10
