# SPDX-License-Identifier: Apache-2.0

"""Automatic coverage, on the install test_7_exchange.py ends with.

impact.ai's list says which workloads are covered (the mock serves it as contract C11). The cover pod adds the injection
annotation to their pods as they are created, and the Operator injects. Proved here, on a kind cluster:

* putting a workload on the list restarts nothing and changes no workload object: the pods keep their UIDs, and
  generation and template of the Deployment, StatefulSet and DaemonSet stay as they were;
* the next rollout covers it, for every owner of pods (Deployment, StatefulSet, DaemonSet, Job, and the Job of a
  CronJob), with the init container exactly once, in either order of the two webhooks;
* opt-outs (a label or an annotation on the pods, a label on the namespace) and a customer's own annotation are left
  alone, and a workload that is not on the list is not touched;
* a covered application really traces, and the reporter reports the marker and the labels;
* the cover pod has no Kubernetes permission and mounts no token, and when it is down or impact.ai is, pods start
  normally;
* what automatic coverage cannot fail open on: when the kit's image cannot be pulled, or a cluster policy refuses the
  injected init container, the next rollout of a listed workload stalls with the old pods serving (pinned as it is,
  with the way out), because the pod the cover pod marked is one the cluster then does not take.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

import pytest

from kube import (
    KEY,
    MANIFESTS,
    PULL_FAILURES,
    UNPULLABLE_IMAGE,
    call_app,
    exists,
    get,
    helm,
    init_waits_for_its_image,
    install_with_token,
    instrumentation_image,
    kubectl,
    operator_ready_now,
    operator_stamp,
    pods,
    rollout,
    running_pod,
    set_instrumentation_image,
    wait_operator_ready,
    wait_until,
)

NAMESPACE = "impact-ai"
AUTO = "auto"
INIT = "opentelemetry-auto-instrumentation-python"
INJECT = "instrumentation.opentelemetry.io/inject-python"
MARKER = "impact.ai/auto-covered"
OPT_OUT = "impact.ai/opt-out"
OPERATOR_CERT = "impact-ai-operator-webhook-cert"
OPERATOR_WEBHOOKS = (
    ("mutatingwebhookconfiguration", "impact-ai-operator-mutation"),
    ("validatingwebhookconfiguration", "impact-ai-operator-validation"),
)
FAST_POLL = ("--set", "cover.pollInterval=5s")
LATE_ORDER = ("--set", "cover.webhookConfigName=impact-ai-zz-cover")  # sorts after the Operator's configuration


def entry(kind: str, name: str, namespace: str = AUTO, **extra) -> dict:
    return {"namespace": namespace, "kind": kind, "name": name, "lang": "python", **extra}


LISTED = [
    entry("Deployment", "web"),
    entry("StatefulSet", "db"),
    entry("DaemonSet", "agent"),
    entry("Job", "batch"),
    entry("CronJob", "nightly"),
    # Listed, and still not touched: the cover pod reads the opt-outs and the customer's annotation itself.
    entry("Deployment", "optout-label"),
    entry("Deployment", "optout-annotation"),
    entry("Deployment", "optout-ns", namespace="auto-optout"),
    entry("Deployment", "own-inject"),
]
WORKLOADS = {  # name -> (kind, selector) of the pods
    "web": ("deployment", "app=web"),
    "db": ("statefulset", "app=db"),
    "agent": ("daemonset", "app=agent"),
}


def pod_of(selector: str, namespace: str = AUTO) -> dict:
    return running_pod(namespace, selector)


def init_names(pod: dict) -> list[str]:
    return [c["name"] for c in pod["spec"].get("initContainers", [])]


def restart(kind: str, name: str, namespace: str = AUTO) -> None:
    kubectl("-n", namespace, "rollout", "restart", f"{kind}/{name}")


def settle(kind: str, name: str, namespace: str = AUTO) -> None:
    rollout(f"{kind}/{name}", namespace)


ATTEMPTS = 3


def asked_and_not_injected(pod: dict) -> bool:
    """The pod asks for injection (the cover pod's annotation, or the customer's own) and has no init container: the
    Operator was down or not yet serving when the pod was created, and its webhook fails open by design."""
    return INJECT in (pod["metadata"].get("annotations") or {}) and INIT not in init_names(pod)


def while_the_operator_serves(make, look) -> list[dict]:
    """Make new pods (`make`) and return them (`look`); once more if the Operator was restarted meanwhile and a pod it
    should have injected was not.

    On a host short of inotify instances the Operator restarts now and then, and a pod created in that moment starts
    uninjected, which is what failing open is for. An Operator that served throughout is not excused: its pods are
    returned as they are, for the test to judge, and so are the pods of the last attempt.
    """
    found: list[dict] = []
    for attempt in range(ATTEMPTS):
        if attempt or not operator_ready_now():
            wait_operator_ready()
        stamp = operator_stamp()
        make()
        found = look()
        if not any(asked_and_not_injected(pod) for pod in found) or operator_stamp() == stamp:
            break
    return found


def roll(targets: list[tuple[str, str, str]], namespace: str = AUTO) -> list[dict]:
    """Restart workloads, given as (kind, name, selector of their pods), and return their new pods."""

    def make() -> None:
        for kind, name, _ in targets:
            restart(kind, name, namespace)
        for kind, name, _ in targets:
            settle(kind, name, namespace)

    return while_the_operator_serves(make, lambda: [pod_of(selector, namespace) for _, _, selector in targets])


def wait_for_list(world, mark: int) -> None:
    """The cover pod has fetched a list newer than `mark` (a 200, not a 304)."""
    wait_until(
        lambda: [r for r in world.mock.requests("/v1/inventory/cover", mark) if r.get("status") == 200],
        timeout=90,
        message="the cover pod to fetch the new list",
    )
    time.sleep(7)  # one more poll interval, so that the webhook has certainly swapped it in


CONTAINER_NAMES = "instrumentation.opentelemetry.io/container-names"


def assert_covered_once(pod: dict, container: str | None = None) -> None:
    assert init_names(pod) == [INIT], f"{pod['metadata']['name']}: init containers {init_names(pod)}"
    annotations = pod["metadata"]["annotations"]
    assert annotations[INJECT] == "impact-ai/impact"
    covered_at = annotations[MARKER]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", covered_at), covered_at
    # the marker is also the label the uninstall command finds the pods by, and nothing else of the labels is the kit's
    assert pod["metadata"]["labels"][MARKER] == "true"
    assert [k for k in pod["metadata"]["labels"] if k.startswith("impact.ai/")] == [MARKER]
    datetime.fromisoformat(covered_at.replace("Z", "+00:00"))
    app = pod["spec"]["containers"][0]
    env = [e["name"] for e in app.get("env", [])]
    assert env.count("PYTHONPATH") == 1, env
    mounts = [m["mountPath"] for m in app.get("volumeMounts", [])]
    assert mounts.count("/otel-auto-instrumentation-python") == 1, mounts
    volumes = [v["name"] for v in pod["spec"]["volumes"]]
    assert volumes.count("opentelemetry-auto-instrumentation-python") == 1, volumes
    # What the cover pod added, and only that: the injection request, the container when the list names one, the marker.
    added = {INJECT, MARKER} | ({CONTAINER_NAMES} if container else set())
    assert set(annotations) - {"kubectl.kubernetes.io/restartedAt"} == added, annotations
    if container:
        assert annotations[CONTAINER_NAMES] == container


@pytest.fixture(scope="module")
def cluster(world):
    """The token install of test_7_exchange.py (made here when this file runs alone), the workloads of this file and
    of the application tests, and an empty list."""
    if not (
        exists("-n", NAMESPACE, "secret", "impact-ai-install-key")
        and exists("-n", NAMESPACE, "deployment", "impact-ai-cover")
    ):
        world.mock.reset()
        world.mock.control(require_key=KEY)
        install_with_token(world.mock.token(), extra=FAST_POLL)
    wait_operator_ready()
    # A cover pod with a clean start: whatever an earlier test did to the key, it asks with the one it holds now.
    restart("deployment", "impact-ai-cover", NAMESPACE)
    settle("deployment", "impact-ai-cover", NAMESPACE)
    world.deploy_workloads()
    kubectl("apply", "-f", str(MANIFESTS / "cover-workloads.yaml"))
    for name, (kind, _) in WORKLOADS.items():
        settle(kind, name)
    for name in ("optout-label", "optout-annotation", "own-inject", "unlisted"):
        settle("deployment", name)
    settle("deployment", "optout-ns", "auto-optout")
    world.mock.set_cover([])
    yield world
    # Left for test_8_uninstall.py, which removes everything.


# --- the list changes nothing that runs ---------------------------------------------------------------------------


def snapshot_of_state() -> dict:
    """Pods (uid, init containers) and workload objects (generation, template annotations) of the namespace."""
    state = {"pods": {}, "objects": {}}
    for pod in get("-n", AUTO, "pods")["items"]:
        state["pods"][pod["metadata"]["name"]] = (pod["metadata"]["uid"], tuple(init_names(pod)))
    for kind in ("deployments", "statefulsets", "daemonsets"):
        for item in get("-n", AUTO, kind)["items"]:
            state["objects"][item["metadata"]["name"]] = (
                item["metadata"]["generation"],
                json.dumps(item["spec"]["template"]["metadata"].get("annotations") or {}, sort_keys=True),
            )
    return state


def test_putting_workloads_on_the_list_restarts_nothing_and_changes_no_workload(cluster):
    before = snapshot_of_state()
    mark = cluster.mock.last_seq()
    cluster.mock.set_cover(LISTED)
    wait_for_list(cluster, mark)
    assert snapshot_of_state() == before, "the pods keep their UIDs and the workload objects do not change"
    for name, (_, selector) in WORKLOADS.items():
        assert init_names(pod_of(selector)) == [], f"{name} was injected while it ran"


# --- the next rollout covers them ---------------------------------------------------------------------------------


def roll_all() -> list[dict]:
    """Restart the Deployment, the StatefulSet and the DaemonSet; their new pods."""
    return roll([(kind, name, selector) for name, (kind, selector) in WORKLOADS.items()])


def test_the_next_rollout_covers_a_deployment_a_statefulset_and_a_daemonset_once_each(cluster):
    for pod in roll_all():
        assert_covered_once(pod)
    # The workload objects hold nothing the cover pod added: no drift for a GitOps tool to fight.
    for kind, name in (("deployment", "web"), ("statefulset", "db"), ("daemonset", "agent")):
        template = get("-n", AUTO, kind, name)["spec"]["template"]["metadata"].get("annotations") or {}
        assert set(template) <= {"kubectl.kubernetes.io/restartedAt"}, (name, template)


JOBS = (
    "batch",
    "nightly-29000001",
)  # the second is named as the CronJob controller names its Jobs: <cronjob>-<8 digits>


def test_a_job_and_the_job_of_a_cronjob_are_covered_as_theirs(cluster):
    def make() -> None:  # a Job's pods are made once: another attempt starts from new Jobs
        kubectl("-n", AUTO, "delete", "job", *JOBS, "--ignore-not-found", "--cascade=foreground", "--wait=true")
        image = "--image=localhost/impact-instrumentation/mock:dev"
        kubectl("-n", AUTO, "create", "job", "batch", image, "--", "sleep", "3600")
        kubectl("-n", AUTO, "create", "job", "--from=cronjob/nightly", JOBS[1])

    for pod in while_the_operator_serves(make, lambda: [pod_of(f"job-name={job}") for job in JOBS]):
        assert_covered_once(pod)


def test_opt_outs_the_customers_own_annotation_and_unlisted_workloads_are_left_alone(cluster):
    deployments = (
        ("optout-label", AUTO, "app=optout-label"),
        ("optout-annotation", AUTO, "app=optout-annotation"),
        ("optout-ns", "auto-optout", "app=optout-ns"),
        ("unlisted", AUTO, "app=unlisted"),
        ("own-inject", AUTO, "app=own-inject"),
    )

    def make() -> None:
        for name, namespace, _ in deployments:
            restart("deployment", name, namespace)
        for name, namespace, _ in deployments:
            settle("deployment", name, namespace)

    *left_alone, own = while_the_operator_serves(
        make, lambda: [pod_of(selector, namespace) for _, namespace, selector in deployments]
    )
    for pod in left_alone:
        assert init_names(pod) == [], pod["metadata"]["name"]
        assert MARKER not in (pod["metadata"].get("annotations") or {}), pod["metadata"]["name"]
    # The customer asked for it themselves: the Operator injects once, and the cover pod marks nothing.
    assert init_names(own) == [INIT] and MARKER not in own["metadata"]["annotations"]
    assert [m["mountPath"] for m in own["spec"]["containers"][0]["volumeMounts"]].count(
        "/otel-auto-instrumentation-python"
    ) == 1


# --- either order of the two webhooks -----------------------------------------------------------------------------


def webhook_names() -> list[str]:
    return sorted(
        m["metadata"]["name"]
        for m in get("mutatingwebhookconfiguration")["items"]
        if m["metadata"]["name"].startswith("impact-ai")
    )


def test_the_cover_pod_is_called_before_the_operator_and_the_pod_is_injected_once(cluster):
    # The default order, which test_..._covers_a_deployment... already proved by the init container count of 1.
    assert webhook_names() == ["impact-ai-cover", "impact-ai-operator-mutation"]
    operator_pod_webhook = next(
        w
        for w in get("mutatingwebhookconfiguration", "impact-ai-operator-mutation")["webhooks"]
        if w["name"] == "mpod.kb.io"
    )
    assert operator_pod_webhook["reinvocationPolicy"] == "IfNeeded"


def operator_ca_bundles() -> list[str]:
    """The CA bundle of every webhook in the Operator's two configurations."""
    return [
        webhook["clientConfig"]["caBundle"]
        for kind, name in OPERATOR_WEBHOOKS
        for webhook in get(kind, name)["webhooks"]
    ]


