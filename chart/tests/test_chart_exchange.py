# SPDX-License-Identifier: Apache-2.0

"""AE-14: the install token. What `helm template` produces for a token install, without a cluster.

The command Cover your AI shows carries `install.token` and no ingest key. A Helm pre-install and pre-upgrade hook runs
`reporter exchange`, which makes the key inside the cluster and keeps it in a Secret of its own. The hook, its
permissions and the way the token reaches it are pinned here; the exchange itself is tested in
reporter/internal/exchange, and the whole thing against a cluster in e2e/test_7_exchange.py.
"""

from __future__ import annotations

import functools
import hashlib
import json

import pytest
from test_chart import CHART, CI_VALUES, DIGEST, UID, find, helm, kinds, pod_specs
from test_chart import render as render_uncached


@functools.cache
def _render(sets: tuple, release: str, namespace: str, values: bool, extra: tuple) -> tuple:
    return tuple(render_uncached(*sets, release=release, namespace=namespace, values=values, extra=extra))


def render(*sets: str, release: str = "impact-ai", namespace: str = "impact-ai", values: bool = True, extra=()) -> list:
    """helm template, once per distinct set of arguments: these tests only read what it renders."""
    return list(_render(sets, release, namespace, values, tuple(extra)))


TOKEN = "impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"
OTHER = "impactinstall_e2e_" + "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-ZyXwV"
KEY = "test-ingest-key"  # the key of ci/values.yaml, absent from every token install


def token_install(*sets: str, token: str = TOKEN, **kwargs) -> list:
    """A token install: the key of the CI values is switched off, the token given."""
    return render("ingest.key=", f"install.token={token}", *sets, **kwargs)


@pytest.fixture(scope="module")
def docs() -> list:
    return token_install()


def hooks_of(documents: list, event: str) -> list:
    return [
        d for d in documents if event in (d["metadata"].get("annotations") or {}).get("helm.sh/hook", "").split(",")
    ]


# --- no key in any manifest ----------------------------------------------------------------------------------------


def test_a_token_install_renders_an_empty_secret_for_the_key_and_no_key(docs):
    # The key is made in the cluster by the hook, into a Secret Helm makes empty: no manifest holds it, no value could.
    secrets = [d for d in docs if d["kind"] == "Secret"]
    assert {s["metadata"]["name"] for s in secrets} == {
        "impact-ai-operator-webhook-cert",
        "impact-ai-cover-cert",
        "impact-ai-install-token",
        "impact-ai-install-key",
    }
    for secret in secrets:
        assert "ingest-key" not in {**(secret.get("data") or {}), **(secret.get("stringData") or {})}
    assert KEY not in json.dumps(docs)
    key_secret = find(docs, "Secret", "impact-ai-install-key")
    assert not key_secret.get("data") and not key_secret.get("stringData"), "Helm makes it empty"
    assert key_secret["type"] == "Opaque"


