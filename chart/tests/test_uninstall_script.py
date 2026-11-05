# SPDX-License-Identifier: Apache-2.0

"""The uninstall script, run for real with `sh` against a stand-in `helm` and `kubectl` that keep a small cluster in a
file. In this order it

1. uninstalls the release (the hook stops the reporter and the cover pod and sends the notice);
2. takes the marks off the pods the cover pod marked, found by the label `impact.ai/auto-covered`: the injection
   annotations, the marker and the label, a metadata patch of each pod, under the operator's own rights;
3. takes the annotations the per-workload commands added off the workloads, for Python and Node.js in every kind of
   workload, and nothing that is not the kit's;
4. deletes the namespace;
5. deletes the cluster-scoped objects labelled with the release, which an uninstall that was cut short can leave.

The kind end-to-end suite runs the same script against a real cluster; this proves the script's logic where no cluster
is needed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "uninstall-cluster-kit.sh"

PREFIX = "instrumentation.opentelemetry.io/"
OURS = "impact-ai/impact"
PYTHON = PREFIX + "inject-python"
MUSL = PREFIX + "otel-python-platform"
NODEJS = PREFIX + "inject-nodejs"
NAMES = PREFIX + "container-names"
MARKER = "impact.ai/auto-covered"
KIT = "app.kubernetes.io/part-of"
INSTANCE = "app.kubernetes.io/instance"
LIVE = "status.phase!=Succeeded,status.phase!=Failed"
CLUSTER_KINDS = {
    "clusterrolebindings": "ClusterRoleBinding",
    "clusterroles": "ClusterRole",
    "mutatingwebhookconfigurations": "MutatingWebhookConfiguration",
    "validatingwebhookconfigurations": "ValidatingWebhookConfiguration",
}

# A stand-in for kubectl that understands exactly what the script says to it, and fails on anything else:
# `get <workloads> --all-namespaces -o jsonpath=...annotations['<key>']=='<value>'...`, `get pods --all-namespaces
# --selector <labels> --field-selector <fields> -o jsonpath=...`, `--namespace <ns> patch <kind> <name> --type merge -p
# <json>`, `delete namespace <ns> --wait` and `delete <kinds> --selector <labels>`. The state is a JSON file; the log is
# one JSON line per call.
FAKE_KUBECTL = r"""#!{python}
import json
import os
import re
import sys

