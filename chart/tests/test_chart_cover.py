# SPDX-License-Identifier: Apache-2.0

"""AE-15 automatic coverage, the uninstall that undoes it (AE-8), and the Node.js part of AE-20: what `helm template`
produces, without a cluster.

The cover pod adds the injection annotation to the pods of the workloads impact.ai lists, as they are created: no
workload object changes and nothing rolls. It has no Kubernetes permission. The cases that need a cluster (both webhook
orders against the pinned Operator, opt-outs, pod UIDs, the uninstall) are in e2e/test_8_*.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_chart import CHART, CI_VALUES, find, helm, kinds, pod_specs, unknown_fields
from test_chart_exchange import TOKEN, render, token_install

ROOT = Path(__file__).resolve().parents[2]
OPT_OUT = "impact.ai/opt-out"
OPENSSL = shutil.which("openssl") or "openssl"
NODEJS_DIGEST = "sha256:" + "c" * 64
NODEJS = ("images.nodejs.tag=v0.0.0-test", f"images.nodejs.digest={NODEJS_DIGEST}")


def cover(*sets: str, **kwargs) -> list:
    """A token install, which turns automatic coverage on by itself."""
    return token_install(*sets, **kwargs)


def cover_documents(documents: list) -> list:
    return [d for d in documents if d["metadata"]["name"].startswith("impact-ai-cover")]


def pod_webhook(documents: list, name: str) -> dict:
    configuration = next(
        d for d in documents if d["kind"] == "MutatingWebhookConfiguration" and d["metadata"]["name"] == name
    )
    (webhook,) = configuration["webhooks"]
    return webhook


# --- when the cover pod is there -------------------------------------------------------------------------------------


def test_automatic_coverage_comes_with_a_token_install_and_not_with_a_pasted_key():
    assert cover_documents(render()) == [], "a pasted key install is what it was: no cover pod"
    assert {d["kind"] for d in cover_documents(cover())} == {
        "ServiceAccount",
        "Secret",
        "Service",
        "Deployment",
        "MutatingWebhookConfiguration",
    }


@pytest.mark.parametrize(
    ("by_token", "sets", "installed"),
    [
        (False, ("cover.enabled=true",), True),
        (False, ("cover.enabled=auto",), False),
        (True, ("cover.enabled=false",), False),
        (True, ("cover.enabled=auto",), True),
        (True, ("operator.install=false",), False),  # nothing would inject: another injector owns the cluster
        (False, ("operator.install=false", "cover.enabled=true"), False),
    ],
)
def test_the_cover_value_decides(by_token, sets, installed):
    documents = cover(*sets) if by_token else render(*sets)
    assert bool(cover_documents(documents)) is installed


MIRROR = "global.imageRegistry=registry.example.com/mirror"


@pytest.mark.parametrize(
    ("sets", "installed"),
    [
        ((), True),  # a token install, no mirror: automatic coverage
        ((MIRROR,), False),  # auto behind a mirror: the covered workloads may not pull from it
        ((MIRROR, "cover.enabled=auto"), False),
        (
            (MIRROR, "cover.enabled=true"),
            True,
        ),  # asked for by name: the cluster says its workloads pull from the mirror
        ((MIRROR, "cover.enabled=false"), False),
        (("global.imageRegistry=",), True),  # an empty mirror is no mirror
    ],
)
def test_auto_installs_no_cover_pod_behind_an_image_mirror_and_true_installs_it(sets, installed):
    # The kit's own pods pull from the mirror with the release's pull secrets. The workloads the cover pod would cover
    # are in other namespaces and may not pull from it: the init container the Operator adds to their pods would wait
    # in ImagePullBackOff at their next rollout. `auto` therefore waits for the cluster to say so with `true`.
    documents = cover(*sets)
    assert bool(cover_documents(documents)) is installed
    # What follows the cover pod follows it: the Operator's reinvocation, the hook's cover flags.
    operator = pod_webhook_of(documents, "mpod.kb.io").get("reinvocationPolicy", "Never")
    assert (operator == "IfNeeded") is installed
    args = find(documents, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert ("--cover-deployment=impact-ai-cover" in args) is installed


def test_a_token_install_behind_a_mirror_still_reports_the_cover_fields_and_deletes_its_key():
    # Only the cover pod is left out: the install token is used, so the snapshot says what automatic coverage would read
    # and the uninstall deletes the key Secret the install hook made.
    documents = cover(MIRROR)
    assert "--cover-fields" in reporter_args(documents)
    args = find(documents, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--key-secret=impact-ai-install-key" in args


def test_cover_enabled_takes_true_false_and_auto_as_words_or_booleans():
    for value, installed in (("true", True), ("false", False), ("auto", False)):
        assert bool(cover_documents(render(f"cover.enabled={value}"))) is installed
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "cover.enabled=sometimes", check=False)
    assert result.returncode != 0 and "/cover/enabled" in result.stderr


# --- no Kubernetes permission, no token ------------------------------------------------------------------------------


def test_the_cover_pod_has_no_kubernetes_permission_and_mounts_no_token():
    documents = cover()
    account = find(documents, "ServiceAccount", "impact-ai-cover")
    assert account["automountServiceAccountToken"] is False
    pod = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "impact-ai-cover"
    assert not [v for v in pod["volumes"] if "projected" in v], "no projected service account token"
    # Nothing binds the account to anything.
    for d in documents:
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding"):
            assert not [s for s in d["subjects"] if s["name"] == "impact-ai-cover"], d["metadata"]["name"]


def test_the_cover_pod_is_not_given_the_reporters_permissions_either():
    documents = cover()
    assert (
        find(documents, "ClusterRole", "impact-ai-kit-reporter")["rules"]
        == find(render(), "ClusterRole", "impact-ai-kit-reporter")["rules"]
    )
    binding = find(documents, "ClusterRoleBinding", "impact-ai-kit-reporter")
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "impact-ai-reporter", "namespace": "impact-ai"}]


# --- the webhook configuration ------------------------------------------------------------------------------------


def test_the_cover_webhook_is_a_fail_open_pod_create_webhook_that_changes_nothing_else():
    webhook = pod_webhook(cover(), "impact-ai-cover")
    assert webhook["failurePolicy"] == "Ignore" and webhook["timeoutSeconds"] == 2
    assert webhook["sideEffects"] == "None" and webhook["admissionReviewVersions"] == ["v1"]
    assert webhook["rules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE"],
            "resources": ["pods"],
            "scope": "Namespaced",
        }
    ]
    assert webhook["clientConfig"]["service"] == {
        "name": "impact-ai-cover",
        "namespace": "impact-ai",
        "path": "/mutate",
        "port": 443,
    }
    assert webhook.get("reinvocationPolicy", "Never") == "Never"
    assert webhook["name"].count(".") >= 2, "a webhook name is a fully qualified domain name"


def operator_webhook_timeouts(documents: list) -> set[int]:
    return {
        webhook["timeoutSeconds"]
        for d in documents
        if d["kind"].endswith("WebhookConfiguration") and d["metadata"]["name"] != "impact-ai-cover"
        for webhook in d["webhooks"]
    }


def test_the_cover_webhook_waits_two_seconds_and_has_a_timeout_of_its_own():
    # The cover pod answers from memory and calls nothing, so 2 seconds is plenty. When the path to its pod drops
    # packets (a NetworkPolicy, a firewall on 9443) every pod creation in a covered namespace waits this long, then for
    # the Operator's webhook, so the cover webhook's wait is the one to keep short. It is not the Operator's setting.
    assert pod_webhook(cover(), "impact-ai-cover")["timeoutSeconds"] == 2
    assert operator_webhook_timeouts(cover()) == {5}
    documents = cover("webhook.timeoutSeconds=9")
    assert pod_webhook(documents, "impact-ai-cover")["timeoutSeconds"] == 2, "webhook.timeoutSeconds is the Operator's"
    assert operator_webhook_timeouts(documents) == {9}
    documents = cover("cover.timeoutSeconds=1")
    assert pod_webhook(documents, "impact-ai-cover")["timeoutSeconds"] == 1
    assert operator_webhook_timeouts(documents) == {5}
    assert pod_webhook(documents, "impact-ai-cover")["failurePolicy"] == "Ignore"


@pytest.mark.parametrize("value", ["0", "11", "-1", "soon"])
def test_the_cover_timeout_is_between_one_and_ten_seconds(value):
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), "--set", f"cover.timeoutSeconds={value}", check=False
    )
    assert result.returncode != 0 and "/cover/timeoutSeconds" in result.stderr, result.stderr


def test_the_selectors_honour_the_opt_out_of_namespaces_and_pods_and_skip_the_system_namespaces():
    webhook = pod_webhook(cover(), "impact-ai-cover")
    namespaces = webhook["namespaceSelector"]["matchExpressions"]
    by_operator = {e["operator"]: e for e in namespaces}
    assert by_operator["NotIn"]["key"] == "kubernetes.io/metadata.name"
    assert set(by_operator["NotIn"]["values"]) == {"kube-system", "kube-public", "kube-node-lease", "impact-ai"}
    assert by_operator["DoesNotExist"] == {"key": OPT_OUT, "operator": "DoesNotExist"}
    assert webhook["objectSelector"] == {"matchExpressions": [{"key": OPT_OUT, "operator": "DoesNotExist"}]}


def test_namespaces_include_and_exclude_reach_the_cover_webhook_like_the_operators():
    webhook = pod_webhook(cover("namespaces.include={shop,support}", "namespaces.exclude={legacy}"), "impact-ai-cover")
    by_operator = {e["operator"]: e for e in webhook["namespaceSelector"]["matchExpressions"]}
    assert set(by_operator["NotIn"]["values"]) == {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "impact-ai",
        "legacy",
    }
    assert by_operator["In"]["values"] == ["shop", "support"]
    assert OPT_OUT in {e["key"] for e in webhook["namespaceSelector"]["matchExpressions"]}


def test_the_cover_configuration_sorts_before_the_operators_and_the_name_can_be_changed():
    # The API server runs mutating webhook configurations in name order. Sorting first is the cheap way to be seen
    # by the Operator in one call; correctness does not depend on it (reinvocationPolicy), and the end-to-end test
    # flips the order on purpose with cover.webhookConfigName.
    documents = cover()
    names = sorted(d["metadata"]["name"] for d in documents if d["kind"] == "MutatingWebhookConfiguration")
    assert names == ["impact-ai-cover", "impact-ai-operator-mutation"]
    renamed = cover("cover.webhookConfigName=impact-ai-zz-cover")
    assert sorted(d["metadata"]["name"] for d in renamed if d["kind"] == "MutatingWebhookConfiguration") == [
        "impact-ai-operator-mutation",
        "impact-ai-zz-cover",
    ]


def test_the_operators_pod_webhook_is_called_again_when_a_later_webhook_changed_the_pod():
    on = pod_webhook_of(cover(), "mpod.kb.io")
    assert on["reinvocationPolicy"] == "IfNeeded"
    # Only where the cover pod is: a pasted key install keeps exactly the webhook it had.
    off = pod_webhook_of(render(), "mpod.kb.io")
    assert "reinvocationPolicy" not in off or off["reinvocationPolicy"] == "Never"


def pod_webhook_of(documents: list, name: str) -> dict:
    configuration = find(documents, "MutatingWebhookConfiguration", "impact-ai-operator-mutation")
    return next(w for w in configuration["webhooks"] if w["name"] == name)


# --- the certificate ---------------------------------------------------------------------------------------------


def certificate(documents: list) -> dict:
    return find(documents, "Secret", "impact-ai-cover-cert")


def test_the_webhook_certificate_is_generated_by_the_chart_and_matches_the_ca_bundle():
    documents = cover()
    secret = certificate(documents)
    assert secret["type"] == "kubernetes.io/tls"
    assert set(secret["data"]) == {"tls.crt", "tls.key", "ca.crt"}
    assert pod_webhook(documents, "impact-ai-cover")["clientConfig"]["caBundle"] == secret["data"]["ca.crt"]
    assert not [d for d in documents if "cert-manager.io" in d["apiVersion"]]


def openssl(args: list[str], pem: str) -> str:
    return subprocess.run(  # noqa: S603 - openssl with test-controlled input
        [OPENSSL, *args],
        input=pem,
        capture_output=True,
        text=True,
        check=True,  # noqa: S607
    ).stdout


def test_the_certificate_names_the_service_and_verifies_against_the_ca(tmp_path):
    secret = certificate(cover())
    crt, ca = (base64.b64decode(secret["data"][k]).decode() for k in ("tls.crt", "ca.crt"))
    text = openssl(["x509", "-noout", "-text"], crt)
    for name in ("impact-ai-cover", "impact-ai-cover.impact-ai", "impact-ai-cover.impact-ai.svc"):
        assert f"DNS:{name}" in text, (name, text)
    (tmp_path / "ca.pem").write_text(ca)
    (tmp_path / "crt.pem").write_text(crt)
    verified = subprocess.run(  # noqa: S603
        [OPENSSL, "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "crt.pem")],  # noqa: S607
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_the_cover_pod_restarts_with_its_certificate_and_not_otherwise():
    # `helm template` has no cluster to reuse a certificate from, so every render makes a new one. In a cluster the
    # chart reuses the Secret it made, so the checksum and the pod stay (e2e/test_8_auto_cover.py).
    documents = cover()
    annotations = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["metadata"]["annotations"]
    assert (
        annotations["checksum/cert"]
        == hashlib.sha256(certificate(documents)["data"]["tls.crt"].encode()).hexdigest()[:16]
    )
    # another render (another arguments, so not the cached one) makes another certificate
    other = find(cover("reporter.interval=2m"), "Deployment", "impact-ai-cover")["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert other["checksum/cert"] != annotations["checksum/cert"]


# --- the pod ----------------------------------------------------------------------------------------------------------


def test_the_cover_service_and_deployment():
    documents = cover()
    service = find(documents, "Service", "impact-ai-cover")
    (port,) = service["spec"]["ports"]
    assert port["port"] == 443 and port["targetPort"] == "webhook"
    deployment = find(documents, "Deployment", "impact-ai-cover")
    assert deployment["spec"]["replicas"] == 1
    assert service["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    assert (
        deployment["spec"]["selector"]["matchLabels"].items()
        <= deployment["spec"]["template"]["metadata"]["labels"].items()
    )
    assert find(cover("cover.replicas=2"), "Deployment", "impact-ai-cover")["spec"]["replicas"] == 2


def cover_container(documents: list) -> dict:
    (container,) = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]["containers"]
    return container


def test_the_cover_pod_runs_the_reporter_image_as_the_cover_command():
    container = cover_container(cover())
    assert (
        container["image"]
        == find(cover(), "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    args = container["args"]
    assert args[0] == "cover"
    assert {
        "--ingest-endpoint=https://ingest.example.test",
        "--instrumentation=impact-ai/impact",  # what the per-row command points the Operator at
        "--poll-interval=1m",
    } <= set(args)
    assert not [a for a in args if TOKEN in a]
    assert {p["name"]: p["containerPort"] for p in container["ports"]} == {"webhook": 9443, "health": 8081}


def test_the_instrumentation_it_points_the_operator_at_is_the_one_the_chart_installs():
    documents = cover()
    instrumentation = find(documents, "Instrumentation")
    assert (
        f"--instrumentation={instrumentation['metadata']['namespace']}/{instrumentation['metadata']['name']}"
        in cover_container(documents)["args"]
    )
    other = cover(namespace="kit", release="kit")
    (container,) = find(other, "Deployment", "kit-cover")["spec"]["template"]["spec"]["containers"]
    assert "--instrumentation=kit/impact" in container["args"]


def test_the_cover_pod_reads_the_key_the_certificate_and_keeps_its_list_in_an_empty_dir():
    pod = find(cover(), "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    key = volumes[mounts["/var/run/secrets/impact"]["name"]]
    assert key["secret"]["secretName"] == "impact-ai-install-key"
    assert key["secret"]["items"] == [{"key": "ingest-key", "path": "ingest-key"}]
    tls = volumes[mounts["/var/run/secrets/impact-cover-tls"]["name"]]
    assert tls["secret"]["secretName"] == "impact-ai-cover-cert"
    assert {i["key"] for i in tls["secret"]["items"]} == {"tls.crt", "tls.key"}
    state = volumes[mounts["/var/lib/impact-cover"]["name"]]
    assert "emptyDir" in state
    assert mounts["/var/run/secrets/impact"]["readOnly"] and mounts["/var/run/secrets/impact-cover-tls"]["readOnly"]
    assert not mounts["/var/lib/impact-cover"].get("readOnly")


def test_with_a_secret_the_customer_manages_the_cover_pod_reads_that_one():
    documents = render("ingest.key=", "ingest.existingSecret=my-key", "cover.enabled=true")
    pod = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]
    assert [v for v in pod["volumes"] if v.get("secret", {}).get("secretName") == "my-key"]


def test_the_cover_pod_is_locked_down_and_small():
    documents = cover()
    deployment = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]
    assert deployment["securityContext"]["runAsNonRoot"] is True
    container = deployment["containers"][0]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["livenessProbe"]["httpGet"]["port"] == "health"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["resources"]["limits"]["memory"] and container["resources"]["requests"]["cpu"]
    assert (
        find(cover("cover.resources.limits.memory=128Mi"), "Deployment", "impact-ai-cover")["spec"]["template"]["spec"][
            "containers"
        ][0]["resources"]["limits"]["memory"]
        == "128Mi"
    )


def test_every_pod_of_a_covered_install_runs_unprivileged_and_by_digest():
    documents = cover(*NODEJS)
    images = []
    for resource, spec in pod_specs(documents):
        name = resource["metadata"]["name"]
        assert spec["securityContext"].get("runAsNonRoot") is True, name
        for container in spec["containers"]:
            context = container["securityContext"]
            assert context["allowPrivilegeEscalation"] is False and context["readOnlyRootFilesystem"] is True, name
            assert context["capabilities"]["drop"] == ["ALL"], name
        images += [c["image"] for c in spec["containers"]]
    assert images and all("@sha256:" in i for i in images)


def test_the_cover_pod_honours_the_mirror_the_pull_secrets_and_the_placement():
    documents = cover(
        "global.imageRegistry=registry.example.com/mirror",
        "cover.enabled=true",  # `auto` leaves the cover pod out behind a mirror
        "imagePullSecrets[0].name=regcred",
        "nodeSelector.pool=system",
    )
    pod = find(documents, "Deployment", "impact-ai-cover")["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"].startswith("registry.example.com/mirror/")
    assert pod["imagePullSecrets"] == [{"name": "regcred"}] and pod["nodeSelector"] == {"pool": "system"}


# --- what the reporter reports -------------------------------------------------------------------------------------


def reporter_args(documents: list) -> list[str]:
    return find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["args"]


def test_the_reporter_adds_the_marker_and_the_labels_only_when_the_cover_pod_or_a_token_is_there():
    assert "--cover-fields" not in reporter_args(render())  # a server that does not know the fields never sees them
    assert "--cover-fields" in reporter_args(cover())
    assert "--cover-fields" in reporter_args(render("cover.enabled=true"))
    assert "--cover-fields" in reporter_args(cover("cover.enabled=false"))  # a token was used
    assert "--cover-fields" in reporter_args(cover("operator.install=false"))


# --- the uninstall that undoes it (AE-8) -------------------------------------------------------------------------


def uninstall_args(documents: list) -> list[str]:
    return find(documents, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]["containers"][0]["args"]


def test_the_uninstall_hook_of_a_covered_install_stops_the_cover_pod_and_deletes_the_key():
    args = uninstall_args(cover())
    assert {
        "--uninstall-notice",
        "--stop-deployment=impact-ai-reporter",
        "--cover-deployment=impact-ai-cover",
        "--key-secret=impact-ai-install-key",
    } <= set(args)
    # The marks on the pods come off in the uninstall script, under the operator's own rights, not in the hook.
    assert not [a for a in args if a.startswith("--strip")]
    selector = next(a for a in args if a.startswith("--cover-pod-selector=")).split("=", 1)[1]
    labels = find(cover(), "Deployment", "impact-ai-cover")["spec"]["template"]["metadata"]["labels"]
    assert dict(pair.split("=") for pair in selector.split(",")).items() <= labels.items()


def test_the_uninstall_hook_of_a_pasted_key_install_is_what_it_was():
    args = uninstall_args(render())
    assert not [a for a in args if a.startswith(("--cover-", "--strip-", "--key-secret"))]
    assert find(render(), "Role", "impact-ai-uninstall")["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments/scale"],
            "resourceNames": ["impact-ai-reporter"],
            "verbs": ["patch"],
        }
    ]


def test_the_uninstall_role_of_a_covered_token_install_is_pinned():
    # The pin of the uninstall hook's namespaced permissions. It scales two Deployments of the kit to zero, and deletes
    # the one Secret the install hook made. It can read no Secret: `delete` names it, and there is no get or list.
    rules = find(cover(), "Role", "impact-ai-uninstall")["rules"]
    assert rules == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments/scale"],
            "resourceNames": ["impact-ai-reporter", "impact-ai-cover"],
            "verbs": ["patch"],
        },
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["impact-ai-install-key"], "verbs": ["delete"]},
    ]
    granted = {(r, v) for rule in rules for r in rule["resources"] for v in rule["verbs"]}
    assert not {v for r, v in granted if r == "secrets"} & {"get", "list", "watch"}


def test_the_uninstall_hook_holds_no_permission_on_pods_beyond_reading_them():
    # It used to patch pods in every namespace while it ran, to take the cover pod's marks off live pods: a pod patch
    # can change a running container's image, and the marks are cosmetic. The uninstall script takes them off.
    for documents in (cover(), render()):
        assert not [d for d in documents if "uninstall-strip" in d["metadata"]["name"]]
        assert "impact-ai-kit-uninstall-strip" not in json.dumps(documents)
        binding = find(documents, "ClusterRoleBinding", "impact-ai-kit-uninstall")
        assert binding["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "impact-ai-kit-reporter",
        }, "the hook reads like the reporter does, and that is all"
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "impact-ai-uninstall", "namespace": "impact-ai"}
        ]
        annotations = binding["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-delete"
        assert {"hook-succeeded", "hook-failed"} <= set(annotations["helm.sh/hook-delete-policy"].split(","))
    # The reporter that runs all the time never had it either.
    assert (
        find(cover(), "ClusterRole", "impact-ai-kit-reporter")["rules"]
        == find(render(), "ClusterRole", "impact-ai-kit-reporter")["rules"]
    )


SCRIPT_SELECTOR = re.compile(r'delete (?:\S+) --selector "([^"]+)"')


def test_the_cluster_scoped_objects_of_the_kit_carry_the_labels_the_uninstall_script_selects_them_by():
    # The last step of the script deletes what an uninstall that was cut short leaves behind, by label, for the release
    # it is given: the kit's own label and the release's. A hook's binding is not Helm's, so only this finds it.
    script = (ROOT / "scripts" / "uninstall-cluster-kit.sh").read_text()
    (selector,) = SCRIPT_SELECTOR.findall(script)
    scoped = ("ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")
    for release, namespace in (("impact-ai", "impact-ai"), ("kit-a", "kit")):
        documents = cover(release=release, namespace=namespace)
        want = dict(term.split("=", 1) for term in selector.replace("$RELEASE", release).split(","))
        assert set(want) == {"app.kubernetes.io/part-of", "app.kubernetes.io/instance"}
        ours = [(d["kind"], d["metadata"]["name"]) for d in documents if d["kind"] in scoped]
        # every cluster-scoped object of the kit: the reporter's, the Operator's, the cover pod's and the hooks'
        assert {
            ("ClusterRole", "impact-ai-kit-reporter"),
            ("ClusterRoleBinding", "impact-ai-kit-uninstall"),
            ("ClusterRole", "impact-ai-operator-manager"),
            ("ClusterRoleBinding", "impact-ai-operator-manager"),
            ("MutatingWebhookConfiguration", "impact-ai-cover"),
            ("MutatingWebhookConfiguration", "impact-ai-operator-mutation"),
            ("ValidatingWebhookConfiguration", "impact-ai-operator-validation"),
        } <= set(ours)
        for kind, name in ours:
            labels = find(documents, kind, name)["metadata"]["labels"]
            assert labels.items() >= want.items(), (kind, name, labels)


def test_a_covered_install_has_no_cluster_scoped_object_a_second_release_would_clash_on_by_name():
    first, other = cover(), cover(release="something-else", namespace="elsewhere")
    scoped = lambda documents: {  # noqa: E731
        (d["kind"], d["metadata"]["name"])
        for d in documents
        if d["kind"]
        in ("ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")
    }
    assert scoped(first) == scoped(other)


def test_the_uninstall_job_has_the_time_to_stop_both_pods_and_send_the_notice():
    spec = find(cover(), "Job", "impact-ai-uninstall")["spec"]
    # 60 s to stop the reporter's pods + 60 s for the cover pod's + 60 s to send, with slack.
    assert spec["activeDeadlineSeconds"] >= 240
    # Without the cover pod there is one set of pods to wait for.
    assert find(render(), "Job", "impact-ai-uninstall")["spec"]["activeDeadlineSeconds"] >= 180


def test_the_hook_objects_of_the_covered_uninstall_all_go_when_it_is_done():
    hooks = [d for d in cover() if (d["metadata"].get("annotations") or {}).get("helm.sh/hook") == "pre-delete"]
    assert {d["kind"] for d in hooks} == {"ServiceAccount", "Role", "RoleBinding", "ClusterRoleBinding", "Job"}
    for hook in hooks:
        assert {"hook-succeeded", "before-hook-creation"} <= set(
            hook["metadata"]["annotations"]["helm.sh/hook-delete-policy"].split(",")
        )


# --- Node.js (AE-20, the chart's part) ------------------------------------------------------------------------------


def operator_args(documents: list) -> list[str]:
    return find(documents, "Deployment", "impact-ai-operator")["spec"]["template"]["spec"]["containers"][0]["args"]


def test_the_operator_injects_node_and_python_and_nothing_else():
    args = operator_args(render())
    assert "--enable-nodejs-instrumentation=true" in args
    for language in ("java", "dotnet", "apache-httpd"):
        assert f"--enable-{language}-instrumentation=false" in args
    assert not [a for a in args if a.startswith(("--enable-go", "--enable-nginx"))]


def test_the_node_image_is_in_the_instrumentation_once_the_release_has_one():
    assert "nodejs" not in find(render(), "Instrumentation")["spec"], "a chart without a Node image injects no Node"
    spec = find(render(*NODEJS), "Instrumentation")["spec"]
    assert set(spec) == {"imagePullPolicy", "initContainerSecurityContext", "python", "nodejs"}
    nodejs = spec["nodejs"]
    assert nodejs["image"] == f"ghcr.io/impact-ai/autoinstrumentation-nodejs:v0.0.0-test@{NODEJS_DIGEST}"
    assert nodejs["resourceRequirements"]["limits"]["memory"] == "64Mi"
    assert nodejs["resourceRequirements"]["requests"]["cpu"] == "10m"


def test_node_gets_the_gateway_and_the_operators_defaults_are_suppressed_as_for_python():
    spec = find(render(*NODEJS), "Instrumentation")["spec"]
    python, nodejs = ({e["name"]: e for e in spec[language]["env"]} for language in ("python", "nodejs"))
    assert nodejs["IMPACT_OTLP_ENDPOINT"] == python["IMPACT_OTLP_ENDPOINT"]
    assert set(nodejs) == set(python)
    for name in set(nodejs) - {"IMPACT_OTLP_ENDPOINT"}:
        ref = nodejs[name]["valueFrom"]["configMapKeyRef"]
        assert ref["optional"] is True and "value" not in nodejs[name]


def test_the_instrumentation_with_node_only_uses_fields_its_crd_defines():
    crd = yaml.safe_load((CHART / "crds" / "instrumentations.opentelemetry.io.yaml").read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    assert unknown_fields(schema, {"spec": find(render(*NODEJS), "Instrumentation")["spec"]}) == []


def test_the_node_image_follows_the_mirror_and_is_pinned_by_digest():
    spec = find(render(*NODEJS, "global.imageRegistry=registry.example.com/mirror"), "Instrumentation")["spec"]
    assert spec["nodejs"]["image"].startswith("registry.example.com/mirror/impact-ai/autoinstrumentation-nodejs:")
    assert spec["nodejs"]["image"].endswith(f"@{NODEJS_DIGEST}")


def test_node_is_not_injected_when_another_injector_owns_the_cluster():
    documents = render(*NODEJS, "operator.install=false")
    assert "Instrumentation" not in kinds(documents)