def test_helm_makes_the_key_secret_first_and_never_deletes_it(docs):
    # The exchange hook may only get, patch and delete the Secret, so something else makes it: a hook resource of its
    # own that runs before every other hook. Its delete policy is hook-failed alone: Helm's default,
    # before-hook-creation, would delete the Secret and the key in it before an upgrade makes it again, and the other
    # two policies delete it when the hook succeeds. The template makes it only while the cluster has none.
    key_secret = find(docs, "Secret", "impact-ai-install-key")
    annotations = key_secret["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "hook-failed"
    weights = {
        d["metadata"]["name"] + "/" + d["kind"]: int(d["metadata"]["annotations"]["helm.sh/hook-weight"])
        for d in hooks_of(docs, "pre-install")
    }
    assert weights["impact-ai-install-key/Secret"] == min(weights.values()), "it exists before anything else runs"
    assert key_secret["metadata"]["labels"]["app.kubernetes.io/component"] == "ingest-key"
    assert key_secret["metadata"]["labels"]["app.kubernetes.io/instance"] == "impact-ai"
    source = (CHART / "templates" / "exchange-hook.yaml").read_text()
    assert 'if not (lookup "v1" "Secret" .Release.Namespace $secret)' in source, "made only while there is none"


def test_the_key_secret_of_a_pasted_key_is_unchanged_and_renders_only_for_a_key():
    assert find(render(), "Secret", "impact-ai-ingest-key")["stringData"] == {"ingest-key": KEY}
    for sets in (("ingest.key=", "ingest.existingSecret=mine"), ("ingest.key=", f"install.token={TOKEN}")):
        documents = render(*sets)
        assert not [d for d in documents if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-ingest-key")]


def test_the_token_is_in_the_hooks_secret_and_nowhere_else(docs):
    holders = [d["metadata"]["name"] for d in docs if TOKEN in json.dumps(d)]
    assert holders == ["impact-ai-install-token"]
    assert find(docs, "Secret", "impact-ai-install-token")["stringData"] == {"token": TOKEN}
    for _, spec in pod_specs(docs):
        assert TOKEN not in json.dumps(spec), "a pod spec holds the token: it reaches the Job through a mounted Secret"


def test_the_token_secret_is_a_hook_that_goes_away_with_the_job(docs):
    secret = find(docs, "Secret", "impact-ai-install-token")
    annotations = secret["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert set(annotations["helm.sh/hook-delete-policy"].split(",")) == {
        "before-hook-creation",
        "hook-succeeded",
        "hook-failed",
    }
    assert int(annotations["helm.sh/hook-weight"]) < 0, "it must exist before the Job runs"


def test_whitespace_around_the_token_is_not_part_of_it(tmp_path):
    values = tmp_path / "token.yaml"
    values.write_text(f'install:\n  token: "{TOKEN}\\n"\n')
    documents = render("ingest.key=", extra=("-f", str(values)))
    assert find(documents, "Secret", "impact-ai-install-token")["stringData"] == {"token": TOKEN}


# --- the hook ---------------------------------------------------------------------------------------------------


def test_the_exchange_hook_is_a_pre_install_and_pre_upgrade_job_with_its_own_permissions(docs):
    hooks = hooks_of(docs, "pre-install")
    assert {d["kind"] for d in hooks} == {"Secret", "ServiceAccount", "Role", "RoleBinding", "Job"}
    assert {d["metadata"]["annotations"]["helm.sh/hook"] for d in hooks} == {"pre-install,pre-upgrade"}
    assert hooks_of(docs, "pre-upgrade") == hooks
    for hook in hooks:
        policy = set(hook["metadata"]["annotations"]["helm.sh/hook-delete-policy"].split(","))
        if hook["metadata"]["name"] == "impact-ai-install-key":
            assert policy == {"hook-failed"}, "the Secret of the key is kept (test_helm_makes_the_key_secret_first...)"
        else:
            assert {"hook-succeeded", "hook-failed", "before-hook-creation"} <= policy, hook["metadata"]["name"]
        assert hook["metadata"]["namespace"] == "impact-ai"
    weights = {d["kind"]: int(d["metadata"]["annotations"]["helm.sh/hook-weight"]) for d in hooks}
    assert weights["Job"] > max(w for k, w in weights.items() if k != "Job"), "the Job runs after its permissions exist"
    assert weights["RoleBinding"] > weights["Role"], "bound after the role"
    # No cluster-wide permission at all: the hook writes one Secret of the release namespace.
    assert not [d for d in hooks if d["kind"].startswith("Cluster")]


def test_the_exchange_role_may_get_patch_and_delete_the_key_secret_and_nothing_else(docs):
    # The pin of the exchange hook's permissions: one rule, one Secret by name, three verbs. Get and patch store the
    # key; delete removes the Secret when a first install is refused, so that a refusal leaves nothing of the kit. No
    # create (it cannot be limited to a name, so a role that holds it may make any Secret of the namespace), no list,
    # no watch, no update, and no other Secret.
    assert find(docs, "Role", "impact-ai-exchange")["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["impact-ai-install-key"],
            "verbs": ["get", "patch", "delete"],
        },
    ]
    binding = find(docs, "RoleBinding", "impact-ai-exchange")
    assert binding["roleRef"]["name"] == "impact-ai-exchange"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "impact-ai-exchange", "namespace": "impact-ai"}]