def test_with_the_operator_called_first_a_reinvocation_still_injects_exactly_once(cluster):
    before_cover = pods(NAMESPACE, "app.kubernetes.io/name=cover")[0]["metadata"]["uid"]
    before_cert = get("-n", NAMESPACE, "secret", "impact-ai-cover-cert")["data"]
    before_operator = get("-n", NAMESPACE, "secret", OPERATOR_CERT)["data"]
    assert set(operator_ca_bundles()) == {before_operator["ca.crt"]}, "the Operator's CA bundles fit its certificate"
    mark = cluster.mock.last_seq()
    result = install_with_token(_token_of_the_install(cluster), extra=FAST_POLL + LATE_ORDER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert webhook_names() == ["impact-ai-operator-mutation", "impact-ai-zz-cover"], "the Operator is now called first"
    assert cluster.mock.exchanges(mark) == [], "the same token is not exchanged again"
    # An upgrade reuses the certificates it made, so the cover pod did not restart and the CA bundles still fit them.
    assert get("-n", NAMESPACE, "secret", "impact-ai-cover-cert")["data"] == before_cert
    assert pods(NAMESPACE, "app.kubernetes.io/name=cover")[0]["metadata"]["uid"] == before_cover
    ca = get("mutatingwebhookconfiguration", "impact-ai-zz-cover")["webhooks"][0]["clientConfig"]["caBundle"]
    assert ca == before_cert["ca.crt"]
    # The same for the Operator's own webhooks: a new certificate on every upgrade left the Operator's pod serving the
    # old one for the minute the kubelet needs to hand over the new Secret, and pods created then started uninjected.
    assert get("-n", NAMESPACE, "secret", OPERATOR_CERT)["data"] == before_operator
    assert set(operator_ca_bundles()) == {before_operator["ca.crt"]}

    time.sleep(7)
    for pod in roll_all():
        assert_covered_once(pod)


def _token_of_the_install(world) -> str:
    """The token the install ran with: the Helm release has it as a value (as the chart documents)."""
    values = json.loads(helm("get", "values", "impact-ai", "-n", NAMESPACE, "-o", "json").stdout)
    return values["install"]["token"]


# --- a covered application traces; the reporter says what it found --------------------------------------------


def test_a_covered_application_traces_without_a_change_to_its_deployment(cluster):
    world = cluster
    template = get("-n", "shop", "deployment", "support-assistant")["spec"]["template"]["metadata"]
    assert INJECT not in (template.get("annotations") or {}), "nobody annotated the application"
    mark = world.mock.last_seq()
    world.mock.set_cover([*LISTED, entry("Deployment", "support-assistant", namespace="shop", container="app")])
    wait_for_list(world, mark)
    (pod,) = roll([("deployment", "support-assistant", "app=support-assistant")], "shop")
    assert_covered_once(pod, container="app")
    assert pod["spec"]["containers"][0]["name"] == "app"
    template = get("-n", "shop", "deployment", "support-assistant")["spec"]["template"]["metadata"]
    assert set(template.get("annotations") or {}) <= {"kubectl.kubernetes.io/restartedAt"}, (
        "the Deployment is as it was"
    )

    assert call_app("shop", "support-assistant", "openai") == {"answer": "Hello from the stub."}

    def spans():
        return [
            s
            for s in world.mock.spans(since=mark)
            if s["resource"].get("k8s.deployment.name") == "support-assistant" and "gen_ai.request.model" in s["attrs"]
        ]

    found = wait_until(spans, timeout=120, message="an LLM span of the covered application")
    assert found[0]["resource"]["k8s.cluster.uid"] == world.cluster_uid
    assert found[0]["resource"]["k8s.namespace.name"] == "shop"


def latest(world, namespace: str, kind: str, name: str):
    for snapshot in reversed(world.mock.inventories()):
        for w in snapshot["workloads"]:
            if (w["namespace"], w["kind"], w["name"]) == (namespace, kind, name):
                return w
    return None


def test_the_snapshot_carries_the_marker_and_the_labels_of_the_workloads(cluster):
    world = cluster

    def reported():
        web = latest(world, AUTO, "Deployment", "web")
        label = latest(world, AUTO, "Deployment", "optout-label")
        ns = latest(world, "auto-optout", "Deployment", "optout-ns")
        ok = web and web.get("autoCovered") and label and label.get("labels") and ns and ns.get("namespaceLabels")
        return (web, label, ns) if ok else None

    web, label, ns = wait_until(reported, timeout=90, message="a snapshot with the marker and the labels")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", web["autoCovered"])
    assert web["containers"][0]["injected"]["python"] is True
    assert web["annotations"] == {} or INJECT not in web["annotations"], (
        "the workload object itself holds no annotation"
    )
    assert label["labels"] == {OPT_OUT: "true"} and "autoCovered" not in label
    assert ns["namespaceLabels"] == {OPT_OUT: "true"}
    unlisted = latest(world, AUTO, "Deployment", "unlisted")
    assert not unlisted.get("autoCovered") and not unlisted.get("labels")
    # Names and the impact.ai/* labels only: nothing else of a label leaves the cluster.
    assert all(
        key.startswith("impact.ai/")
        for w in (web, label, ns)
        for key in {**w.get("labels", {}), **w.get("namespaceLabels", {})}
    )


# --- no permission, no token -----------------------------------------------------------------------------------


def test_the_cover_pod_has_no_kubernetes_permission_and_no_token(cluster):
    account = "system:serviceaccount:impact-ai:impact-ai-cover"
    asks = (
        ("get", "pods"), ("list", "pods"), ("patch", "pods"), ("get", "secrets"), ("list", "namespaces"),
        ("patch", "deployments.apps"), ("create", "pods"),
    )  # fmt: skip
    for verb, resource in asks:
        answer = kubectl("auth", "can-i", verb, resource, "--as", account, "-n", AUTO, check=False)
        assert answer.stdout.strip() == "no", (verb, resource)
    pod = pods(NAMESPACE, "app.kubernetes.io/name=cover")[0]
    assert pod["spec"]["automountServiceAccountToken"] is False
    mounted = [m["mountPath"] for c in pod["spec"]["containers"] for m in c.get("volumeMounts", [])]
    assert not [m for m in mounted if "serviceaccount" in m], mounted
    assert not [v for v in pod["spec"]["volumes"] if "projected" in v]


# --- fail open -------------------------------------------------------------------------------------------------


def test_with_the_cover_pod_down_pods_start_normally_and_uninjected(cluster):
    world = cluster
    kubectl("-n", NAMESPACE, "scale", "deployment/impact-ai-cover", "--replicas=0")
    wait_until(lambda: not pods(NAMESPACE, "app.kubernetes.io/name=cover"), timeout=90, message="the cover pod to go")
    try:
        started = time.monotonic()
        restart("deployment", "web")
        settle("deployment", "web")
        pod = pod_of("app=web")
        assert time.monotonic() - started < 90, "a pod create is not held up by a webhook that is down"
        assert init_names(pod) == [] and MARKER not in (pod["metadata"].get("annotations") or {})
    finally:
        mark = world.mock.last_seq()
        kubectl("-n", NAMESPACE, "scale", "deployment/impact-ai-cover", "--replicas=1")
        rollout("deployment/impact-ai-cover", NAMESPACE)
    wait_for_list(world, mark)  # the new cover pod has its list again
    wait_operator_ready()


def test_with_impact_ai_failing_the_cover_pod_keeps_the_last_list_and_nothing_waits_for_it(cluster):
    # impact.ai answers 503 to every poll for a while (the mock keeps its state, so the cluster's key stays valid for
    # what follows). The cover pod keeps the list it has: a pod is covered all the same.
    world = cluster
    world.mock.rules([{"path": "/v1/inventory/cover", "status": 503, "retry_after": 1}])
    try:
        time.sleep(12)  # a few polls, all refused

        def make() -> None:
            started = time.monotonic()
            restart("statefulset", "db")
            settle("statefulset", "db")
            assert time.monotonic() - started < 120, "nothing waits for impact.ai"

        (pod,) = while_the_operator_serves(make, lambda: [pod_of("app=db")])
        assert_covered_once(pod)  # the list it had is still applied
    finally:
        world.mock.rules([])
    refused = [r for r in world.mock.requests("/v1/inventory/cover") if r.get("status") == 503]
    assert refused, "the polls were refused while the pod was restarted"


# --- what automatic coverage does not fail open on ------------------------------------------------------------------
#
# The cover pod only adds an annotation, and the Operator only adds an init container. If the kubelet cannot pull that
# init container's image, or the cluster's own admission policy refuses it, the pod of the next rollout of a listed
# workload is the one that does not start. Nobody ran a command, so nobody watches. What is pinned: the rollout stalls,
# the pods that run are not touched, and a rollout started again once the cause is gone covers the workload.


def one_pod_per_uid(selector: str) -> dict[str, dict]:
    return {p["metadata"]["uid"]: p for p in pods(AUTO, selector) if not p["metadata"].get("deletionTimestamp")}


def test_when_the_kit_image_cannot_be_pulled_the_next_rollout_of_a_listed_workload_stalls_and_the_old_pod_serves(
    cluster,
):
    wait_operator_ready()
    old = pod_of("app=web")
    image = instrumentation_image("python")
    set_instrumentation_image("python", UNPULLABLE_IMAGE)
    try:
        restart("deployment", "web")

        def covered_and_waiting():
            fresh = [p for uid, p in one_pod_per_uid("app=web").items() if uid != old["metadata"]["uid"]]
            return fresh[0] if len(fresh) == 1 and init_waits_for_its_image(fresh[0], INIT) else None

        stuck = wait_until(covered_and_waiting, timeout=150, message="the new pod of web to wait for the kit's image")
        # the cover pod marked it and the Operator injected it, with the image nobody can pull
        assert MARKER in stuck["metadata"]["annotations"] and init_names(stuck) == [INIT]
        assert stuck["spec"]["initContainers"][0]["image"] == UNPULLABLE_IMAGE
        time.sleep(10)
        # the rollout is stalled where it is: one pod is new and waits, the old one runs, never restarted
        deployment = get("-n", AUTO, "deployment", "web")["status"]
        assert deployment.get("updatedReplicas") == 1 and deployment.get("unavailableReplicas") == 1, deployment
        (still,) = [p for uid, p in one_pod_per_uid("app=web").items() if uid == old["metadata"]["uid"]]
        assert still["status"]["phase"] == "Running" and still["status"]["containerStatuses"][0]["ready"] is True
        assert still["status"]["containerStatuses"][0]["restartCount"] == 0
        # The reporter names the reason in its next snapshot (the suite's cycle is 10 s), reasons only: the init
        # container waits for the pull, and nothing failed to create a pod.
        stall = wait_until(
            lambda: (latest(cluster, AUTO, "Deployment", "web") or {}).get("stall"),
            timeout=45,
            interval=2,
            message="a snapshot that says why the pods of web wait",
        )
        assert stall.get("initWaiting") and set(stall["initWaiting"]) <= PULL_FAILURES, stall
        assert "replicaFailure" not in stall, stall
    finally:
        set_instrumentation_image("python", image)
    # The way out once the image can be pulled: the rollout is started again, and it covers the workload.
    (pod,) = roll([("deployment", "web", "app=web")])
    assert_covered_once(pod)
    # And the snapshots stop saying that the workload is stalled.
    wait_until(
        lambda: "stall" not in (latest(cluster, AUTO, "Deployment", "web") or {"stall": True}),
        timeout=45,
        interval=2,
        message="the snapshot to stop reporting a stall",
    )


PLANTED_BY_THE_POLICY = (
    "sk-live-abcdefghijklmnop"  # in the message of the policy; the check of internal details allows it
)


def policy_refuses() -> bool:
    """A pod with the injected init container is refused in `auto` (the policy takes a few seconds to take effect)."""
    probe = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "policy-probe", "namespace": AUTO},
        "spec": {
            "initContainers": [{"name": INIT, "image": "localhost/impact-instrumentation/mock:dev"}],
            "containers": [{"name": "c", "image": "localhost/impact-instrumentation/mock:dev"}],
        },
    }
    result = kubectl("create", "--dry-run=server", "-f", "-", input=json.dumps(probe), check=False)
    return result.returncode != 0 and "e2e-refuse-kit-init-container" in result.stderr