state_path, log_path = os.environ["FAKE_CLUSTER"], os.environ["FAKE_LOG"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(log_path, "a") as log:
    log.write(json.dumps(["kubectl", *args]) + "\n")

namespace = None
if args[0] == "--namespace":
    namespace, args = args[1], args[2:]
command, rest = args[0], args[1:]
WORKLOADS = ("deployments", "statefulsets", "daemonsets", "cronjobs")
CLUSTER_KINDS = {cluster_kinds}


def template_path(kind):
    return ["spec", "jobTemplate", "spec", "template"] if kind == "cronjobs" else ["spec", "template"]


def flags_of(words):
    # (positional words, {flag: value}) of kubectl words: `--k v`, `--k=v`, `-l v`, `-o v` and the switches
    names = {"-l": "--selector", "-o": "--output", "-A": "--all-namespaces", "-n": "--namespace"}
    switches = {"--all-namespaces", "--wait", "--ignore-not-found"}
    positional, flags, i = [], {}, 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            name, equals, value = word.partition("=")
            name = names.get(name, name)
            if name in switches:
                flags[name] = True
            elif equals:
                flags[name] = value
            else:
                i += 1
                flags[name] = words[i]
        else:
            positional.append(word)
        i += 1
    return positional, flags


def selected(selector, labels):
    for term in selector.split(","):
        key, equals, value = term.partition("=")
        if equals and labels.get(key) != value:
            return False
        if not equals and key not in labels:
            return False
    return True


def live(field_selector, pod):
    for term in field_selector.split(","):
        field, _, value = term.partition("!=")
        assert field == "status.phase", f"unexpected field selector {term}"
        if pod["phase"] == value:
            return False
    return True


if command == "get" and rest[0] == "pods":
    positional, flags = flags_of(rest)
    assert positional == ["pods"] and flags.pop("--all-namespaces") is True, rest
    assert flags.pop("--output").startswith("jsonpath="), rest
    selector, field_selector = flags.pop("--selector"), flags.pop("--field-selector")
    assert not flags, f"unexpected flags {flags}"
    for pod in state["pods"]:
        if selected(selector, pod["labels"]) and live(field_selector, pod):
            print(pod["namespace"], pod["name"])
elif command == "get":
    kind = rest[0]
    assert kind in WORKLOADS, f"unexpected kind {kind}"
    assert rest[1:3] == ["--all-namespaces", "-o"], rest
    expression = rest[3]
    assert expression.startswith("jsonpath="), expression
    path = "".join("." + part for part in template_path(kind)) + ".metadata.annotations["
    assert path in expression, f"{kind}: {path} is not in {expression}"
    match = re.search(r"annotations\['([^']+)'\]=='([^']+)'", expression)
    key, value = match.group(1).replace("\\.", "."), match.group(2)
    for item in state["items"]:
        if item["kind"] == kind and item["annotations"].get(key) == value:
            print(item["namespace"], item["name"])
elif command == "patch" and rest[0] in ("pod", "pods"):
    name = rest[1]
    assert rest[2:5] == ["--type", "merge", "-p"], rest
    body = json.loads(rest[5])
    assert list(body) == ["metadata"] and set(body["metadata"]) <= {"annotations", "labels"}, f"the patch holds {body}"
    pods = [p for p in state["pods"] if (p["namespace"], p["name"]) == (namespace, name)]
    if not pods or pods[0].get("gone"):
        sys.exit(f'Error from server (NotFound): pods "{name}" not found')
    for section, changes in body["metadata"].items():
        for key, value in changes.items():
            assert value is None, "the uninstall only removes"
            pods[0][section].pop(key, None)
    pods[0]["patches"] += 1
elif command == "patch":
    kind, name = {"cronjob": "cronjobs"}.get(rest[0], rest[0]), rest[1]  # kubectl takes either form
    assert kind in WORKLOADS and rest[2:5] == ["--type", "merge", "-p"], rest
    body = json.loads(rest[5])
    for part in template_path(kind) + ["metadata", "annotations"]:
        assert list(body) == [part], f"{kind}: the patch must hold only {part}, it holds {list(body)}"
        body = body[part]
    (item,) = [i for i in state["items"] if (i["kind"], i["namespace"], i["name"]) == (kind, namespace, name)]
    for key, value in body.items():
        assert value is None, "the uninstall only removes annotations"
        item["annotations"].pop(key, None)
    item["patches"] += 1
elif command == "delete" and rest[0] == "namespace":
    assert rest == ["namespace", os.environ["EXPECT_NAMESPACE"], "--wait"], rest
elif command == "delete":
    positional, flags = flags_of(rest)
    kinds = positional[0].split(",")
    assert len(positional) == 1 and set(kinds) == set(CLUSTER_KINDS), f"only cluster-scoped kinds: {positional}"
    assert set(flags) == {"--selector", "--ignore-not-found"}, flags
    doomed = {CLUSTER_KINDS[k] for k in kinds}
    state["cluster"] = [
        o for o in state["cluster"] if not (o["kind"] in doomed and selected(flags["--selector"], o["labels"]))
    ]
else:
    sys.exit(f"unexpected kubectl call: {args}")
json.dump(state, open(state_path, "w"))
"""

FAKE_HELM = """#!/bin/sh
printf '%s\\n' "[\\"helm\\", \\"$*\\"]" >> "$FAKE_LOG"
"""


def workload(kind: str, namespace: str, name: str, **annotations: str) -> dict:
    return {"kind": kind, "namespace": namespace, "name": name, "annotations": annotations, "patches": 0}


def pod(namespace: str, name: str, annotations: dict, labels: dict, phase: str = "Running", **extra) -> dict:
    return {
        "namespace": namespace,
        "name": name,
        "phase": phase,
        "annotations": annotations,
        "labels": labels,
        "patches": 0,
        **extra,
    }


def cluster_object(kind: str, name: str, **labels: str) -> dict:
    return {"kind": kind, "name": name, "labels": labels}


def cluster() -> list[dict]:
    return [
        workload("deployments", "shop", "py", **{PYTHON: OURS, MUSL: "musl", "team": "support"}),
        workload("deployments", "shop", "node", **{NODEJS: OURS, "team": "search"}),
        workload("statefulsets", "data", "db-node", **{NODEJS: OURS}),
        workload("daemonsets", "ops", "agent-py", **{PYTHON: OURS}),
        workload("daemonsets", "ops", "agent-both", **{PYTHON: OURS, NODEJS: OURS}),
        workload("cronjobs", "batch", "nightly-node", **{NODEJS: OURS}),
        workload("cronjobs", "batch", "nightly-py", **{PYTHON: OURS}),
        # Not the kit's: another injector's Instrumentation, and a workload nobody covered.
        workload("deployments", "shop", "foreign", **{NODEJS: "other/their-instrumentation", "team": "billing"}),
        workload("deployments", "shop", "plain", team="payments"),
    ]


def marked(**annotations: str) -> dict:
    """The annotations of a pod the cover pod annotated: the request, the marker with its time, and the pod's own."""
    return {PYTHON: OURS, MARKER: "2026-11-04T09:30:00Z", **annotations}


def pods() -> list[dict]:
    on = {MARKER: "true"}
    return [
        pod("shop", "api-1", marked(team="support"), {**on, "app": "api", "pod-template-hash": "5d9c7b6f8d"}),
        pod("shop", "api-2", marked(**{NAMES: "app", MUSL: "musl", "team": "support"}), {**on, "app": "api"}),
        pod("jobs", "report-1", {NODEJS: OURS, MARKER: "2026-11-04T09:31:00Z"}, on),
        # Not the cover pod's: a pod of a template the customer annotated has no marker, and neither has a plain pod.
        pod("shop", "theirs-1", {PYTHON: OURS}, {"app": "theirs"}),
        pod("shop", "plain-1", {"team": "payments"}, {"app": "plain"}),
        # A pod that has finished is of no use to anyone.
        pod("batch", "done-1", marked(), on, phase="Succeeded"),
    ]


def scoped() -> list[dict]:
    kit = {KIT: "impact-ai", INSTANCE: "impact-ai"}
    return [
        # What an uninstall that was cut short can leave: a hook's binding, and the objects Helm owns.
        cluster_object("ClusterRoleBinding", "impact-ai-kit-uninstall", **kit),
        cluster_object("ClusterRole", "impact-ai-kit-reporter", **kit),
        cluster_object("ClusterRoleBinding", "impact-ai-kit-reporter", **kit),
        cluster_object("MutatingWebhookConfiguration", "impact-ai-cover", **kit),
        cluster_object("ValidatingWebhookConfiguration", "impact-ai-operator-validation", **kit),
        # Not this release's.
        cluster_object("ClusterRole", "impact-ai-kit-reporter", **{KIT: "impact-ai", INSTANCE: "other"}),
        cluster_object("ClusterRole", "something-else", **{INSTANCE: "impact-ai"}),
        cluster_object("ClusterRoleBinding", "cluster-admin", **{"kubernetes.io/bootstrapping": "rbac-defaults"}),
        cluster_object("CustomResourceDefinition", "instrumentations.opentelemetry.io", **kit),
    ]


@dataclass
class Run:
    items: list[dict]
    pods: list[dict]
    cluster: list[dict]
    calls: list[list[str]]
    stderr: str

    def kubectl(self, *words: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "kubectl" and c[1 : 1 + len(words)] == list(words)]


def run_script(tmp_path: Path, items=None, pod_list=None, objects=None, **env: str) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = FAKE_KUBECTL.replace("{python}", sys.executable).replace("{cluster_kinds}", repr(CLUSTER_KINDS))
    for name, text in (("kubectl", fake), ("helm", FAKE_HELM)):
        path = bin_dir / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    state, log = tmp_path / "cluster.json", tmp_path / "calls.jsonl"
    state.write_text(json.dumps({"items": items or [], "pods": pod_list or [], "cluster": objects or []}))
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CLUSTER": str(state),
        "FAKE_LOG": str(log),
        "EXPECT_NAMESPACE": env.get("NAMESPACE", "impact-ai"),
        **env,
    }
    result = subprocess.run(  # noqa: S603
        ["sh", str(SCRIPT)],  # noqa: S607
        env=environment, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(state.read_text())
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return Run(after["items"], after["pods"], after["cluster"], calls, result.stderr)


def by_name(items: list[dict]) -> dict[str, dict]:
    return {i["name"]: i for i in items}


@pytest.fixture
def after(tmp_path):
    return run_script(tmp_path, cluster(), pods(), scoped())


# --- the annotations the per-workload commands added ------------------------------------------------------------


def test_the_kits_annotations_come_off_every_kind_of_workload_in_both_languages(after):
    left = [(i["kind"], i["name"], k) for i in after.items for k, v in i["annotations"].items() if v == OURS]
    assert left == [], "the kit's injection annotations remain"
    assert not [(i["name"], MUSL) for i in after.items if MUSL in i["annotations"]], "the musl platform remains"


def test_what_is_not_the_kits_stays_and_is_never_touched(after):
    found = by_name(after.items)
    assert found["foreign"]["annotations"] == {NODEJS: "other/their-instrumentation", "team": "billing"}
    assert found["plain"]["annotations"] == {"team": "payments"}
    assert found["foreign"]["patches"] == found["plain"]["patches"] == 0, (
        "a workload that is not the kit's is not restarted"
    )
    assert found["py"]["annotations"] == {"team": "support"}
    assert found["node"]["annotations"] == {"team": "search"}


def test_a_workload_is_patched_once_for_each_language_it_was_covered_for(after):
    found = by_name(after.items)
    for name in ("py", "node", "db-node", "agent-py", "nightly-node", "nightly-py"):
        assert found[name]["patches"] == 1, name
    assert found["agent-both"]["patches"] == 2


# --- the marks the cover pod put on pods ------------------------------------------------------------------------------


def test_the_marks_come_off_every_live_pod_the_cover_pod_marked_and_nothing_else_of_it_changes(after):
    found = by_name(after.pods)
    assert found["api-1"]["annotations"] == {"team": "support"}
    assert found["api-1"]["labels"] == {"app": "api", "pod-template-hash": "5d9c7b6f8d"}
    assert found["api-2"]["annotations"] == {"team": "support"}, "the container and platform annotations go too"
    assert found["api-2"]["labels"] == {"app": "api"}
    assert found["report-1"]["annotations"] == {} and found["report-1"]["labels"] == {}
    assert [found[n]["patches"] for n in ("api-1", "api-2", "report-1")] == [1, 1, 1], "one patch for each pod"


def test_a_pod_the_cover_pod_did_not_mark_is_never_touched_and_neither_is_a_finished_one(after):
    found = by_name(after.pods)
    assert found["theirs-1"]["annotations"] == {PYTHON: OURS}, "the customer annotated the template: it is theirs"
    assert found["plain-1"]["annotations"] == {"team": "payments"}
    assert found["done-1"]["annotations"] == marked() and found["done-1"]["labels"] == {MARKER: "true"}
    assert [found[n]["patches"] for n in ("theirs-1", "plain-1", "done-1")] == [0, 0, 0]


def test_the_pods_are_found_by_the_marker_label_on_the_server_and_never_by_listing_them_all(after):
    (get,) = after.kubectl("get", "pods")
    selector = get[get.index("--selector") + 1] if "--selector" in get else get[get.index("-l") + 1]
    assert selector == f"{MARKER}=true"
    fields = get[get.index("--field-selector") + 1]
    assert fields == LIVE, "pods that have finished are left out by the server"
    assert "--all-namespaces" in get or "-A" in get


def test_the_patch_removes_the_cover_pods_own_marks_and_nothing_that_could_be_the_customers(after):
    patches = [c for c in after.calls if "patch" in c and "pod" in c]
    assert len(patches) == 3
    bodies = {json.dumps(json.loads(c[c.index("-p") + 1]), sort_keys=True) for c in patches}
    assert len(bodies) == 1, "the same patch for every pod"
    (body,) = [json.loads(b) for b in bodies]
    assert body == {
        "metadata": {
            "annotations": dict.fromkeys((PYTHON, NODEJS, NAMES, MUSL, MARKER)),
            "labels": {MARKER: None},
        }
    }


def test_a_pod_that_is_gone_before_it_is_patched_does_not_stop_the_uninstall(tmp_path):
    gone = pod("shop", "gone-1", marked(), {MARKER: "true"}, gone=True)
    run = run_script(tmp_path, cluster(), [gone, *pods()], scoped())
    found = by_name(run.pods)
    assert found["api-1"]["annotations"] == {"team": "support"} and found["report-1"]["annotations"] == {}
    assert "gone-1" in run.stderr, "the script says which pod it could not patch"
    assert run.calls[-2] == ["kubectl", "delete", "namespace", "impact-ai", "--wait"], "and goes on to the end"


def test_the_marks_come_off_right_after_the_release_is_gone_and_before_the_workloads(after):
    calls = after.calls
    assert calls[0] == ["helm", "uninstall impact-ai --namespace impact-ai --wait"]
    assert calls[1][:3] == ["kubectl", "get", "pods"], (
        "the cover pod and its webhook are gone, so nothing is marked meanwhile"
    )
    pod_patches = [i for i, c in enumerate(calls) if "pod" in c and "patch" in c]
    workload_patches = [i for i, c in enumerate(calls) if "patch" in c and "pod" not in c]
    assert pod_patches and workload_patches and max(pod_patches) < min(workload_patches)


# --- the end of the script --------------------------------------------------------------------------------------


def test_the_release_goes_first_the_namespace_next_to_last_and_the_cluster_scoped_objects_last(after):
    calls = after.calls
    assert calls[0] == ["helm", "uninstall impact-ai --namespace impact-ai --wait"]
    assert calls[-2] == ["kubectl", "delete", "namespace", "impact-ai", "--wait"]
    assert calls[-1][:2] == ["kubectl", "delete"] and calls[-1][2] != "namespace"
    assert all(call[0] == "kubectl" for call in calls[1:])


def test_the_cluster_scoped_objects_of_the_release_are_deleted_by_label_and_no_other(after):
    left = {(o["kind"], o["name"], o["labels"].get(INSTANCE)) for o in after.cluster}
    assert left == {
        ("ClusterRole", "impact-ai-kit-reporter", "other"),  # another release's
        ("ClusterRole", "something-else", "impact-ai"),  # not the kit's
        ("ClusterRoleBinding", "cluster-admin", None),
        ("CustomResourceDefinition", "instrumentations.opentelemetry.io", "impact-ai"),  # the definition stays
    }
    (delete,) = [c for c in after.calls if c[:2] == ["kubectl", "delete"] and c[2] != "namespace"]
    selector = delete[delete.index("--selector") + 1] if "--selector" in delete else delete[delete.index("-l") + 1]
    assert sorted(selector.split(",")) == sorted([f"{KIT}=impact-ai", f"{INSTANCE}=impact-ai"])
    assert "customresourcedefinitions" not in delete[2] and "namespaces" not in delete[2]


def test_a_release_and_namespace_of_another_name_are_honoured(tmp_path):
    items = [
        workload("deployments", "shop", "node", **{NODEJS: "kit/impact"}),
        workload("deployments", "shop", "elsewhere", **{NODEJS: OURS}),
    ]
    kit = {KIT: "impact-ai"}
    objects = [
        cluster_object("ClusterRoleBinding", "impact-ai-kit-uninstall", **kit, **{INSTANCE: "kit-a"}),
        cluster_object("ClusterRoleBinding", "impact-ai-kit-uninstall-2", **kit, **{INSTANCE: "impact-ai"}),
    ]
    run = run_script(tmp_path, items, [], objects, RELEASE="kit-a", NAMESPACE="kit")
    found = by_name(run.items)
    assert found["node"]["annotations"] == {}
    assert found["elsewhere"]["annotations"] == {NODEJS: OURS}, "the annotation names the namespace of this install"
    assert run.calls[0] == ["helm", "uninstall kit-a --namespace kit --wait"]
    assert [o["name"] for o in run.cluster] == ["impact-ai-kit-uninstall-2"], "only the objects of release kit-a go"


def test_a_cluster_where_nothing_was_covered_is_only_uninstalled(tmp_path):
    run = run_script(tmp_path, [workload("deployments", "shop", "plain", team="payments")])
    assert run.items[0]["patches"] == 0
    assert [c for c in run.calls if "patch" in c] == []
    assert run.calls[0][0] == "helm" and run.calls[-2][:3] == ["kubectl", "delete", "namespace"]