def test_the_exchange_job_runs_the_reporter_image_with_what_the_exchange_needs(docs):
    job = find(docs, "Job", "impact-ai-exchange")
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"].startswith("ghcr.io/impact-ai/kubernetes-reporter:") and DIGEST.search(container["image"])
    args = container["args"]
    assert args[0] == "exchange"
    assert {
        "--ingest-endpoint=https://ingest.example.test",
        "--product-id=prod_test",
        "--secret-name=impact-ai-install-key",
        f"--cluster-uid={UID}",
        "--cluster-name=test-cluster",
        "--chart-version=0.0.0-dev",
    } <= set(args)
    assert not [a for a in args if TOKEN in a or "ingest-key=" in a], "neither token nor key on the command line"
    assert {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}} in container["env"]
    assert pod["serviceAccountName"] == "impact-ai-exchange"


def test_the_job_reads_the_token_from_a_mounted_secret(docs):
    pod = find(docs, "Job", "impact-ai-exchange")["spec"]["template"]["spec"]
    (volume,) = [v for v in pod["volumes"] if v["secret"]["secretName"] == "impact-ai-install-token"]
    assert volume["secret"]["items"] == [{"key": "token", "path": "token"}]
    (mount,) = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == volume["name"]]
    assert mount["readOnly"] is True
    assert (
        mount["mountPath"] == "/var/run/secrets/impact-install"
    )  # the exchange command's default --install-token-file


def test_the_job_is_bounded_locked_down_and_says_why_it_failed(docs):
    job = find(docs, "Job", "impact-ai-exchange")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["metadata"]["annotations"]["helm.sh/hook-weight"] == "0"
    # Helm prints the hook's logs when it fails: that is where the plain sentence comes out.
    assert job["metadata"]["annotations"]["helm.sh/hook-output-log-policy"] == "hook-failed"
    spec = job["spec"]
    assert spec["backoffLimit"] == 0, "a refusal is final; the command is run again, not retried by Kubernetes"
    assert 120 < spec["activeDeadlineSeconds"] <= 300
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["terminationMessagePolicy"] == "FallbackToLogsOnError"
    assert container["resources"]["limits"]["memory"]


def test_the_job_gets_the_arn_only_when_there_is_one():
    assert not [a for a in hook_args(token_install()) if a.startswith("--cluster-arn=")]
    arn = "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks"
    assert f"--cluster-arn={arn}" in hook_args(token_install(extra=("--set-string", f"cluster.arn={arn}")))


def hook_args(documents: list) -> list[str]:
    return find(documents, "Job", "impact-ai-exchange")["spec"]["template"]["spec"]["containers"][0]["args"]


def test_the_hook_honours_the_mirror_the_pull_secrets_and_the_placement():
    documents = token_install(
        "global.imageRegistry=registry.example.com/mirror",
        "imagePullSecrets[0].name=regcred",
        "nodeSelector.pool=system",
        "priorityClassName=kit",
    )
    pod = find(documents, "Job", "impact-ai-exchange")["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"].startswith("registry.example.com/mirror/impact-ai/kubernetes-reporter")
    assert pod["imagePullSecrets"] == [{"name": "regcred"}]
    assert pod["nodeSelector"] == {"pool": "system"}
    assert pod["priorityClassName"] == "kit"


# --- what reads the key ---------------------------------------------------------------------------------------------


