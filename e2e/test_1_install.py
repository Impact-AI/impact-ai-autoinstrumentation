# SPDX-License-Identifier: Apache-2.0

"""Install, and no secret leaves the cluster."""

from __future__ import annotations

import json

import pytest

from kube import (
    CLOUD_IN_INVENTORY,
    CLUSTER_NAME,
    IMAGE_PREFIX,
    PLANTED,
    SYSTEM_NAMESPACES,
    TAG,
    get,
    helm,
    install,
    kubectl,
    wait_until,
)

EXPECTED = {
    ("shop", "Deployment", "support-assistant"),
    ("shop", "Deployment", "legacy-tracer"),
    ("shop", "Deployment", "own-otel"),
    ("shop", "Deployment", "refund-worker"),
    ("shop", "StatefulSet", "vector-db"),
    ("edge", "DaemonSet", "node-agent"),
    ("edge", "CronJob", "nightly-report"),
    ("edge", "Job", "migrate"),
    ("edge", "Pod", "debug-shell"),
}


def latest_with(world, wanted):
    for snapshot in reversed(world.mock.inventories()):
        have = {(w["namespace"], w["kind"], w["name"]) for w in snapshot["workloads"]}
        if wanted <= have:
            return snapshot
    return None


def workload(snapshot, namespace, kind, name):
    return next(w for w in snapshot["workloads"] if (w["namespace"], w["kind"], w["name"]) == (namespace, kind, name))


@pytest.fixture(scope="module")
def snapshot(world):
    return wait_until(lambda: latest_with(world, EXPECTED), timeout=240, message="a snapshot with every workload")


def test_install_with_the_command_the_page_shows(world):
    result = install()
    assert result.returncode == 0, result.stderr
    world.deploy_workloads()  # after the install: the first snapshot after this lists them, within a cycle
    names = {d["metadata"]["name"] for d in get("-n", "impact-ai", "deployments")["items"]}
    assert names == {"impact-ai-operator", "impact-ai-gateway", "impact-ai-reporter"}
    instrumentation = get("-n", "impact-ai", "instrumentation", "impact")
    assert instrumentation["spec"]["python"]["image"] == f"{IMAGE_PREFIX}/python:{TAG}"


def test_every_workload_outside_the_system_namespaces_appears_with_the_clusters_id(world, snapshot):
    assert snapshot["schema"] == "impact.inventory.v1"
    assert snapshot["cluster"]["uid"] == world.cluster_uid  # the UID of kube-system
    assert snapshot["cluster"]["name"] == CLUSTER_NAME
    assert snapshot["cluster"]["kubernetesVersion"].startswith("1.")
    # The cloud identity of the install command; kind's nodes carry no region label, so `region` is the given one.
    assert {k: snapshot["cluster"].get(k) for k in CLOUD_IN_INVENTORY} == CLOUD_IN_INVENTORY
    assert snapshot["reporter"]["mode"] == "inject"
    assert snapshot["reporter"]["clusterOwner"] is None
    assert {w["namespace"] for w in snapshot["workloads"]}.isdisjoint(SYSTEM_NAMESPACES)
    have = {(w["namespace"], w["kind"], w["name"]) for w in snapshot["workloads"]}
    assert EXPECTED <= have
    assert workload(snapshot, "shop", "Deployment", "refund-worker")["pods"]["desired"] == 2
    assert workload(snapshot, "edge", "CronJob", "nightly-report")["containers"][0]["name"] == "report"


def test_the_webhook_fails_open_and_skips_the_system_namespaces():
    mutating = get("mutatingwebhookconfiguration", "impact-ai-operator-mutation")
    pod = next(w for w in mutating["webhooks"] if w["name"] == "mpod.kb.io")
    assert pod["failurePolicy"] == "Ignore"
    assert pod["timeoutSeconds"] == 5
    (expression,) = pod["namespaceSelector"]["matchExpressions"]
    assert {"kube-system", "kube-public", "kube-node-lease", "impact-ai"} <= set(expression["values"])
    for configuration in (mutating, get("validatingwebhookconfiguration", "impact-ai-operator-validation")):
        assert {w["failurePolicy"] for w in configuration["webhooks"]} == {"Ignore"}


def test_helm_and_the_api_server_agree_on_the_release():
    status = json.loads(helm("status", "impact-ai", "-n", "impact-ai", "-o", "json").stdout)
    assert status["info"]["status"] == "deployed"
    crd = get("crd", "instrumentations.opentelemetry.io")
    assert crd["spec"]["group"] == "opentelemetry.io"


def test_the_snapshot_holds_names_and_executables_never_values(world, snapshot):
    assistant = workload(snapshot, "shop", "Deployment", "support-assistant")
    (container,) = assistant["containers"]
    assert container["executables"] == ["python"]
    assert {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "STUB_URL"} <= set(container["envNames"])
    assert {"envName": "ANTHROPIC_API_KEY", "secret": "llm-keys", "key": "anthropic"} in container["secretRefs"]
    assert {"kind": "Secret", "name": "openai-credentials"} in container["envFrom"]
    # Annotations only from the allowlist: the last-applied-configuration annotation (which holds the manifest and
    # the planted values) is not among them.
    allowed = (
        "instrumentation.opentelemetry.io/",
        "impact.ai/",
        "admission.datadoghq.com/",
        "cloudwatch.aws.amazon.com/",
    )
    assert all(
        key.startswith(allowed) or key.endswith("dynatrace.com") or "dynatrace.com/" in key
        for key in assistant["annotations"]
    )
    assert "kubectl.kubernetes.io/last-applied-configuration" not in json.dumps(snapshot)


def test_no_planted_value_reached_impact_ai(world, snapshot):
    deployed = get("-n", "shop", "deployment", "support-assistant")
    # the planted values really are in the cluster, in the places the definition lists
    assert PLANTED[0] in deployed["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"]
    leaked = [p for body in world.mock.bodies() for p in PLANTED if p.encode() in body]
    assert leaked == []


def test_the_reporter_cannot_read_secrets_or_configmaps():
    account = "system:serviceaccount:impact-ai:impact-ai-reporter"

    def can(verb, resource, namespace="shop"):
        result = kubectl("auth", "can-i", verb, resource, "--as", account, "-n", namespace, check=False)
        return result.stdout.strip() == "yes"

    for resource in ("secrets", "configmaps"):
        assert not can("get", resource) and not can("list", resource) and not can("watch", resource)
    assert not can("create", "deployments.apps") and not can("delete", "pods")
    for resource in (
        "deployments.apps",
        "statefulsets.apps",
        "daemonsets.apps",
        "pods",
        "cronjobs.batch",
        "jobs.batch",
    ):
        assert can("list", resource)
    assert can("get", "namespaces", namespace="")
