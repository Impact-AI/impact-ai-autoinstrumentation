# SPDX-License-Identifier: Apache-2.0

"""The one-time install token, and the kit's part of a cluster connected within a minute.

The command Cover your AI shows carries a single-use token that is valid for 15 minutes, never an ingest key. A Helm
hook trades it inside the cluster for the install's own key, which only a Secret of the cluster holds. Against the mock
ingest (testing/mock), which implements the exchange as contract C10 says:

* a token that was used, has expired or belongs to another product is refused before anything is installed, in plain
  words that Helm itself prints, and leaves only what Helm applies before any hook: the empty namespace and the
  Instrumentation resource definition (so the failure text says so; "before anything changes" is that, not nothing);
* a lost answer is retried with the same hash and costs one token, not two (the key is stored before impact.ai is
  asked);
* the key is in no manifest, no pod spec, no event, no log and no release record, and impact.ai got only its hash;
* the same command run again, as an upgrade with `--reuse-values` does, changes nothing and calls nobody;
* a fresh command for a cluster whose key was revoked replaces the key, and a refused one leaves the working key alone;
* the first snapshot arrives within 45 s of Helm returning. Image pull time is not in that: the suite loads the images
  into the cluster first, and the upstream ones are pulled during `--wait`, before Helm returns.

The install this file ends with stays: test_8_auto_cover.py and test_8_uninstall.py go on from it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time

import pytest

from kube import (
    CLOUD_ARN,
    KEY,
    PRODUCT,
    exists,
    get,
    helm,
    install_with_token,
    kubectl,
    pods,
    wait_until,
)

RELEASE = "impact-ai"
NAMESPACE = "impact-ai"
KEY_SECRET = "impact-ai-install-key"  # noqa: S105 - the name of a Secret, not a password
KEY_SHAPE = re.compile(r"^impact_e2e_[A-Za-z0-9_-]{43}$")
FAST_POLL = ("--set", "cover.pollInterval=5s")  # the default is a minute; the cover tests would wait for it

# What a customer reads, by refusal: the message impact.ai sends with it (the mock has the real exchange's, word for
# word), printed as it is.
SENTENCES = {
    "token_used": "This install command was already used. Create a new one in Cover your AI.",
    "token_expired": (
        "This install command expired. Install commands are valid for 15 minutes; create a new one in Cover your AI."
    ),
    "token_wrong_product": (
        "This install command is for another product. Create a new one in Cover your AI for this product."
    ),
}
STATUS = {"token_used": 409, "token_expired": 410, "token_wrong_product": 403}


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def key_secret() -> dict | None:
    result = kubectl("-n", NAMESPACE, "get", "secret", KEY_SECRET, "-o", "json", check=False)
    return json.loads(result.stdout) if result.returncode == 0 else None


def key_of(secret: dict) -> str:
    return base64.b64decode(secret["data"]["ingest-key"]).decode()


def remove_the_kit() -> None:
    """Back to a cluster without the kit and without its namespace, whatever an earlier file left."""
    if helm("status", RELEASE, "-n", NAMESPACE, check=False).returncode == 0:
        helm("uninstall", RELEASE, "-n", NAMESPACE, "--no-hooks", "--wait", check=False)
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", timeout=300, check=False)
    for kind, name in (
        ("clusterrole", "impact-ai-kit-reporter"),
        ("clusterrolebinding", "impact-ai-kit-reporter"),
        ("mutatingwebhookconfiguration", "impact-ai-operator-mutation"),
        ("validatingwebhookconfiguration", "impact-ai-operator-validation"),
        ("mutatingwebhookconfiguration", "impact-ai-cover"),
    ):
        kubectl("delete", kind, name, "--ignore-not-found", check=False)


@pytest.fixture(scope="module", autouse=True)
def a_cluster_without_the_kit(world):
    remove_the_kit()
    world.mock.reset()
    world.mock.control(require_key=KEY)


def leftovers() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """What of the kit the cluster holds: the namespace's own objects (those every namespace has, and Helm's record of a
    release, left out) and the cluster-wide objects the kit makes."""
    namespaced = []
    for kind in ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs", "services", "pods", "secrets",
                 "configmaps", "serviceaccounts", "roles", "rolebindings"):  # fmt: skip
        for item in get("-n", NAMESPACE, kind)["items"]:
            name = item["metadata"]["name"]
            if (kind, name) in {("serviceaccounts", "default"), ("configmaps", "kube-root-ca.crt")}:
                continue
            if kind == "secrets" and name.startswith("sh.helm.release.v1."):
                continue
            namespaced.append((kind, name))
    cluster = [
        (kind, item["metadata"]["name"])
        for kind in (
            "clusterroles",
            "clusterrolebindings",
            "mutatingwebhookconfigurations",
            "validatingwebhookconfigurations",
        )
        for item in get(kind)["items"]
        if item["metadata"]["name"].startswith("impact-ai")
    ]  # fmt: skip
    return namespaced, cluster


def settled_leftovers() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """leftovers(), once the pod of the hook that failed has been deleted (Kubernetes does that in the background)."""
    last = []

    def gone():
        last[:] = [leftovers()]
        return last[0] if not [n for n in last[0][0] if n[0] == "pods"] else None

    try:
        return wait_until(gone, timeout=60, interval=2, message="the pod of the failed hook to be deleted")
    except AssertionError:
        return last[0]


# --- the refusals ---------------------------------------------------------------------------------------------------


def refused_command(world, token: str, code: str):
    mark = world.mock.last_seq()
    result = install_with_token(token, check=False)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    # The plain sentence comes out of Helm itself (it prints the log of a failed hook), so the customer reads why
    # without a second command, and what is left is said too.
    assert SENTENCES[code] in output, output
    assert "Nothing was installed or changed" in output, output
    assert "empty namespace" in output and "Instrumentation resource definition" in output, output
    sent = world.mock.exchanges(mark)
    assert [r["status"] for r in sent] == [STATUS[code]], "asked once, and a refusal is not retried"
    assert token not in json.dumps(sent[0]["json"]), "the request carries the token in its header only"
    refusals = [e for e in world.mock.installs()["events"] if e["kind"] == "refused"]
    assert refusals and refusals[-1]["code"] == code
    return output


def test_a_token_used_before_is_refused_before_anything_is_installed(world):
    refused_command(world, world.mock.token(state="used"), "token_used")
    namespaced, cluster = settled_leftovers()
    # Helm applies the namespace and crds/ before any hook; the kit's own objects, the hook's included, are not there.
    assert namespaced == [] and cluster == [], (namespaced, cluster)
    assert exists("namespace", NAMESPACE) and exists("crd", "instrumentations.opentelemetry.io")
    assert key_secret() is None
    release = json.loads(helm("status", RELEASE, "-n", NAMESPACE, "-o", "json").stdout)
    assert release["info"]["status"] == "failed"


def test_a_token_that_has_expired_is_refused_and_a_fresh_command_goes_on_from_there(world):
    # The next command is an upgrade of the failed one (`helm upgrade --install` treats it so): the hook runs as
    # pre-upgrade, which is what the customer's second attempt does.
    refused_command(world, world.mock.token(expires_in=-60), "token_expired")
    assert settled_leftovers() == ([], [])


def test_a_token_of_another_product_is_refused(world):
    refused_command(world, world.mock.token(product="prod_someone_else"), "token_wrong_product")
    assert settled_leftovers() == ([], [])
    assert key_secret() is None


def test_a_refused_install_is_removed_with_helm_uninstall_at_once():
    # The pre-delete hook of a release whose key was never made must not wait for a Secret that does not exist.
    started = time.monotonic()
    result = helm("uninstall", RELEASE, "-n", NAMESPACE, "--wait", check=False, timeout=400)
    assert result.returncode == 0, result.stdout + result.stderr
    assert time.monotonic() - started < 90
    assert settled_leftovers() == ([], [])
    kubectl("delete", "namespace", NAMESPACE, "--wait=true", timeout=300)
    assert not exists("namespace", NAMESPACE)
    assert exists("crd", "instrumentations.opentelemetry.io"), "the definition stays, as the uninstall command says"


# --- the install ----------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def installed(world):
    """The install with a token whose first answer is lost on the way back: the key was stored before impact.ai was
    asked, so the retry sends the same hash and impact.ai answers replayed."""
    made = world.mock.new_token()
    world.mock.rules([{"path": "/v1/installs/exchange", "drop": True, "times": 1}])
    mark = world.mock.last_seq()
    started = time.time()
    result = install_with_token(made["token"], extra=FAST_POLL)
    returned = time.time()
    world.mock.rules([])
    return {
        "token": made["token"],
        "id": made["id"],
        "mark": mark,
        "started": started,
        "returned": returned,
        "result": result,
    }


def test_a_lost_answer_costs_one_token_and_stores_one_key(world, installed):
    token = installed["token"]
    sent = world.mock.exchanges(installed["mark"])
    assert len(sent) == 2, [r.get("status") for r in sent]
    assert sent[0]["body_b64"] == sent[1]["body_b64"], "the retry sends the very same request"
    events = [e["kind"] for e in world.mock.installs()["events"] if e.get("token_id") == installed["id"]]
    assert events == ["issued", "exchanged", "replayed"], events
    keys = world.mock.installs()["keys"]
    assert len(keys) == 1 and keys[0]["product"] == PRODUCT and not keys[0]["revoked"]
    assert keys[0]["cluster_uid"] == world.cluster_uid and keys[0]["cluster_arn"] == CLOUD_ARN
    assert installed["result"].returncode == 0
    assert fingerprint(token) == key_secret()["metadata"]["annotations"]["impact.ai/token"]


def test_the_key_was_made_in_the_cluster_and_impact_ai_got_only_its_hash(world, installed):
    secret = key_secret()
    key = key_of(secret)
    assert KEY_SHAPE.match(key), "impact_<environment>_<43 characters>, as a key impact.ai makes itself"
    assert set(secret["data"]) == {"ingest-key"}, "no pending key is left behind"
    annotations = secret["metadata"]["annotations"]
    assert annotations["impact.ai/exchange"] == "exchanged" and "impact.ai/pending-token" not in annotations

    (issued,) = world.mock.installs()["keys"]
    assert issued["lookup_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert issued["display_prefix"] == key[: len("impact_e2e_") + 8]
    for request in world.mock.exchanges(installed["mark"]):
        assert key not in base64.b64decode(request["body_b64"]).decode(), "the request carries the hash, not the key"
        assert request["headers"]["Authorization"] == f"Bearer {installed['token']}"
        assert request["json"]["productId"] == PRODUCT and request["json"]["cluster"]["uid"] == world.cluster_uid


def test_the_key_is_in_no_manifest_no_pod_spec_no_event_no_log_and_no_release_record(installed):
    key = key_of(key_secret())
    token = installed["token"]
    said = {
        "pods": kubectl("get", "pods", "-A", "-o", "json").stdout,
        "workloads": kubectl("get", "deployments,statefulsets,daemonsets,jobs,cronjobs", "-A", "-o", "json").stdout,
        "events": kubectl("get", "events", "-A", "-o", "json").stdout,
        "configmaps": kubectl("get", "configmaps", "-A", "-o", "json").stdout,
        "helm": helm("get", "all", RELEASE, "-n", NAMESPACE).stdout,
    }
    for pod in pods(NAMESPACE, "app.kubernetes.io/part-of=impact-ai"):
        logs = kubectl("-n", NAMESPACE, "logs", pod["metadata"]["name"], "--all-containers", check=False)
        said["log " + pod["metadata"]["name"]] = logs.stdout + logs.stderr
    for where, text in said.items():
        assert key not in text, f"the ingest key is in {where}"
        if where != "helm":
            assert token not in text, f"the install token is in {where}"
    # The token is a Helm value, so the release record has it, as the chart says; once used it opens nothing
    # (a repeat with another key hash is the 409 of the refusals above, and the next test runs the command again).
    assert token in said["helm"]


def test_the_hook_leaves_none_of_its_objects_behind(installed):
    namespaced, cluster = leftovers()
    names = {name for _, name in namespaced}
    assert not {"impact-ai-exchange", "impact-ai-install-token"} & names
    assert (("secrets", KEY_SECRET)) in namespaced
    assert [n for n in namespaced if n[0] == "jobs"] == []
    assert not [c for c in cluster if "exchange" in c[1]]


def test_the_kit_is_up_and_reporting_with_the_key_the_cluster_made(world, installed):
    names = {d["metadata"]["name"] for d in get("-n", NAMESPACE, "deployments")["items"]}
    assert names == {"impact-ai-operator", "impact-ai-gateway", "impact-ai-reporter", "impact-ai-cover"}

    def accepted():
        return [r for r in world.mock.requests("/v1/inventory", installed["mark"]) if r.get("status") == 202]

    snapshots = wait_until(accepted, timeout=60, message="a snapshot accepted with the key the cluster made")
    key = key_of(key_secret())
    assert all(r["headers"]["Authorization"] == f"Bearer {key}" for r in snapshots)
    assert snapshots[0]["json"]["cluster"]["uid"] == world.cluster_uid
    assert snapshots[0]["json"]["reporter"]["mode"] == "inject"


def test_the_first_snapshot_arrives_within_45_seconds_of_helm_returning(world, installed):
    # Pod start and the first POST. Image pull time is out of it: the kit's images are loaded into the cluster, and the
    # upstream ones are pulled while `--wait` waits.
    first = min(r["time"] for r in world.mock.requests("/v1/inventory", installed["mark"]) if r.get("status") == 202)
    assert first >= installed["started"]
    assert first - installed["returned"] <= 45, f"{first - installed['returned']:.0f} s after Helm returned"


# --- the same command again, a fresh one, a refused one ----------------------------------------------------------


def kit_pods() -> dict[str, str]:
    return {
        p["metadata"]["labels"]["app.kubernetes.io/name"]: p["metadata"]["uid"]
        for p in pods(NAMESPACE, "app.kubernetes.io/part-of=impact-ai")
        if not p["metadata"].get("deletionTimestamp")
        and p["metadata"]["labels"]["app.kubernetes.io/name"] != "exchange"
    }


def test_the_same_command_again_changes_nothing_and_calls_nobody(world, installed):
    before, secret = kit_pods(), key_secret()
    mark = world.mock.last_seq()
    # What `helm upgrade --reuse-values` hands the hook, twice in a row: Helm makes the Secret of the key only when the
    # cluster has none (a hook resource, with a delete policy that never deletes), so the working key is the very
    # object it was, not one made again: same resourceVersion, same key, nothing restarted.
    for _ in range(2):
        result = install_with_token(installed["token"], extra=FAST_POLL)
        assert result.returncode == 0, result.stdout + result.stderr
        assert key_secret()["metadata"]["uid"] == secret["metadata"]["uid"], "the Secret was deleted and made again"
        assert key_secret()["metadata"]["resourceVersion"] == secret["metadata"]["resourceVersion"]
    assert world.mock.exchanges(mark) == [], "a token that was exchanged for this key is not sent again"
    assert key_of(key_secret()) == key_of(secret)
    assert kit_pods() == before, "nothing restarted"


def test_a_refused_fresh_command_leaves_the_working_key_and_the_pods_alone(world, installed):
    before, secret = kit_pods(), key_secret()
    result = install_with_token(world.mock.token(expires_in=-1), extra=FAST_POLL, check=False)
    assert result.returncode != 0 and SENTENCES["token_expired"] in result.stdout + result.stderr
    assert key_of(key_secret()) == key_of(secret) and set(key_secret()["data"]) == {"ingest-key"}
    assert "impact.ai/pending-token" not in key_secret()["metadata"]["annotations"]
    assert kit_pods() == before
    # The release is "failed" for Helm; the command that follows is an upgrade of it.
    assert json.loads(helm("status", RELEASE, "-n", NAMESPACE, "-o", "json").stdout)["info"]["status"] == "failed"


def test_a_fresh_command_after_the_key_was_revoked_replaces_it_and_the_cluster_reports_again(world, installed):
    old_key, before = key_of(key_secret()), kit_pods()
    world.mock.revoke_keys()
    mark = world.mock.last_seq()
    wait_until(
        lambda: [r for r in world.mock.requests("/v1/inventory", mark) if r.get("status") == 401],
        timeout=90,
        message="the revoked key to be refused",
    )
    fresh = world.mock.token()
    mark = world.mock.last_seq()
    result = install_with_token(fresh, extra=FAST_POLL)
    assert result.returncode == 0, result.stdout + result.stderr

    new_key = key_of(key_secret())
    assert new_key != old_key and KEY_SHAPE.match(new_key)
    assert key_secret()["metadata"]["annotations"]["impact.ai/token"] == fingerprint(fresh)
    after = kit_pods()
    assert after["reporter"] != before["reporter"] and after["gateway"] != before["gateway"], (
        "a new key restarts what reads it"
    )
    wait_until(
        lambda: [
            r
            for r in world.mock.requests("/v1/inventory", mark)
            if r.get("status") == 202 and r["headers"]["Authorization"] == f"Bearer {new_key}"
        ],
        timeout=90,
        message="a snapshot accepted with the new key",
    )
    keys = world.mock.installs()["keys"]
    assert [k["revoked"] for k in keys] == [True, False], "the old key stays revoked"
