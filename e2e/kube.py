# SPDX-License-Identifier: Apache-2.0

"""kubectl, helm and the mock services, for the end-to-end suite."""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "chart" / "impact-ai"
MANIFESTS = Path(__file__).resolve().parent / "manifests"
UNINSTALL_SCRIPT = ROOT / "scripts" / "uninstall-cluster-kit.sh"

CONTEXT = os.environ.get("E2E_CONTEXT", "")
IMAGE_PREFIX = os.environ.get("IMAGE_PREFIX", "localhost/impact-instrumentation")
TAG = os.environ.get("TAG", "dev")
KEY = "e2e-ingest-key-" + "0123456789abcdef" * 2
INTERVAL = "10s"
PRODUCT = "prod_e2e"
# A cluster name is free text from the install command, and the Collector reads ${...} in what its configuration holds:
# this one has a reference to the gateway's key in it, which has to reach the spans, the inventory and the final notice
# as the text it is.
CLUSTER_NAME = "kind-e2e ${env:IMPACT_INGEST_KEY} $"

SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "impact-ai"}
# The cloud identity the install command carries (Cover your AI fills it in for a discovered cluster): in the
# inventory's cluster object, and as resource attributes on every span through the gateway.
CLOUD_ARN = "arn:aws:eks:eu-west-1:012345678901:cluster/kind-e2e"
CLOUD_IN_INVENTORY = {"cloudProvider": "aws", "accountId": "012345678901", "region": "eu-west-1", "arn": CLOUD_ARN}
CLOUD_ON_SPANS = {
    "cloud.provider": "aws",
    "cloud.account.id": "012345678901",
    "cloud.region": "eu-west-1",
    "cloud.resource_id": CLOUD_ARN,
}
PLANTED = ("sk-e2e-planted", "org-e2e-planted")  # env value, argument value, Secret values: none may leave the cluster


def run(command: list[str], *, input: str | None = None, check: bool = True, timeout: float = 300):
    result = subprocess.run(  # noqa: S603 - kubectl/helm/sh with test-controlled arguments
        command, input=input, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} failed ({result.returncode}):\n{result.stderr}\n{result.stdout}")
    return result