def policy_is_served() -> bool:
    return kubectl("get", "validatingadmissionpolicies.admissionregistration.k8s.io", check=False).returncode == 0


def test_when_a_policy_refuses_the_injected_init_container_the_next_rollout_stalls_and_the_old_pod_serves(cluster):
    if not policy_is_served():
        pytest.skip("ValidatingAdmissionPolicy is served from Kubernetes 1.30")
    wait_operator_ready()
    old = pod_of("app=web")
    old_pod_name = old["metadata"]["name"]
    policy = str(MANIFESTS / "refuse-kit-init-container.yaml")
    kubectl("apply", "-f", policy)
    try:
        wait_until(policy_refuses, timeout=60, interval=2, message="the policy to refuse the init container")
        restart("deployment", "web")

        def replica_failure():
            conditions = {c["type"]: c for c in get("-n", AUTO, "deployment", "web")["status"].get("conditions", [])}
            failure = conditions.get("ReplicaFailure")
            return failure if failure and failure["status"] == "True" else None

        failure = wait_until(replica_failure, timeout=120, message="the Deployment to say its pods cannot be created")
        assert failure["reason"] == "FailedCreate"
        assert "e2e-refuse-kit-init-container" in failure["message"], failure
        # the rollout is stalled where it is: no new pod, the old one runs, never restarted
        current = one_pod_per_uid("app=web")
        assert list(current) == [old["metadata"]["uid"]], "the refused pods were never made"
        status = current[old["metadata"]["uid"]]["status"]
        assert status["phase"] == "Running" and status["containerStatuses"][0]["restartCount"] == 0
        assert get("-n", AUTO, "deployment", "web")["status"].get("availableReplicas") == 1
        # The reporter names the reason of the Deployment's condition in its next snapshot, and nothing it was told with
        # it: the policy's name and words, the value its message carries, the pod's name are in no snapshot it sent.
        stall = wait_until(
            lambda: (latest(cluster, AUTO, "Deployment", "web") or {}).get("stall"),
            timeout=45,
            interval=2,
            message="a snapshot that says why the pods of web cannot be created",
        )
        assert stall.get("replicaFailure") == "FailedCreate" and not stall.get("initWaiting"), stall
        sent = json.dumps([r.get("json") for r in cluster.mock.requests("/v1/inventory")])
        for planted in (PLANTED_BY_THE_POLICY, "e2e-refuse-kit-init-container", "this cluster refuses", old_pod_name):
            assert planted not in sent, f"{planted!r} left the cluster in a snapshot"
    finally:
        kubectl("delete", "-f", policy, "--ignore-not-found", "--wait=true")
        wait_until(lambda: not policy_refuses(), timeout=60, interval=2, message="the policy to let it through again")
    # The way out once the policy lets the init container through (or the workload is opted out): roll it again.
    (pod,) = roll([("deployment", "web", "app=web")])
    assert_covered_once(pod)
