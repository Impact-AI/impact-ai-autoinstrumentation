# SPDX-License-Identifier: Apache-2.0

"""Fail open: the application does not depend on impact.ai.

One condition holds up new pods and is pinned here as it is: the image the Operator injects cannot be pulled.
"""

from __future__ import annotations

import json
import time

from conftest import World
from kube import (
    INIT_CONTAINER,
    KEY,
    PULL_FAILURES,
    UNPULLABLE_IMAGE,
    call_app,
    cover,
    get,
    init_waits_for_its_image,
    instrumentation_image,
    kubectl,
    pods,
    rollout,
    running_pod,
    set_instrumentation_image,
    wait_operator_ready,
    wait_until,
    waiting_reason,
)

INJECT = "instrumentation.opentelemetry.io/inject-python"
ANSWER = {"answer": "Hello from the stub."}
UNPULLABLE = UNPULLABLE_IMAGE


def serve_normally(deployment: str = "support-assistant", rounds: int = 5) -> float:
    slowest = 0.0
    for _ in range(rounds):
        for provider in ("openai", "anthropic", "bedrock"):
            started = time.monotonic()
            assert call_app("shop", deployment, provider) == ANSWER
            slowest = max(slowest, time.monotonic() - started)
    return slowest


def restarts(namespace: str, selector: str) -> int:
    return sum(
        s["restartCount"]
        for p in get("-n", namespace, "pods", "-l", selector)["items"]
        for s in p["status"]["containerStatuses"]
    )


def test_a_revoked_key_does_not_touch_the_workload(world: World):
    wait_operator_ready()
    world.mock.control(require_key="the-key-that-was-revoked")
    try:
        assert serve_normally(rounds=2) < 15
        rejected = wait_until(
            lambda: (
                {r["path"] for r in world.mock.requests() if r.get("status") == 401} >= {"/v1/inventory", "/v1/traces"}
            ),
            timeout=120,
            message="401 receipts for both routes",
        )
        assert rejected
    finally:
        world.mock.control(require_key=KEY)
    assert restarts("shop", "app=support-assistant") == 0
    assert restarts("impact-ai", "app.kubernetes.io/name=reporter") == 0


def test_ingest_down_does_not_touch_the_workload(world: World):
    # The model providers are served by `theirs`, so taking `mock` away takes only impact.ai's ingest away.
    kubectl("-n", "impact-mock", "scale", "deployment/mock", "--replicas=0")
    wait_until(lambda: not get("-n", "impact-mock", "pods", "-l", "app=mock")["items"], timeout=90, message="mock gone")
    try:
        assert serve_normally(rounds=3) < 10
        time.sleep(25)  # a few reporter cycles and trace exports, all refused
        assert serve_normally(rounds=1) < 10
        assert restarts("impact-ai", "app.kubernetes.io/name=reporter") == 0
        assert restarts("impact-ai", "app.kubernetes.io/name=gateway") == 0
        assert restarts("shop", "app=support-assistant") == 0
    finally:
        kubectl("-n", "impact-mock", "scale", "deployment/mock", "--replicas=1")
        rollout("deployment/mock", "impact-mock")
        world.mock.control(require_key=KEY)


def test_the_operator_down_new_pods_of_an_annotated_workload_start_uninjected(world: World):
    kubectl("-n", "impact-ai", "scale", "deployment/impact-ai-operator", "--replicas=0")
    wait_until(
        lambda: not get("-n", "impact-ai", "pods", "-l", "app.kubernetes.io/component=controller-manager")["items"],
        timeout=90,
        message="operator gone",
    )
    try:
        started = time.monotonic()
        kubectl("-n", "shop", "rollout", "restart", "deployment/support-assistant")
        rollout("deployment/support-assistant", "shop")
        assert time.monotonic() - started < 120
        pod = running_pod("shop", "app=support-assistant")
        assert not pod["spec"].get("initContainers")  # started, uninjected
        template = get("-n", "shop", "deployment", "support-assistant")["spec"]["template"]["metadata"]["annotations"]
        assert template[INJECT] == "impact-ai/impact"  # still annotated
        assert call_app("shop", "support-assistant", "openai") == ANSWER  # and it serves

        def row_says_so():
            for snapshot in reversed(world.mock.inventories()):
                for w in snapshot["workloads"]:
                    if w["name"] == "support-assistant" and w["kind"] == "Deployment":
                        return (
                            w if (INJECT in w["annotations"] and not w["containers"][0]["injected"]["python"]) else None
                        )
            return None

        wait_until(row_says_so, timeout=90, message="a snapshot showing the annotation without the injection")
    finally:
        kubectl("-n", "impact-ai", "scale", "deployment/impact-ai-operator", "--replicas=1")
    # And once the Operator is back, the next pod is injected again.
    cover("support-assistant")