def run_uninstall_script(*, helm_uninstall_done: bool = False, timeout: float = 900):
    """Run the shown uninstall command (scripts/uninstall-cluster-kit.sh) against the cluster of the suite.

    With `helm_uninstall_done` its first step is left out: the script finds a `helm` that accepts `uninstall` and does
    nothing, and passes everything else to the real one. That is for a test that ran `helm uninstall` itself, to look at
    what the pre-delete hook does, before the rest of the command (steps 2 to 5) runs."""
    if CONTEXT:
        kubectl("config", "use-context", CONTEXT)
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as directory:
        if helm_uninstall_done:
            stand_in = Path(directory) / "helm"
            stand_in.write_text(
                f'#!/bin/sh\nif [ "$1" = uninstall ]; then exit 0; fi\nexec "{shutil.which("helm")}" "$@"\n'
            )
            stand_in.chmod(0o755)
            env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"
        return subprocess.run(  # noqa: S603 - sh with the repository's own script
            ["sh", str(UNINSTALL_SCRIPT)],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def fixture_frameworks() -> list[str]:
    """The AI libraries of the fixture workloads (testing/apps/requirements.txt), as the setup report lists them."""
    pins = (ROOT / "testing" / "apps" / "requirements.txt").read_text()
    versions = {name: re.search(rf"^{name}==(\S+)", pins, re.MULTILINE)[1] for name in ("anthropic", "openai")}
    return [f"{name} {version}" for name, version in versions.items()]


def kubectl(*args: str, **kwargs):
    context = ["--context", CONTEXT] if CONTEXT else []
    return run(["kubectl", *context, *args], **kwargs)


def helm(*args: str, **kwargs):
    context = ["--kube-context", CONTEXT] if CONTEXT else []
    return run(["helm", *context, *args], **kwargs)


def get(*args: str) -> dict:
    return json.loads(kubectl("get", *args, "-o", "json").stdout)


def wait_until(predicate, *, timeout: float = 120.0, interval: float = 1.0, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001 - the condition is not there yet
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    text = message() if callable(message) else message
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {text} (last: {last!r})")


def rollout(kind_name: str, namespace: str, timeout: int = 240) -> None:
    kubectl("-n", namespace, "rollout", "status", kind_name, f"--timeout={timeout}s", timeout=timeout + 30)


def operator_hint() -> str:
    """Why the Operator is not Ready, from its own log: a host out of inotify instances is the usual local cause."""
    logs = kubectl("-n", "impact-ai", "logs", "deploy/impact-ai-operator", "--tail=3", check=False)
    text = (logs.stdout + logs.stderr).strip()
    if "inotify" in text:
        return (
            "The Operator cannot start: this host is out of inotify instances (fs.inotify.max_user_instances, "
            "shared by every process of the user, kind's containers included). Raise it, e.g. "
            "sudo sysctl -w fs.inotify.max_user_instances=1024. Operator log:\n" + text
        )
    return "Operator log:\n" + text


def operator_pod() -> dict | None:
    """The Operator's one pod, or None while there is none or more than one (a rollout, a restart of the pod)."""
    items = pods("impact-ai", "app.kubernetes.io/component=controller-manager")
    return items[0] if len(items) == 1 else None


def operator_ready_now() -> bool:
    pod = operator_pod()
    return pod is not None and all(s.get("ready") for s in pod["status"].get("containerStatuses", []))


def operator_stamp() -> tuple:
    """Which Operator pod there is and how often its containers restarted: the same before and after a step means that
    the Operator was not restarted during it (it still may have been starting up before; see wait_operator_ready)."""
    pod = operator_pod()
    if pod is None:
        return ("none",)
    return (pod["metadata"]["uid"], sum(s["restartCount"] for s in pod["status"].get("containerStatuses", [])))


@contextlib.contextmanager
def operator_nudged(period: float = 15.0):
    """While the block runs, replace the Operator's pod at once whenever it sits in CrashLoopBackOff.

    On a host short of inotify instances (fs.inotify.max_user_instances is shared by all processes of a user, kind's
    containers included) the Operator exits as it starts, and the kubelet then waits up to five minutes before the
    next try. A replacement pod tries at once, so the first moment an instance is free is used. A host with enough
    instances never gets here.
    """
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(period):
            try:
                pod = operator_pod()
                waiting = [
                    status["state"]["waiting"].get("reason")
                    for status in (pod or {}).get("status", {}).get("containerStatuses", [])
                    if "waiting" in status.get("state", {})
                ]
                if pod and "CrashLoopBackOff" in waiting:
                    kubectl("-n", "impact-ai", "delete", "pod", pod["metadata"]["name"], "--wait=false", check=False)
            except (AssertionError, subprocess.SubprocessError, KeyError, ValueError):
                continue  # the cluster did not answer this time; the next look is soon

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=60)


def wait_operator_ready(timeout: float = 900.0) -> None:
    """The Operator's pod is Ready and stays so for a few seconds (it restarts on a host out of inotify instances)."""

    def ready():
        return operator_ready_now() or None

    try:
        with operator_nudged():
            wait_until(ready, timeout=timeout, interval=3, message="the Operator to be ready")
            time.sleep(8)
            wait_until(ready, timeout=timeout, interval=3, message="the Operator to stay ready")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{operator_hint()}") from None


INJECT = "instrumentation.opentelemetry.io/inject-python"
INJECT_VALUE = "impact-ai/impact"  # what the per-row command sets
INIT_CONTAINER = "opentelemetry-auto-instrumentation-python"


def cover(name: str, namespace: str = "shop", *, annotate: bool = False, attempts: int = 5) -> None:
    """Get an injected pod of the workload: the per-row command (`annotate`), or a restart, repeated if need be.

    A pod created while the Operator is down or not yet serving starts uninjected (that is the point of failing open),
    so wait for the Operator and restart the workload again until the pod is injected.
    """
    patch = {"spec": {"template": {"metadata": {"annotations": {INJECT: INJECT_VALUE}}}}}
    for attempt in range(attempts):
        wait_operator_ready()
        if annotate and attempt == 0:
            kubectl("-n", namespace, "patch", "deployment", name, "--type", "merge", "-p", json.dumps(patch))
        else:
            kubectl("-n", namespace, "rollout", "restart", f"deployment/{name}")
        rollout(f"deployment/{name}", namespace)
        pod = running_pod(namespace, f"app={name}")
        if [c["name"] for c in pod["spec"].get("initContainers", [])] == [INIT_CONTAINER]:
            return
    raise AssertionError(f"{name} was not injected after {attempts} attempts with the Operator ready")


def pods(namespace: str, selector: str) -> list[dict]:
    return get("-n", namespace, "pods", "-l", selector)["items"]


def running_pod(namespace: str, selector: str) -> dict:
    """The one Running pod of a workload that is not being deleted."""

    def find():
        items = [
            p
            for p in pods(namespace, selector)
            if p["status"].get("phase") == "Running"
            and not p["metadata"].get("deletionTimestamp")
            and all(s.get("ready") for s in p["status"].get("containerStatuses", []))
        ]
        return items[0] if len(items) == 1 else None

    return wait_until(find, timeout=180, message=f"one ready pod of {selector} in {namespace}")


# An image the kubelet cannot pull: it asks the registry at localhost, and none answers there.
UNPULLABLE_IMAGE = "localhost/impact-instrumentation/python:cannot-be-pulled"
PULL_FAILURES = {"ErrImagePull", "ImagePullBackOff"}


def waiting_reason(status: dict) -> str | None:
    return (status.get("state") or {}).get("waiting", {}).get("reason")


def instrumentation_image(language: str) -> str | None:
    """The image the Operator injects for `language` ("python" or "nodejs")."""
    return (get("-n", "impact-ai", "instrumentation", "impact")["spec"].get(language) or {}).get("image")


def set_instrumentation_image(language: str, image: str) -> None:
    patch = json.dumps({"spec": {language: {"image": image}}})
    kubectl("-n", "impact-ai", "patch", "instrumentation", "impact", "--type", "merge", "-p", patch)


def init_waits_for_its_image(pod: dict, init_container: str) -> bool:
    """The pod has not started its application, because the init container of that name cannot pull its image."""
    statuses = [s for s in pod["status"].get("initContainerStatuses", []) if s["name"] == init_container]
    return (
        pod["status"].get("phase") == "Pending" and len(statuses) == 1 and waiting_reason(statuses[0]) in PULL_FAILURES
    )


def call_app(namespace: str, deployment: str, provider: str, prompt: str = "hi there", pod: str | None = None) -> dict:
    """POST /chat/<provider> to the application from inside its own pod (a ready one of the Deployment, or `pod`)."""
    code = (
        "import json, urllib.request\n"
        f"req = urllib.request.Request('http://localhost:8000/chat/{provider}', "
        f"data=json.dumps({{'prompt': {prompt!r}}}).encode(), "
        "headers={'Content-Type': 'application/json'}, method='POST')\n"
        "print(urllib.request.urlopen(req, timeout=20).read().decode())\n"
    )
    target = f"pod/{pod}" if pod else f"deploy/{deployment}"
    out = kubectl("-n", namespace, "exec", target, "-c", "app", "--", "python", "-c", code, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


class Mock:
    """A mock service (testing/mock/server.py), reached through the API server's service proxy."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.base = f"/api/v1/namespaces/impact-mock/services/{name}:8080/proxy"

    def get(self, path: str):
        return json.loads(kubectl("get", "--raw", self.base + path).stdout)

    def post(self, path: str, body: dict) -> None:
        kubectl("create", "--raw", self.base + path, "-f", "-", input=json.dumps(body))

    def post_json(self, path: str, body: dict) -> dict:
        """POST and return the answer: the control calls that make something (a token)."""
        out = kubectl("create", "--raw", self.base + path, "-f", "-", input=json.dumps(body)).stdout
        return json.loads(out) if out.strip() else {}

    # --- the install tokens, keys and cover list the mock keeps (contracts C10 and C11) ---

    def new_token(self, **fields) -> dict:
        """A fresh install token: {"id", "token", "expires_at"}. Fields: product, expires_in (negative: already
        expired), state ("used", "revoked"), bind ({"uid": ..., "arn": ...}), auto_cover, cluster_name, env."""
        return self.post_json("/_tokens", {"env": "e2e", "product": PRODUCT, **fields})

    def token(self, **fields) -> str:
        return self.new_token(**fields)["token"]

    def installs(self) -> dict:
        """{"events": [...], "keys": [...], "tokens": [...]}: what impact.ai recorded of installs."""
        return self.get("/_installs")

    def exchanges(self, since: int = 0) -> list[dict]:
        return self.requests("/v1/installs/exchange", since)

    def revoke_keys(self) -> None:
        self.post("/_keys/revoke", {"all": True})

    def set_cover(self, workloads: list[dict], automatic: bool = True) -> None:
        self.post("/_cover", {"automatic": automatic, "workloads": workloads})

    def rules(self, rules: list[dict]) -> None:
        """Rules for the next requests, keeping the key the suite runs with (control() alone would drop it)."""
        self.control(rules, require_key=KEY)

    def requests(self, path: str | None = None, since: int = 0) -> list[dict]:
        """Requests the mock saw, oldest first; `since` is a `seq` already seen (only later requests are returned)."""
        query = "&".join(part for part in (f"path={path}" if path else "", f"since={since}" if since else "") if part)
        return self.get("/_requests" + (f"?{query}" if query else ""))

    def last_seq(self) -> int:
        return max((r["seq"] for r in self.requests()), default=0)

    def reset(self) -> None:
        self.post("/_reset", {})

    def control(self, rules: list[dict] | None = None, require_key: str | None = None) -> None:
        self.post("/_control", {"rules": rules or [], "require_key": require_key})

    @staticmethod
    def raw(record: dict) -> bytes:
        body = base64.b64decode(record["body_b64"])
        return gzip.decompress(body) if record["headers"].get("Content-Encoding") == "gzip" else body

    def inventories(self) -> list[dict]:
        """Accepted inventory snapshots, oldest first."""
        return [r["json"] for r in self.requests("/v1/inventory") if r.get("json") and r.get("status") == 202]

    def spans(self, path: str = "/v1/traces", since: int = 0) -> list[dict]:
        """Spans received (and accepted) on `path`, JSON or protobuf, with their resource attributes as `resource`."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        out = []
        for record in self.requests(path, since):
            if record.get("status") not in (200, 202):
                continue
            if record.get("json"):
                for resource_spans in record["json"]["resourceSpans"]:
                    resource = json_attributes(resource_spans["resource"]["attributes"])
                    for scope_spans in resource_spans["scopeSpans"]:
                        for span in scope_spans["spans"]:
                            out.append(
                                {
                                    "name": span["name"],
                                    "attrs": json_attributes(span.get("attributes", [])),
                                    "resource": resource,
                                }
                            )
                continue
            request = ExportTraceServiceRequest()
            request.ParseFromString(self.raw(record))
            for resource_spans in request.resource_spans:
                resource = {a.key: any_value(a.value) for a in resource_spans.resource.attributes}
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        out.append(
                            {
                                "name": span.name,
                                "kind": span.kind,
                                "attrs": {a.key: any_value(a.value) for a in span.attributes},
                                "resource": resource,
                            }
                        )
        return out

    def bodies(self) -> list[bytes]:
        return [self.raw(r) for r in self.requests()]


def any_value(value):
    which = value.WhichOneof("value")
    if which == "array_value":
        return [any_value(item) for item in value.array_value.values]
    return getattr(value, which) if which else None


def json_value(value: dict):
    ((kind, raw),) = value.items()
    if kind == "intValue":
        return int(raw)
    if kind == "arrayValue":
        return [json_value(item) for item in raw.get("values", [])]
    return raw


def json_attributes(items: list[dict]) -> dict:
    return {item["key"]: json_value(item["value"]) for item in items}


def kit_values(product: str = PRODUCT) -> list[str]:
    return [
        "--set-string", f"product.id={product}",
        # The mock ingest has no certificate: the chart takes an http endpoint only when told so on purpose.
        "--set-string", "ingest.endpoint=http://mock.impact-mock.svc:8080", "--set", "ingest.allowInsecureHttp=true",
        "--set-string", f"cluster.name={CLUSTER_NAME}",
        "--set-string", "cloud.provider=aws",
        "--set-string", "cloud.accountId=012345678901",
        "--set-string", "cloud.region=eu-west-1",
        "--set-string", f"cluster.arn={CLOUD_ARN}",
        "--set-file", "ingest.key=/dev/stdin",
        "--set", f"images.python.repository={IMAGE_PREFIX}/python", "--set", f"images.python.tag={TAG}",
        "--set", f"images.reporter.repository={IMAGE_PREFIX}/reporter", "--set", f"images.reporter.tag={TAG}",
        "--set", f"reporter.interval={INTERVAL}",
    ]  # fmt: skip


HELM_TIMEOUT = os.environ.get("E2E_HELM_TIMEOUT", "5m")


def helm_timeout_seconds() -> float:
    value = HELM_TIMEOUT.strip()
    return float(value[:-1]) * (60 if value.endswith("m") else 1) if value[-1] in "ms" else float(value)


def install(
    *,
    release: str = "impact-ai",
    namespace: str = "impact-ai",
    product: str = PRODUCT,
    key: str = KEY,
    extra: tuple[str, ...] = (),
    check: bool = True,
):
    """The command Cover your AI shows (helm upgrade --install, the key on standard input), against local images."""
    command = [
        "upgrade", "--install", release, str(CHART), "--namespace", namespace, "--create-namespace",
        "--wait", "--timeout", HELM_TIMEOUT, *kit_values(product), *extra,
    ]  # fmt: skip
    timeout = helm_timeout_seconds() + 120  # Helm's own timeout must fire first, or the release is left pending
    with operator_nudged():
        result = helm(*command, input=key + "\n", check=False, timeout=timeout)
        if result.returncode != 0 and "context deadline exceeded" in result.stderr:
            # Slow image pulls or a slow first start of the Operator: the release is "failed", its objects come up.
            result = helm(*command, input=key + "\n", check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(
            f"helm {' '.join(command)} failed ({result.returncode}):\n{result.stderr}\n{result.stdout}"
        )
    return result


def token_values(token: str, product: str = PRODUCT) -> list[str]:
    """The values of the command Cover your AI shows with AE-14: the one-time install token in place of the key."""
    values = kit_values(product)
    at = values.index("--set-file")  # ingest.key=/dev/stdin: nobody pastes a key any more
    del values[at : at + 2]
    return [*values, "--set-string", f"install.token={token}"]


def install_with_token(
    token: str,
    *,
    release: str = "impact-ai",
    namespace: str = "impact-ai",
    product: str = PRODUCT,
    extra: tuple[str, ...] = (),
    check: bool = True,
):
    """The command Cover your AI shows (helm upgrade --install with the install token), against local images."""
    command = [
        "upgrade", "--install", release, str(CHART), "--namespace", namespace, "--create-namespace",
        "--wait", "--timeout", HELM_TIMEOUT, *token_values(token, product), *extra,
    ]  # fmt: skip
    timeout = helm_timeout_seconds() + 120
    with operator_nudged():
        result = helm(*command, check=False, timeout=timeout)
        if result.returncode != 0 and "context deadline exceeded" in result.stderr:
            result = helm(*command, check=False, timeout=timeout)  # slow image pulls: the same command again is safe
    if check and result.returncode != 0:
        raise AssertionError(
            f"helm {' '.join(command[:6])} ... failed ({result.returncode}):\n{result.stderr}\n{result.stdout}"
        )
    return result


def exists(*args: str) -> bool:
    return kubectl("get", *args, check=False).returncode == 0
