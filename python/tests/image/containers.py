# SPDX-License-Identifier: Apache-2.0

"""A thin wrapper over the docker or podman CLI for the image tests (CONTAINER=docker|podman)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


def runtime() -> str:
    chosen = os.environ.get("CONTAINER")
    if chosen:
        return chosen
    return "docker" if shutil.which("docker") else "podman"


CONTAINER = runtime()


def run(*args: str, check: bool = True, timeout: float = 300, input: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(  # noqa: S603 - the container CLI with test-controlled arguments
        [CONTAINER, *args], capture_output=True, text=True, timeout=timeout, check=False, input=input
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{CONTAINER} {' '.join(args)} failed ({result.returncode}):\n{result.stderr}\n{result.stdout}"
        )
    return result


def image_exists(name: str) -> bool:
    return run("image", "inspect", name, check=False).returncode == 0


@dataclass
class Network:
    name: str

    @classmethod
    def create(cls) -> Network:
        name = f"i019-{uuid.uuid4().hex[:8]}"
        run("network", "create", name)
        return cls(name)

    def remove(self) -> None:
        run("network", "rm", "-f", self.name, check=False)


@dataclass
class Container:
    name: str
    port: int | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        image: str,
        *,
        network: Network,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        volumes: dict[str, str] | None = None,
        publish: int | None = None,
        user: str | None = None,
        name: str | None = None,
        extra_args: list[str] | None = None,
    ) -> Container:
        name = name or f"i019-{uuid.uuid4().hex[:8]}"
        args = ["run", "-d", "--name", name, "--network", network.name, "--network-alias", name]
        if publish:
            args += ["-p", f"127.0.0.1::{publish}"]
        if user:
            args += ["--user", user]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        for volume, target in (volumes or {}).items():
            args += ["-v", f"{volume}:{target}"]
        args += extra_args or []
        args += [image, *(command or [])]
        run(*args)
        container = cls(name)
        if publish:
            container.port = container._published(publish)
        return container

    def _published(self, internal: int) -> int:
        out = run("port", self.name, f"{internal}/tcp").stdout.strip().splitlines()[0]
        return int(out.rsplit(":", 1)[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def internal_url(self) -> str:
        return f"http://{self.name}:{self.extra.get('internal_port', 8000)}"

    def logs(self) -> str:
        result = run("logs", self.name, check=False)
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        return run("inspect", "-f", "{{.State.Running}}", self.name, check=False).stdout.strip() == "true"

    def stop(self) -> None:
        """Kill the container and remove it. Podman's `rm` takes -t, docker's does not, so the two steps are separate: a
        container that is left running keeps exporting into the mock the next test of the worker uses."""
        run("kill", self.name, check=False)
        run("rm", "-f", self.name, check=False)


def http(method: str, url: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read().decode(), error.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.2, message: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    text = message() if callable(message) else message
    raise AssertionError(f"timed out waiting for {text} (last: {last!r})")


class Mock:
    """The mock server (testing/mock/server.py) running in a container."""

    def __init__(self, container: Container):
        self.container = container
        container.extra["internal_port"] = 8080
        wait_until(lambda: http("GET", f"{container.url}/healthz")[0] == 200, message="mock to start")

    @property
    def url(self) -> str:
        return self.container.url

    @property
    def internal_url(self) -> str:
        return self.container.internal_url

    def requests(self, path: str | None = None) -> list[dict]:
        query = f"?path={path}" if path else ""
        status, body = http("GET", f"{self.url}/_requests{query}")
        assert status == 200
        return body  # type: ignore[return-value]

    def reset(self) -> None:
        http("POST", f"{self.url}/_reset", {})

    def control(self, rules: list[dict] | None = None, require_key: str | None = None) -> None:
        http("POST", f"{self.url}/_control", {"rules": rules or [], "require_key": require_key})

    @staticmethod
    def body(record: dict) -> bytes:
        return base64.b64decode(record["body_b64"])

    def spans(self) -> list[dict]:
        """Every span received on /v1/traces as JSON, flattened with its resource and scope attached."""
        out = []
        for record in self.requests("/v1/traces"):
            payload = record.get("json")
            if not payload:
                continue
            for resource_spans in payload["resourceSpans"]:
                resource = attributes(resource_spans["resource"]["attributes"])
                for scope_spans in resource_spans["scopeSpans"]:
                    for span in scope_spans["spans"]:
                        out.append(
                            {
                                **span,
                                "attrs": attributes(span.get("attributes", [])),
                                "resource": resource,
                                "scope": scope_spans.get("scope", {}).get("name"),
                            }
                        )
        return out


def attribute_value(value: dict):
    ((kind, raw),) = value.items()
    if kind == "intValue":
        return int(raw)
    if kind == "arrayValue":
        return [attribute_value(item) for item in raw.get("values", [])]
    return raw


def attributes(items: list[dict]) -> dict:
    return {item["key"]: attribute_value(item["value"]) for item in items}


def protobuf_spans(mock: Mock, path: str = "/v1/traces") -> list[dict]:
    """Spans a customer's own OpenTelemetry exporter sent (OTLP/HTTP protobuf), as dicts with attributes by key."""
    import gzip

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    out = []
    for record in mock.requests(path):
        raw = Mock.body(record)
        if record["headers"].get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        request = ExportTraceServiceRequest()
        request.ParseFromString(raw)
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    out.append(
                        {
                            "name": span.name,
                            "kind": span.kind,
                            "scope": scope_spans.scope.name,
                            "attrs": {a.key: a.value.string_value or a.value.int_value for a in span.attributes},
                        }
                    )
    return out