def test_the_gateway_the_reporter_and_the_uninstall_hook_read_the_key_the_hook_made(docs):
    reporter = find(docs, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]
    assert reporter["volumes"][0]["secret"]["secretName"] == "impact-ai-install-key"
    gateway = find(docs, "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]
    assert gateway["env"][1]["valueFrom"]["secretKeyRef"] == {"name": "impact-ai-install-key", "key": "ingest-key"}
    uninstall = find(docs, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]
    assert uninstall["volumes"][0]["secret"]["secretName"] == "impact-ai-install-key"


def test_the_hook_made_secret_has_a_name_helm_never_uses_for_a_pasted_key():
    # A cluster that was installed with a pasted key has a Secret Helm owns, `<release>-ingest-key`. Switching it to a
    # token must not write the new key into a Secret that Helm deletes when it leaves the manifest.
    assert (
        find(render(), "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["volumes"][0]["secret"][
            "secretName"
        ]
        == "impact-ai-ingest-key"
    )
    assert (
        find(token_install(), "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["volumes"][0]["secret"][
            "secretName"
        ]
        == "impact-ai-install-key"
    )


# --- a new token rolls what reads the key; the same token rolls nothing ------------------------------------------


def key_checksums(documents: list) -> dict[str, str]:
    """checksum/key of what reads the key: the gateway, the reporter and, with a token, the cover pod."""
    names = ("impact-ai-gateway", "impact-ai-reporter", "impact-ai-cover")
    return {
        name: find(documents, "Deployment", name)["spec"]["template"]["metadata"]["annotations"]["checksum/key"]
        for name in names
    }


def test_a_new_token_restarts_the_gateway_and_the_reporter_and_the_same_token_does_not():
    one, again, two = (
        key_checksums(token_install()),
        key_checksums(token_install()),
        key_checksums(token_install(token=OTHER)),
    )
    assert one == again
    assert all(one.values()) and one["impact-ai-gateway"] != two["impact-ai-gateway"]
    assert one["impact-ai-reporter"] != two["impact-ai-reporter"]
    # The cover pod re-reads the key at every poll, but after a refusal it backs off for minutes: a new key restarts
    # it, so that it covers again at once instead of when the kubelet has refreshed the Secret and the backoff is over.
    assert one["impact-ai-cover"] != two["impact-ai-cover"]


def test_the_checksum_is_the_tokens_fingerprint_which_the_exchange_stores_in_the_secret():
    # reporter/internal/exchange fingerprint(): the first 16 hex characters of the token's sha256. The checksum of the
    # pods is the same value, so that "this token" means one thing in the chart and in the cluster.
    expected = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
    assert set(key_checksums(token_install()).values()) == {expected}
    assert TOKEN not in expected


def test_a_stale_token_on_reuse_values_changes_nothing_that_runs():
    # `helm upgrade --reuse-values` renders with the old token again; the pods' checksum is the same, so nothing rolls.
    assert key_checksums(token_install()) == key_checksums(token_install("reporter.interval=2m"))


# --- the ways to give the cluster its key --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sets",
    [
        ("install.token=" + TOKEN,),  # with the CI values' pasted key
        ("ingest.key=", "ingest.existingSecret=mine", "install.token=" + TOKEN),
    ],
)
def test_a_token_and_a_key_are_two_ways_and_not_both(sets):
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), *[a for s in sets for a in ("--set", s)], check=False
    )
    assert result.returncode != 0
    assert "install.token" in result.stderr and "ingest.key" in result.stderr and "use one" in result.stderr


def test_without_a_token_or_a_key_both_ways_are_named():
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "ingest.key=", check=False)
    assert result.returncode != 0
    assert "ingest.key is required" in result.stderr and "install.token" in result.stderr


def test_a_blank_token_is_no_token():
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "ingest.key=", "--set-string", "install.token=  ",
        check=False,
    )  # fmt: skip
    assert result.returncode != 0 and "ingest.key is required" in result.stderr


def test_the_cluster_id_is_needed_by_the_hook_too():
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "ingest.key=", "--set", f"install.token={TOKEN}",
        "--set", "cluster.uid=", check=False,
    )  # fmt: skip
    assert result.returncode != 0 and "cluster.uid is required" in result.stderr


def test_a_pasted_key_install_has_no_hook_before_install(docs):
    legacy = render()
    assert not hooks_of(legacy, "pre-install") and not hooks_of(legacy, "pre-upgrade")
    assert "Job" in kinds(legacy)  # the uninstall hook only
    assert [d["metadata"]["name"] for d in legacy if d["kind"] == "Job"] == ["impact-ai-uninstall"]


def test_the_token_install_passes_helm_lint():
    result = helm("lint", str(CHART), "-f", str(CI_VALUES), "--set", "ingest.key=", "--set", f"install.token={TOKEN}")
    assert "0 chart(s) failed" in result.stdout


def test_the_token_value_is_only_accepted_in_the_shape_of_a_token_or_blank():
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "ingest.key=", "--set", "install.token=has space",
        check=False,
    )  # fmt: skip
    assert result.returncode != 0 and "/install/token" in result.stderr


def test_the_uninstall_hook_starts_without_the_key_secret_a_refused_install_never_made():
    # `helm uninstall` of a release whose first install was refused runs the pre-delete hook; its pod must not wait
    # for a Secret that will not come (it would hold the uninstall until the Job's deadline). The volume is optional,
    # the notice finds no key and is skipped, and the hook ends at once.
    for documents in (token_install(), render()):
        (volume,) = find(documents, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]["volumes"]
        assert volume["secret"]["optional"] is True
        assert volume["secret"]["items"] == [{"key": "ingest-key", "path": "ingest-key"}]