def test_the_injected_image_cannot_be_pulled_new_pods_wait_in_init_and_running_pods_are_untouched(world: World):
    # What the Operator injects is an init container with the python image. If the kubelet cannot pull that image (the
    # registry is unreachable or has dropped it, a mirror is closed to the workload), a new pod of a covered workload
    # does not start its application until the pull works: webhook failures fail open, this one does not. What is
    # pinned: the new pod waits in Init, the pod that runs is not touched, and the application keeps serving.
    wait_operator_ready()
    selector = "app=support-assistant"
    running = running_pod("shop", selector)
    image = instrumentation_image("python")

    def set_image(value: str) -> None:
        set_instrumentation_image("python", value)

    set_image(UNPULLABLE)
    try:
        kubectl("-n", "shop", "scale", "deployment/support-assistant", "--replicas=2")  # a scale-out: one new pod

        def new_pod_waits():
            fresh = [p for p in pods("shop", selector) if p["metadata"]["uid"] != running["metadata"]["uid"]]
            if len(fresh) != 1:
                return None
            statuses = fresh[0]["status"].get("initContainerStatuses", [])
            return fresh[0] if [waiting_reason(s) in PULL_FAILURES for s in statuses] == [True] else None

        waiting = wait_until(new_pod_waits, timeout=120, message="the new pod to wait for the image")
        assert [c["name"] for c in waiting["spec"]["initContainers"]] == [INIT_CONTAINER]  # it was injected
        assert waiting["spec"]["initContainers"][0]["image"] == UNPULLABLE
        time.sleep(15)  # and it stays that way: the application container has not started
        (same,) = [p for p in pods("shop", selector) if p["metadata"]["uid"] == waiting["metadata"]["uid"]]
        assert same["status"]["phase"] == "Pending"
        assert [waiting_reason(s) for s in same["status"]["containerStatuses"]] == ["PodInitializing"]
        assert waiting_reason(same["status"]["initContainerStatuses"][0]) in PULL_FAILURES

        # The pod that was running is the same pod, still ready, never restarted, and the application serves.
        (still,) = [p for p in pods("shop", selector) if p["metadata"]["uid"] == running["metadata"]["uid"]]
        assert not still["metadata"].get("deletionTimestamp") and still["status"]["phase"] == "Running"
        assert all(s["ready"] and s["restartCount"] == 0 for s in still["status"]["containerStatuses"])
        for provider in ("openai", "anthropic", "bedrock"):
            assert call_app("shop", "support-assistant", provider, pod=still["metadata"]["name"]) == ANSWER
        assert get("-n", "shop", "deployment", "support-assistant")["status"].get("readyReplicas") == 1
    finally:
        set_image(image)
        kubectl("-n", "shop", "scale", "deployment/support-assistant", "--replicas=1")
    # The waiting pod is the one that goes; the running one stays.
    wait_until(lambda: len(pods("shop", selector)) == 1, timeout=120, message="the waiting pod to be removed")
    (left,) = pods("shop", selector)
    assert left["metadata"]["uid"] == running["metadata"]["uid"]


NODEJS_INIT = "opentelemetry-auto-instrumentation-nodejs"
INJECT_NODEJS = "instrumentation.opentelemetry.io/inject-nodejs"


def test_the_injected_nodejs_image_cannot_be_pulled_either_the_new_pod_waits_and_the_old_one_runs(world: World):
    # The Node.js image has the same init container, the same copy and the same pull as the Python one: a workload
    # annotated inject-nodejs waits in Init when its image cannot be pulled, and the pod that runs is not touched. A
    # throwaway Deployment stands for the workload; the image the Operator injects is pointed at one nobody can pull
    # before the annotation is added, so that the rollout it starts is the one that stalls.
    wait_operator_ready()
    kubectl("-n", "shop", "delete", "deployment", "node-probe", "--ignore-not-found", "--wait=true")
    kubectl("-n", "shop", "create", "deployment", "node-probe", "--image=localhost/impact-instrumentation/mock:dev",
            "--", "sleep", "infinity")  # fmt: skip
    rollout("deployment/node-probe", "shop")
    selector = "app=node-probe"
    running = running_pod("shop", selector)
    original = instrumentation_image("nodejs")
    set_instrumentation_image("nodejs", "localhost/impact-instrumentation/nodejs:cannot-be-pulled")
    try:
        patch = {"spec": {"template": {"metadata": {"annotations": {INJECT_NODEJS: "impact-ai/impact"}}}}}
        kubectl("-n", "shop", "patch", "deployment", "node-probe", "--type", "merge", "-p", json.dumps(patch))

        def new_pod_waits():
            fresh = [p for p in pods("shop", selector) if p["metadata"]["uid"] != running["metadata"]["uid"]]
            return fresh[0] if len(fresh) == 1 and init_waits_for_its_image(fresh[0], NODEJS_INIT) else None

        waiting = wait_until(new_pod_waits, timeout=120, message="the new pod to wait for the Node.js image")
        assert [c["name"] for c in waiting["spec"]["initContainers"]] == [NODEJS_INIT]  # it was injected
        time.sleep(10)  # and it stays that way
        (still,) = [p for p in pods("shop", selector) if p["metadata"]["uid"] == running["metadata"]["uid"]]
        assert still["status"]["phase"] == "Running" and not still["metadata"].get("deletionTimestamp")
        assert all(s["ready"] and s["restartCount"] == 0 for s in still["status"]["containerStatuses"])
        (again,) = [p for p in pods("shop", selector) if p["metadata"]["uid"] == waiting["metadata"]["uid"]]
        assert init_waits_for_its_image(again, NODEJS_INIT)
        assert get("-n", "shop", "deployment", "node-probe")["status"].get("readyReplicas") == 1
    finally:
        set_instrumentation_image("nodejs", original)
        kubectl("-n", "shop", "delete", "deployment", "node-probe", "--ignore-not-found", "--wait=true")
