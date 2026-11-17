# SPDX-License-Identifier: Apache-2.0

"""Uninstall, for what automatic coverage added: the uninstall leaves nothing of it, and the cluster stops reporting at
once.

On the install and the covered pods test_8_auto_cover.py leaves behind, `helm uninstall` alone (step 1 of the shown
uninstall command) runs the pre-delete hook, which

* stops the cover pod too, so that no pod is marked while the command runs;
* sends the final snapshot, with the key the cluster made (so the notice is accepted), and only then deletes the Secret
  that holds that key, which Helm does not own;
* patches no pod: the marks the cover pod put on pods are still there when it is done.

Steps 2 to 5 of the command (scripts/uninstall-cluster-kit.sh, run here with step 1 already done) are made with the
rights of the person who installed the kit, and

* take the injection annotations, the marker and the marker label off every pod the cover pod marked, with a metadata
  patch: no pod restarts, and a pod the customer annotated through its workload is not one of them (it is theirs);
* delete the namespace and then the cluster-scoped objects labelled for the release, which finds what an uninstall that
  was cut short leaves behind (a hook's binding is not Helm's to delete then).

Nothing of the kit remains afterwards except the Operator's resource definition, and a pod that is created again is not
injected. The file removes the namespaces of the tests when it is done, so that the next file starts clean.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from kube import exists as kube_exists
from kube import (
    get,
    helm,
    kubectl,
    pods,
    rollout,
    run_uninstall_script,
    running_pod,
    wait_until,
)

NAMESPACE = "impact-ai"
AUTO = "auto"
INJECT_PREFIX = "instrumentation.opentelemetry.io/inject-"
MARKER = "impact.ai/auto-covered"
RELEASE_LABELS = "app.kubernetes.io/part-of=impact-ai,app.kubernetes.io/instance=impact-ai"
CLUSTER_SCOPED = "clusterrole,clusterrolebinding,mutatingwebhookconfiguration,validatingwebhookconfiguration"

# What an uninstall that was cut short leaves: the hook's binding, which Helm deletes only when the hook is done.
LEFT_BY_A_CUT_SHORT_UNINSTALL = """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: impact-ai-kit-uninstall
  labels:
    app.kubernetes.io/part-of: impact-ai
    app.kubernetes.io/instance: impact-ai
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: view}
subjects: [{kind: ServiceAccount, name: impact-ai-uninstall, namespace: impact-ai}]
"""


def marked_pods(*, label: bool = False) -> dict[str, dict]:
    """name -> pod, of every live pod in the cluster that carries the cover pod's marker (as an annotation, or with
    `label`, as the label of the same name)."""
    found = {}
    for pod in get("pods", "--all-namespaces")["items"]:
        if pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        holds = pod["metadata"].get("labels") if label else pod["metadata"].get("annotations")
        if MARKER in (holds or {}):
            found[f"{pod['metadata']['namespace']}/{pod['metadata']['name']}"] = pod
    return found


def injected_annotations() -> dict[str, list[str]]:
    """pod -> its inject-* annotations, for every live pod in the cluster that has one."""
    found = {}
    for pod in get("pods", "--all-namespaces")["items"]:
        keys = [k for k in (pod["metadata"].get("annotations") or {}) if k.startswith(INJECT_PREFIX)]
        if keys:
            found[f"{pod['metadata']['namespace']}/{pod['metadata']['name']}"] = keys
    return found


@pytest.fixture(scope="module")
def before(world):
    """What the cluster looks like just before the uninstall: the pods the cover pod marked, and the key."""
    marked = marked_pods()
    assert len(marked) >= 4, f"run test_8_auto_cover.py first: {sorted(marked)} are marked"
    assert set(marked_pods(label=True)) == set(marked), "the marker is on each pod as an annotation and as a label"
    assert kube_exists("-n", NAMESPACE, "secret", "impact-ai-install-key")
    secret = get("-n", NAMESPACE, "secret", "impact-ai-install-key")
    return {
        "marked": marked,
        "uids": {name: pod["metadata"]["uid"] for name, pod in marked.items()},
        "restarts": {
            name: sum(s["restartCount"] for s in pod["status"].get("containerStatuses", []))
            for name, pod in marked.items()
        },
        "key": base64.b64decode(secret["data"]["ingest-key"]).decode(),
        "notices": len([r for r in world.mock.inventories() if r["reporter"]["mode"] == "uninstalled"]),
        "mark": world.mock.last_seq(),
        # the customer's own annotation, through the template of a workload: not the cover pod's marks
        "own": [f"{AUTO}/{p['metadata']['name']}" for p in pods(AUTO, "app=own-inject")],
    }


@pytest.fixture(scope="module")
def uninstalled(world, before):
    """Step 1 of the shown command, alone: helm uninstall and the hook."""
    started = time.monotonic()
    result = helm("uninstall", "impact-ai", "--namespace", NAMESPACE, "--wait", check=False, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    return {"seconds": time.monotonic() - started, "result": result}


@pytest.fixture(scope="module")
def scripted(uninstalled):
    """Steps 2 to 5 of the shown command, as scripts/uninstall-cluster-kit.sh runs them, after a cut-short uninstall
    left a hook's binding behind."""
    kubectl("apply", "-f", "-", input=LEFT_BY_A_CUT_SHORT_UNINSTALL)
    assert kube_exists("clusterrolebinding", "impact-ai-kit-uninstall")
    result = run_uninstall_script(helm_uninstall_done=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


# --- step 1: helm uninstall and the hook -------------------------------------------------------------------


def test_the_uninstall_took_a_hook_and_not_forever(uninstalled):
    # 60 s to stop the pods of the reporter and 60 s to stop the cover pod's, 60 s to send: a covered cluster stays in.
    assert uninstalled["seconds"] < 240


def test_the_hook_patched_no_pod_so_the_marks_are_still_on_the_pods(before, uninstalled):
    # The hook has no right to write a pod, and no role of the kit has: the marks come off with the rights of the
    # person who runs steps 2 to 5 of the command.
    assert set(marked_pods()) == set(before["marked"])
    assert set(marked_pods(label=True)) == set(before["marked"])
    after = {name: pod["metadata"]["uid"] for name, pod in marked_pods().items()}
    assert after == before["uids"], "the same pods, none replaced"


def test_the_final_snapshot_was_accepted_with_the_key_and_then_the_key_was_deleted(world, before, uninstalled):
    notices = [
        r
        for r in world.mock.requests("/v1/inventory", before["mark"])
        if r["json"]["reporter"]["mode"] == "uninstalled"
    ]
    assert len(notices) == 1, "one final snapshot"
    notice = notices[0]
    assert notice["status"] == 202, "accepted: the key was still there when it was sent"
    assert notice["headers"]["Authorization"] == f"Bearer {before['key']}"
    assert notice["json"]["workloads"] == [] and notice["json"]["cluster"]["uid"] == world.cluster_uid
    # Helm does not own the Secret the install hook made: the uninstall hook deleted it, the namespace is still there.
    assert not kube_exists("-n", NAMESPACE, "secret", "impact-ai-install-key")
    assert kube_exists("namespace", NAMESPACE)
    assert world.mock.requests("/v1/inventory", notice["seq"]) == [], "no snapshot follows the final one"


def test_no_webhook_collector_reporter_cover_pod_or_hook_object_remains(uninstalled):
    for kind, name in (
        ("mutatingwebhookconfiguration", "impact-ai-cover"),
        ("mutatingwebhookconfiguration", "impact-ai-zz-cover"),
        ("mutatingwebhookconfiguration", "impact-ai-operator-mutation"),
        ("validatingwebhookconfiguration", "impact-ai-operator-validation"),
        ("clusterrole", "impact-ai-kit-reporter"),
        ("clusterrole", "impact-ai-operator-manager"),
        ("clusterrolebinding", "impact-ai-kit-reporter"),
        ("clusterrolebinding", "impact-ai-kit-uninstall"),
        ("clusterrolebinding", "impact-ai-operator-manager"),
    ):
        assert not kube_exists(kind, name), (kind, name)
    # the hook's strip role of an earlier version is not made any more: no role of the kit writes a pod
    for kind, name in (
        ("clusterrole", "impact-ai-kit-uninstall-strip"),
        ("clusterrolebinding", "impact-ai-kit-uninstall-strip"),
    ):
        assert not kube_exists(kind, name), (kind, name)
    leftovers = [
        (kind, item["metadata"]["name"])
        for kind in (
            "deployments",
            "services",
            "serviceaccounts",
            "secrets",
            "jobs",
            "roles",
            "rolebindings",
            "configmaps",
        )
        for item in get("-n", NAMESPACE, kind)["items"]
        if item["metadata"]["name"].startswith("impact-ai")
    ]
    assert leftovers == [], leftovers
    assert get("instrumentations.opentelemetry.io", "--all-namespaces")["items"] == []
    assert kube_exists("crd", "instrumentations.opentelemetry.io"), (
        "the definition stays, as the uninstall command says"
    )


def test_the_cluster_stops_reporting(world, before, uninstalled):
    last = world.mock.requests("/v1/inventory")[-1]["json"]
    assert last["reporter"]["mode"] == "uninstalled"
    time.sleep(12)  # more than a reporter cycle of the suite (10 s): the old reporter is gone, nothing follows
    assert world.mock.requests("/v1/inventory")[-1]["json"]["reporter"]["mode"] == "uninstalled"


# --- steps 2 to 5: the marks, the namespace and the cluster-scoped objects ---------------------------------


def test_no_pod_carries_the_cover_pods_marker_its_label_or_an_annotation_it_added(before, scripted):
    assert marked_pods() == {}, "the marker is gone from every pod"
    assert marked_pods(label=True) == {}, "and so is the label that found them"
    left = injected_annotations()
    # What remains is the customer's own request on the template of one workload (which step 3 of the command removes
    # from the workload, and which the cover pod never marked).
    assert set(left) <= set(before["own"]), left
    for name in before["marked"]:
        assert name not in left, f"{name} still asks for injection"


def test_taking_the_marks_off_restarted_nothing(before, scripted):
    after = {
        f"{p['metadata']['namespace']}/{p['metadata']['name']}": p for p in get("pods", "--all-namespaces")["items"]
    }
    for name, uid in before["uids"].items():
        pod = after.get(name)
        assert pod is not None, f"{name} was deleted"
        assert pod["metadata"]["uid"] == uid, f"{name} was replaced"
        restarts = sum(s["restartCount"] for s in pod["status"].get("containerStatuses", []))
        assert restarts == before["restarts"][name], f"{name} restarted"
        # only the metadata changed: an injected pod keeps what the Operator put into it
        assert [c["name"] for c in pod["spec"].get("initContainers", [])] == [
            c["name"] for c in before["marked"][name]["spec"].get("initContainers", [])
        ]


def test_the_pods_of_a_template_the_customer_annotated_are_not_among_those_the_pod_patch_selects(before, scripted):
    # Step 2 selects pods by the label only the cover pod adds. The customer's own pods are not on its list, so the
    # command never patches them: the workload's template is what step 3 changes (and the workload restarts then).
    assert before["own"], "the file under test has a workload the customer annotated"
    assert not set(before["own"]) & set(before["marked"])


def test_the_namespace_is_gone_and_no_cluster_scoped_object_of_the_release_remains(scripted):
    assert not kube_exists("namespace", NAMESPACE)
    labelled = kubectl("get", CLUSTER_SCOPED, "-l", RELEASE_LABELS, "-o", "name", check=False).stdout.split()
    assert labelled == [], labelled
    # step 5 found what the cut-short uninstall left, and the definition of the resource stays
    assert not kube_exists("clusterrolebinding", "impact-ai-kit-uninstall")
    assert kube_exists("crd", "instrumentations.opentelemetry.io"), "the definition stays"


def test_a_pod_created_again_is_not_injected(scripted):
    kubectl("-n", AUTO, "rollout", "restart", "deployment/web")
    rollout("deployment/web", AUTO)
    pod = running_pod(AUTO, "app=web")
    assert not pod["spec"].get("initContainers")
    assert MARKER not in (pod["metadata"].get("annotations") or {})
    assert MARKER not in (pod["metadata"].get("labels") or {})
    assert "PYTHONPATH" not in {e["name"] for e in pod["spec"]["containers"][0].get("env", [])}


def test_the_namespaces_of_the_tests_are_removed_so_the_next_file_starts_clean(scripted):
    kubectl("delete", "namespace", NAMESPACE, AUTO, "auto-optout", "--ignore-not-found", "--wait=true", timeout=400)
    for namespace in (NAMESPACE, AUTO, "auto-optout"):
        assert not kube_exists("namespace", namespace)
    wait_until(
        lambda: not json.loads(helm("list", "--all-namespaces", "-o", "json").stdout), timeout=30, message="no release"
    )
