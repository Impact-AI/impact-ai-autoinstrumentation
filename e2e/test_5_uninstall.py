# SPDX-License-Identifier: Apache-2.0

"""AE-8: the shown uninstall command leaves nothing, and the cluster stops reporting at once."""

from __future__ import annotations

import json
import time

import pytest

from kube import (
    CLOUD_IN_INVENTORY,
    CONTEXT,
    INTERVAL,
    UNINSTALL_SCRIPT,
    get,
    helm,
    kubectl,
    rollout,
    run,
    running_pod,
)

INJECT = "instrumentation.opentelemetry.io/inject-python"


def exists(*args: str) -> bool:
    return kubectl("get", *args, check=False).returncode == 0


@pytest.fixture(scope="module")
def before(world):
    """The snapshots the old reporter sent, taken just before the uninstall."""
    return {"count": len(world.mock.requests("/v1/inventory")), "inventories": world.mock.inventories()}


@pytest.fixture(scope="module")
def uninstalled(world, before):
    if CONTEXT:
        kubectl("config", "use-context", CONTEXT)
    result = run(["sh", str(UNINSTALL_SCRIPT)], timeout=900, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_ae8_the_release_the_kit_and_its_webhooks_are_gone(world, uninstalled):
    assert json.loads(helm("list", "--all-namespaces", "-o", "json").stdout) == []
    assert not exists("namespace", "impact-ai")
    for kind, name in [
        ("mutatingwebhookconfiguration", "impact-ai-operator-mutation"),
        ("validatingwebhookconfiguration", "impact-ai-operator-validation"),
        ("clusterrole", "impact-ai-kit-reporter"),
        ("clusterrole", "impact-ai-operator-manager"),
        ("clusterrolebinding", "impact-ai-kit-reporter"),
        ("clusterrolebinding", "impact-ai-kit-uninstall"),
        ("clusterrolebinding", "impact-ai-operator-manager"),
    ]:
        assert not exists(kind, name), (kind, name)


def test_ae8_no_cluster_scoped_object_labelled_for_the_release_remains(uninstalled):
    # The last step of the command deletes, by label, what an uninstall that was cut short leaves behind. After the
    # whole command nothing of the release is labelled, and the Operator's resource definition (which is not) stays.
    labelled = kubectl(
        "get",
        "clusterrole,clusterrolebinding,mutatingwebhookconfiguration,validatingwebhookconfiguration",
        "-l",
        "app.kubernetes.io/part-of=impact-ai,app.kubernetes.io/instance=impact-ai",
        "-o",
        "name",
        check=False,
    ).stdout.split()
    assert labelled == [], labelled
    assert exists("crd", "instrumentations.opentelemetry.io")


def test_ae8_no_collector_reporter_instrumentation_or_hook_object_remains(uninstalled):
    assert get("instrumentations.opentelemetry.io", "--all-namespaces")["items"] == []
    for kind in ("deployments", "services", "serviceaccounts", "secrets", "jobs"):
        remaining = [
            i["metadata"]["name"]
            for i in get(kind, "--all-namespaces")["items"]
            if i["metadata"]["name"].startswith("impact-ai")
        ]
        assert remaining == [], (kind, remaining)


def test_ae8_it_names_the_resource_type_it_keeps(uninstalled):
    # The Operator's resource definition stays: Helm never deletes definitions, and other tools may use it.
    assert exists("crd", "instrumentations.opentelemetry.io")
    assert "instrumentations.opentelemetry.io" in (
        UNINSTALL_SCRIPT.read_text() + (UNINSTALL_SCRIPT.parent.parent / "chart/impact-ai/README.md").read_text()
    )


def test_ae8_no_injection_annotation_remains(uninstalled):
    annotated = []
    for kind in ("deployments", "statefulsets", "daemonsets", "cronjobs"):
        for item in get(kind, "--all-namespaces")["items"]:
            template = item["spec"].get("template") or item["spec"]["jobTemplate"]["spec"]["template"]
            if INJECT in (template["metadata"].get("annotations") or {}):
                annotated.append((kind, item["metadata"]["name"]))
    assert annotated == []


def test_ae8_restarted_pods_are_not_injected(uninstalled):
    kubectl("-n", "shop", "rollout", "restart", "deployment/support-assistant")
    rollout("deployment/support-assistant", "shop")
    pod = running_pod("shop", "app=support-assistant")
    assert not pod["spec"].get("initContainers")
    assert "PYTHONPATH" not in {e["name"] for e in pod["spec"]["containers"][0].get("env", [])}


def test_ae8_the_last_thing_impact_ai_hears_is_one_notice_and_nothing_follows(world, before, uninstalled):
    requests = world.mock.requests("/v1/inventory")
    last = requests[-1]["json"]
    assert last["reporter"]["mode"] == "uninstalled"
    assert last["workloads"] == []
    assert last["cluster"]["uid"] == world.cluster_uid
    assert {k: last["cluster"].get(k) for k in CLOUD_IN_INVENTORY} == CLOUD_IN_INVENTORY  # the same identity
    assert requests[-1]["status"] == 202
    earlier = [r["json"] for r in requests[:-1] if r.get("json")]
    assert earlier, "the old reporter sent snapshots before"
    # The race: the notice is the last request that arrived, and its sequence is above every other one.
    assert all(r["reporter"]["mode"] != "uninstalled" for r in earlier)
    assert last["sequence"] > max(r["sequence"] for r in earlier)
    assert last["observedAt"] >= max(r["observedAt"] for r in earlier)
    # And no late snapshot of the old reporter arrives afterwards: wait two reporter cycles.
    seconds = int(INTERVAL.rstrip("s"))
    time.sleep(2 * seconds + 5)
    after = world.mock.requests("/v1/inventory")
    assert len(after) == len(requests)
    assert after[-1]["json"]["reporter"]["mode"] == "uninstalled"


def test_ae8_the_notice_arrived_while_the_uninstall_command_ran(world, before, uninstalled):
    assert len(world.mock.requests("/v1/inventory")) > before["count"]
