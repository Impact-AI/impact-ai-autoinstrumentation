# SPDX-License-Identifier: Apache-2.0

"""Render checks for the cluster kit chart: what `helm template` produces, without a cluster.

The checks that need a cluster (lookup: another injector, a second product, the real webhook and injection) are in
e2e/. Here: the settings that install, names only, fail open and uninstall depend on, and the names the Cover
your AI commands rely on.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "impact-ai"
CI_VALUES = CHART / "ci" / "values.yaml"
OPERATOR_CHART = CHART / "charts" / "opentelemetry-operator"
UID = "4e5622e7-f420-499c-9662-50af7f8a64b9"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
IMAGE_ARGUMENT = re.compile(r"--[a-z0-9-]+-image=")


def helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["helm", *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603, S607
    if check and result.returncode != 0:
        raise AssertionError(f"helm {' '.join(args)} failed:\n{result.stderr}")
    return result


def render(*sets: str, release: str = "impact-ai", namespace: str = "impact-ai", values: bool = True, extra=()) -> list:
    command = ["template", release, str(CHART), "-n", namespace]
    if values:
        command += ["-f", str(CI_VALUES)]
    command += list(extra)
    for item in sets:
        command += ["--set", item]
    documents = yaml.safe_load_all(helm(*command).stdout)
    return [d for d in documents if d]


def find(documents: list, kind: str, name: str | None = None) -> dict:
    matches = [d for d in documents if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]
    assert len(matches) == 1, (
        f"{kind}/{name}: {len(matches)} matches among {[(d['kind'], d['metadata']['name']) for d in documents]}"
    )
    return matches[0]


def kinds(documents: list) -> set[str]:
    return {d["kind"] for d in documents}


def pod_specs(documents: list):
    for d in documents:
        if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet"):
            yield d, d["spec"]["template"]["spec"]
        elif d["kind"] == "Job":
            yield d, d["spec"]["template"]["spec"]


def image_references(documents: list) -> list[tuple[str, str]]:
    """Every image reference of a render, as (where, reference): the `image` of every container and init container and
    of every language of an Instrumentation, wherever it is, and the value of every `--<name>-image=` argument (the
    Operator's defaults for what it would create)."""
    found = []

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    found.append((f"{where}.image", value))
                else:
                    walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, str) and IMAGE_ARGUMENT.match(item):
                    flag, _, value = item.partition("=")
                    found.append((f"{where}[{flag}]", value))
                else:
                    walk(item, f"{where}[{index}]")

    for document in documents:
        walk(document, f"{document['kind']}/{document['metadata']['name']}:")
    return found


def operator_args(documents: list) -> list[str]:
    return find(documents, "Deployment", "impact-ai-operator")["spec"]["template"]["spec"]["containers"][0]["args"]


def operator_languages() -> set[str]:
    """The languages the pinned Operator can inject, by the names of its flags (apache-httpd, dotnet, nodejs, ...): each
    the vendored deployment template has an image or a switch for, and each the Instrumentation kind has a section for.
    The second finds what the first does not: the template of chart 0.128.0 has no PHP, which Operator 0.162.0 injects
    when it is switched on."""
    template = (OPERATOR_CHART / "templates" / "deployment.yaml").read_text()
    languages = set(re.findall(r"--auto-instrumentation-([a-z0-9-]+)-image=", template))
    languages |= set(re.findall(r"--enable-([a-z0-9-]+)-instrumentation\b", template)) - {"multi"}
    crd = yaml.safe_load((CHART / "crds" / "instrumentations.opentelemetry.io.yaml").read_text())
    for version in crd["spec"]["versions"]:
        sections = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        for name, section in sections.items():
            if "image" in section.get("properties", {}):  # a language: apacheHttpd is apache-httpd in the flags
                languages.add(re.sub(r"[A-Z]", lambda capital: "-" + capital.group().lower(), name))
    return languages


def language_switches(args: list[str]) -> dict[str, str]:
    """What the Operator's --enable-<language>-instrumentation flags say, read as it reads them: a flag without a value
    is true, and the last flag of a language wins. --enable-multi-instrumentation is not a language."""
    switches = {}
    for arg in args:
        match = re.fullmatch(r"--enable-([a-z0-9-]+)-instrumentation(?:=(.*))?", arg)
        if match and match.group(1) != "multi":
            switches[match.group(1)] = match.group(2) or "true"
    return switches


@pytest.fixture(scope="module")
def docs() -> list:
    return render()


# --- the install the page shows ---------------------------------------------------------------------------------------


def test_lint_is_clean():
    result = helm("lint", str(CHART), "-f", str(CI_VALUES))
    assert "0 chart(s) failed" in result.stdout


def test_the_names_the_commands_use(docs):
    # Release impact-ai in namespace impact-ai; the Instrumentation is named impact in the release namespace, which
    # is what the per-row command's annotation points at (inject-python: impact-ai/impact)
    instrumentation = find(docs, "Instrumentation")
    assert instrumentation["metadata"]["name"] == "impact"
    assert instrumentation["metadata"]["namespace"] == "impact-ai"
    for kind in ("Deployment", "Service"):
        assert find(docs, kind, "impact-ai-gateway")["metadata"]["namespace"] == "impact-ai"
    assert find(docs, "Deployment", "impact-ai-reporter")["metadata"]["namespace"] == "impact-ai"


def test_what_the_chart_installs(docs):
    assert {(d["kind"], d["metadata"]["name"]) for d in docs if d["kind"] in ("Deployment", "Instrumentation")} == {
        ("Deployment", "impact-ai-operator"),
        ("Deployment", "impact-ai-gateway"),
        ("Deployment", "impact-ai-reporter"),
        ("Instrumentation", "impact"),
    }
    assert {d["kind"] for d in docs} >= {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration", "Secret"}


def test_no_cert_manager(docs):
    assert not [d for d in docs if "cert-manager.io" in d["apiVersion"]]
    annotated = [d for d in docs if any("cert-manager" in k for k in (d["metadata"].get("annotations") or {}))]
    assert annotated == []


def test_the_chart_ships_only_the_instrumentation_crd():
    crds = list((CHART / "crds").glob("*.yaml"))
    assert [p.name for p in crds] == ["instrumentations.opentelemetry.io.yaml"]
    crd = yaml.safe_load(crds[0].read_text())
    assert crd["kind"] == "CustomResourceDefinition"
    assert crd["metadata"]["name"] == "instrumentations.opentelemetry.io"
    # No conversion webhook: the CRD stands alone, so keeping it after an uninstall leaves nothing that calls us.
    assert "conversion" not in crd["spec"] or crd["spec"]["conversion"].get("strategy") == "None"


def test_no_other_custom_resource_definition_is_rendered(docs):
    assert "CustomResourceDefinition" not in kinds(docs)  # CRDs come from crds/, which Helm never deletes


# --- fail open -------------------------------------------------------------------------------------------------------


def test_every_webhook_fails_open_with_a_five_second_timeout(docs):
    webhooks = [w for d in docs if d["kind"].endswith("WebhookConfiguration") for w in d["webhooks"]]
    assert {w["name"] for w in webhooks} == {
        "minstrumentation.kb.io",
        "mpod.kb.io",
        "vinstrumentationcreateupdate.kb.io",
    }
    assert {w["failurePolicy"] for w in webhooks} == {"Ignore"}
    assert {w["timeoutSeconds"] for w in webhooks} == {5}
    assert {w["sideEffects"] for w in webhooks} == {"None"}


def test_the_pod_webhook_skips_the_system_namespaces_and_its_own(docs):
    mutating = find(docs, "MutatingWebhookConfiguration")
    pod = next(w for w in mutating["webhooks"] if w["name"] == "mpod.kb.io")
    (expression,) = pod["namespaceSelector"]["matchExpressions"]
    assert expression["key"] == "kubernetes.io/metadata.name"
    assert expression["operator"] == "NotIn"
    assert set(expression["values"]) == {"kube-system", "kube-public", "kube-node-lease", "impact-ai"}


def test_the_instrumentation_webhooks_only_look_at_the_release_namespace(docs):
    for kind in ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"):
        for webhook in find(docs, kind)["webhooks"]:
            if webhook["name"] != "mpod.kb.io":
                assert webhook["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "impact-ai"}}


def test_namespaces_include_and_exclude_reach_the_webhook_and_the_reporter():
    documents = render("namespaces.include={shop,support}", "namespaces.exclude={legacy}")
    pod = next(w for w in find(documents, "MutatingWebhookConfiguration")["webhooks"] if w["name"] == "mpod.kb.io")
    by_operator = {e["operator"]: e["values"] for e in pod["namespaceSelector"]["matchExpressions"]}
    assert set(by_operator["NotIn"]) == {"kube-system", "kube-public", "kube-node-lease", "impact-ai", "legacy"}
    assert by_operator["In"] == ["shop", "support"]
    args = find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--include-namespaces=shop,support" in args
    assert "--exclude-namespaces=legacy" in args


def test_failure_policy_cannot_be_set_to_fail():
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "webhook.failurePolicy=Fail", check=False)
    assert result.returncode != 0
    assert "failurePolicy" in result.stderr


def test_the_webhook_certificate_is_generated_not_cert_manager(docs):
    secret = find(docs, "Secret", "impact-ai-operator-webhook-cert")
    assert secret["type"] == "kubernetes.io/tls"
    assert set(secret["data"]) == {"tls.crt", "tls.key", "ca.crt"}
    mutating = find(docs, "MutatingWebhookConfiguration")
    assert all(w["clientConfig"]["caBundle"] == secret["data"]["ca.crt"] for w in mutating["webhooks"])
    deployment = find(docs, "Deployment", "impact-ai-operator")
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    assert {"name": "cert", "secret": {"defaultMode": 420, "secretName": "impact-ai-operator-webhook-cert"}} in volumes


# --- Python only ------------------------------------------------------------------------------------------------------


def test_python_and_node_are_switched_on_and_every_other_language_off_by_name(docs):
    # Node.js joins Python. The Operator may inject it; the Instrumentation gets a Node.js image once the release
    # has one, so this render (no Node.js image) still holds Python alone (test_chart_cover.py has the Node.js cases).
    # Every other language the pinned Operator can inject is switched off by name: one left to the Operator's default
    # is one an Operator bump can switch on. The languages come from the vendored chart, so a bump that brings a new one
    # fails here until values.yaml switches it off.
    languages = operator_languages()
    assert {"python", "nodejs", "java"} < languages, f"the languages were not found in the vendored chart: {languages}"
    expected = {language: "true" if language in ("python", "nodejs") else "false" for language in languages}
    assert language_switches(operator_args(docs)) == expected
    assert "--ignore-missing-collector-crds=true" in operator_args(docs)
    spec = find(docs, "Instrumentation")["spec"]
    assert set(spec) == {"imagePullPolicy", "initContainerSecurityContext", "python"}


def test_the_language_switches_are_read_as_the_operator_reads_them():
    # pflag: a bare boolean flag is true and the last one wins (the vendored template renders a bare
    # --enable-go-instrumentation when operator.manager.autoInstrumentation.go.enabled is true, before extraArgs)
    assert language_switches(["--enable-go-instrumentation", "--enable-go-instrumentation=false"]) == {"go": "false"}
    assert language_switches(["--enable-ruby-instrumentation"]) == {"ruby": "true"}
    assert language_switches(["--enable-multi-instrumentation=true", "--enable-leader-election"]) == {}
    assert "php" in operator_languages(), "the Instrumentation kind has a PHP section, the template has none"


def test_the_instrumentation_sets_the_gateway_and_leaves_other_tracers_environment_alone(docs):
    python = find(docs, "Instrumentation")["spec"]["python"]
    env = {e["name"]: e for e in python["env"]}
    assert env["IMPACT_OTLP_ENDPOINT"]["value"] == "http://impact-ai-gateway.impact-ai.svc:4318"
    # The Operator adds these unless they are defined; they are defined as optional references to a missing key, so
    # Kubernetes leaves them out. ddtrace 4 would otherwise export OTLP to localhost:4318 (OTEL_TRACES_EXPORTER).
    for name in (
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
    ):
        ref = env[name]["valueFrom"]["configMapKeyRef"]
        assert ref["optional"] is True
        assert "value" not in env[name]
    # Whether the workload trusts its callers' user, session and use case: the pod's own annotation, read by the
    # downward API (an empty value when the pod has none, which the package reads as not trusted).
    assert env["IMPACT_TRUST_CALLER_BAGGAGE"]["valueFrom"] == {
        "fieldRef": {"fieldPath": "metadata.annotations['impact.ai/trust-caller-baggage']"}
    }
    # Nothing else: the package applies its own defaults only when it starts (a stood-down workload keeps its env).
    assert set(env) == {"IMPACT_OTLP_ENDPOINT", "IMPACT_TRUST_CALLER_BAGGAGE"} | {
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
    }


def test_the_instrumentation_resources_and_image(docs):
    python = find(docs, "Instrumentation")["spec"]["python"]
    assert DIGEST.search(python["image"])
    assert python["image"].startswith("ghcr.io/impact-ai/autoinstrumentation-python:")
    assert python["resourceRequirements"]["limits"]["memory"] == "64Mi"
    assert python["resourceRequirements"]["requests"]["cpu"] == "10m"


# --- images by digest, with a mirror -------------------------------------------------------------------------------


def test_every_image_is_pinned_by_digest(docs):
    references = image_references(docs)
    assert any(where.endswith("[--collector-image]") for where, _ in references)
    assert [(where, image) for where, image in references if not DIGEST.search(image)] == []


MIRROR = "registry.example.com/mirror"
NODEJS_IMAGE = ("images.nodejs.tag=v0.0.0-test", "images.nodejs.digest=sha256:" + "c" * 64)
TOKEN_INSTALL = ("ingest.key=", "install.token=impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE")


def every_operator_image_argument() -> tuple[str, ...]:
    """--set values that give each image argument of the vendored Operator chart an image under an upstream name, read
    from its template: `--collector-image=`, `--auto-instrumentation-<language>-image=` and the others."""
    template = (OPERATOR_CHART / "templates" / "deployment.yaml").read_text()
    images = sorted(
        set(re.findall(r"\{\{- if and \.Values\.(manager\.[A-Za-z0-9.]+)\.repository \.Values\.\1\.tag \}\}", template))
    )
    assert len(images) == len(IMAGE_ARGUMENT.findall(template)) > 1, "an image argument of another shape"
    return tuple(
        value
        for number, image in enumerate(images)
        for value in (
            f"operator.{image}.repository=ghcr.io/upstream/image-{number}",
            f"operator.{image}.tag=1.{number}.0",
        )
    )


@pytest.mark.parametrize(
    "shape",
    [NODEJS_IMAGE, (*TOKEN_INSTALL, "cover.enabled=true", *NODEJS_IMAGE)],
    ids=["a pasted key", "a token, with the cover pod"],
)
def test_one_value_points_every_image_at_a_mirror(shape):
    # The README promises that one value covers every image. An air-gapped cluster's scanner reads the whole render,
    # the Operator's arguments too, and flags an image outside the mirror whether anything pulls it or not.
    documents = render(f"global.imageRegistry={MIRROR}", *shape)
    references = image_references(documents)
    assert [(where, image) for where, image in references if not image.startswith(f"{MIRROR}/")] == []
    # what that covers: every pod of the render, the Instrumentation's images and the Operator's arguments
    holders = {where.split(":", 1)[0] for where, _ in references}
    pods = {f"{d['kind']}/{d['metadata']['name']}" for d, _ in pod_specs(documents)}
    assert pods | {"Instrumentation/impact"} <= holders
    collector = f"--collector-image={MIRROR}/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector:"
    assert [a for a in operator_args(documents) if a.startswith(collector)]
    # and nothing else in the render names the registry the images come from
    assert "ghcr.io" not in json.dumps(documents)


def test_every_image_argument_of_the_operator_follows_the_mirror():
    # The vendoring script sends each of them through the kit's image helper; with all of them given, all are rendered.
    given = every_operator_image_argument()
    documents = render(f"global.imageRegistry={MIRROR}", *given)
    arguments = [a for a in operator_args(documents) if IMAGE_ARGUMENT.match(a)]
    assert len(arguments) == len(given) // 2
    assert [a for a in arguments if not a.partition("=")[2].startswith(f"{MIRROR}/upstream/image-")] == []
    assert "ghcr.io" not in json.dumps(documents)


def test_an_image_without_tag_or_digest_is_refused():
    result = helm(
        "template",
        "x",
        str(CHART),
        "-f",
        str(CI_VALUES),
        "--set",
        "images.reporter.tag=,images.reporter.digest=",
        check=False,
    )
    assert result.returncode != 0
    assert "neither a tag nor a digest" in result.stderr


# --- the reporter reads names, never values ------------------------------------------------------------------------


def rules_of(documents, kind, name):
    return find(documents, kind, name)["rules"]


def test_the_reporter_can_read_workloads_pods_and_namespaces_but_never_secrets_or_configmaps(docs):
    rules = rules_of(docs, "ClusterRole", "impact-ai-kit-reporter")
    granted = {(g, r, v) for rule in rules for g in rule["apiGroups"] for r in rule["resources"] for v in rule["verbs"]}
    assert ("apps", "deployments", "list") in granted
    assert ("apps", "statefulsets", "list") in granted
    assert ("apps", "daemonsets", "list") in granted
    assert ("batch", "cronjobs", "list") in granted
    assert ("batch", "jobs", "list") in granted
    assert ("", "pods", "list") in granted
    assert ("", "namespaces", "get") in granted
    assert ("admissionregistration.k8s.io", "mutatingwebhookconfigurations", "list") in granted
    assert {v for _, _, v in granted} <= {"get", "list"}  # read only
    assert not {r for _, r, _ in granted} & {"secrets", "configmaps", "*"}


def test_nothing_the_kit_binds_to_the_reporter_or_the_hook_can_read_secrets_or_configmaps(docs):
    for name in ("impact-ai-kit-reporter",):
        for rule in rules_of(docs, "ClusterRole", name):
            assert not set(rule["resources"]) & {"secrets", "configmaps", "*"}
    hook = rules_of(docs, "Role", "impact-ai-uninstall")
    assert hook == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments/scale"],
            "resourceNames": ["impact-ai-reporter"],
            "verbs": ["patch"],
        }
    ]


# --- the Operator's role: injecting an Instrumentation, nothing else ------------------------------------------------
#
# The OpenTelemetry Operator's own chart gives it a role for the collector, target allocator and OpAMP features this
# kit does not install: create and delete on pods, workloads, Services and policies in every namespace (a pod that
# mounts any Secret is one create away), and below Kubernetes 1.33 nodes/proxy. The kit writes the role itself
# (templates/operator-rbac.yaml). These tests pin what it may do, and `make lint` runs trivy on the render.

OPERATOR = "impact-ai-operator"  # its ServiceAccount, its ClusterRole (-manager) and its Role (-leader-election)
WRITES = {"create", "update", "patch", "delete", "deletecollection"}
# What the Operator may change: its Instrumentation resources, the events it records, the two review requests its
# metrics endpoint makes, and its own leader-election Lease. (API group, resource)
OPERATOR_MAY_WRITE = {
    ("opentelemetry.io", "instrumentations"),
    ("opentelemetry.io", "instrumentations/status"),
    ("", "events"),
    ("events.k8s.io", "events"),
    ("authentication.k8s.io", "tokenreviews"),
    ("authorization.k8s.io", "subjectaccessreviews"),
    ("coordination.k8s.io", "leases"),
}
# In no role the kit creates: Secrets (the ingest key and every Helm release record are Secrets), the kubelet's proxy
# (a read of it runs commands in any pod on the node), commands and tunnels into pods, a wildcard, and the rights to
# hand out rights.
NEVER_RESOURCES = {
    "secrets",
    "nodes/proxy",
    "pods/exec",
    "pods/attach",
    "pods/portforward",
    "serviceaccounts/token",
    "*",
}
NEVER_VERBS = {"*", "escalate", "bind", "impersonate"}


def granted(rules: list) -> set[tuple[str, str, str]]:
    """(API group, resource, verb) for every grant of the rules."""
    return {
        (group, resource, verb)
        for rule in rules
        for group in rule.get("apiGroups", [])
        for resource in rule.get("resources", [])
        for verb in rule["verbs"]
    }


def roles_bound_to(documents: list, account: str) -> list:
    """The Role and ClusterRole objects that the ServiceAccount is given, found through the bindings."""
    roles = []
    for binding in documents:
        if binding["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if any(s["kind"] == "ServiceAccount" and s["name"] == account for s in binding.get("subjects", [])):
            roles.append(find(documents, binding["roleRef"]["kind"], binding["roleRef"]["name"]))
    return roles


def operator_grants(documents: list) -> set[tuple[str, str, str]]:
    return granted([rule for role in roles_bound_to(documents, OPERATOR) for rule in role["rules"]])


def writes_outside_the_allowlist(rules: list) -> set[tuple[str, str]]:
    return {(group, resource) for group, resource, verb in granted(rules) if verb in WRITES} - OPERATOR_MAY_WRITE


def never_granted(rules: list) -> list[dict]:
    """The rules that name something no role of the kit may hold."""
    return [
        rule
        for rule in rules
        if set(rule.get("resources", [])) & NEVER_RESOURCES
        or set(rule["verbs"]) & NEVER_VERBS
        or "*" in rule.get("apiGroups", [])
        or rule.get("nonResourceURLs")
    ]


@pytest.mark.parametrize("version", ["1.27.0", "1.32.9", "1.33.0", "1.34.0", "v1.31.2-eks-abcdef"])
def test_no_role_the_kit_creates_reaches_secrets_the_kubelet_proxy_a_shell_in_a_pod_or_a_wildcard(version):
    documents = render(extra=("--kube-version", version))
    roles = [d for d in documents if d["kind"] in ("Role", "ClusterRole")]
    assert {r["metadata"]["name"] for r in roles} >= {"impact-ai-kit-reporter", "impact-ai-operator-manager"}
    for role in roles:
        name = role["metadata"]["name"]
        assert "aggregationRule" not in role, name  # an aggregated role takes rules from roles this test cannot see
        assert never_granted(role["rules"]) == [], name


def test_the_operator_can_change_only_its_instrumentations_events_and_its_lease(docs):
    (cluster_role,) = [r for r in roles_bound_to(docs, OPERATOR) if r["kind"] == "ClusterRole"]
    (role,) = [r for r in roles_bound_to(docs, OPERATOR) if r["kind"] == "Role"]
    assert cluster_role["metadata"]["name"] == "impact-ai-operator-manager"
    assert role["metadata"]["name"] == "impact-ai-operator-leader-election"
    assert role["metadata"]["namespace"] == "impact-ai"
    rules = [rule for bound in roles_bound_to(docs, OPERATOR) for rule in bound["rules"]]
    assert writes_outside_the_allowlist(rules) == set()
    assert {(group, resource) for group, resource, verb in granted(rules) if verb in WRITES} == OPERATOR_MAY_WRITE
    # and the Lease is the only thing it may touch in the release namespace
    assert {(g, r) for g, r, _ in granted(role["rules"])} == {("coordination.k8s.io", "leases")}


def test_the_operators_role_is_exactly_what_injecting_an_instrumentation_needs(docs):
    reads = {"get", "list", "watch"}
    expected = {
        *{("", "namespaces", v) for v in reads},  # the namespace of the pod being created
        *{("apps", "replicasets", v) for v in reads},  # the owner of the pod, whose owner is the Deployment
        *{("batch", "jobs", v) for v in reads},  # the owner of the pod, whose owner is the CronJob
        *{("discovery.k8s.io", "endpointslices", v) for v in reads},  # the API server's address, read once at start
        *{("opentelemetry.io", "instrumentations", v) for v in (*reads, "update", "patch")},
        *{("opentelemetry.io", "instrumentations/status", v) for v in ("get", "update", "patch")},
        *{(g, "events", v) for g in ("", "events.k8s.io") for v in ("create", "patch")},
        ("authentication.k8s.io", "tokenreviews", "create"),
        ("authorization.k8s.io", "subjectaccessreviews", "create"),
        *{("coordination.k8s.io", "leases", v) for v in ("create", "get", "list", "update")},
    }
    assert operator_grants(docs) == expected


@pytest.mark.parametrize("version", ["1.27.0", "1.30.0", "1.32.9", "1.33.0", "1.35.0", "1.37.0"])
def test_the_operators_role_is_the_same_on_every_kubernetes_version(docs, version):
    # The upstream role asked for nodes/proxy below 1.33 and nodes/pods from 1.33.
    assert operator_grants(render(extra=("--kube-version", version))) == operator_grants(docs)


def test_the_operator_runs_as_the_account_its_role_is_bound_to(docs):
    pod = find(docs, "Deployment", "impact-ai-operator")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == OPERATOR
    assert find(docs, "ServiceAccount", OPERATOR)["metadata"]["namespace"] == "impact-ai"
    bindings = [
        d
        for d in docs
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding") and d["metadata"]["name"].startswith(OPERATOR)
    ]
    assert len(bindings) == 2
    for binding in bindings:
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": OPERATOR, "namespace": "impact-ai"}]


def test_the_operators_role_follows_the_release_namespace():
    documents = render(release="something-else", namespace="elsewhere")
    assert find(documents, "Role", "impact-ai-operator-leader-election")["metadata"]["namespace"] == "elsewhere"
    for kind in ("RoleBinding", "ClusterRoleBinding"):
        for binding in (d for d in documents if d["kind"] == kind and d["metadata"]["name"].startswith(OPERATOR)):
            assert binding["subjects"] == [{"kind": "ServiceAccount", "name": OPERATOR, "namespace": "elsewhere"}]


def test_the_upstream_charts_roles_are_gone_and_stay_gone():
    templates = {p.name for p in (CHART / "charts" / "opentelemetry-operator" / "templates").iterdir()}
    assert not templates & {"clusterrole.yaml", "clusterrolebinding.yaml", "role.yaml", "rolebinding.yaml"}
    # nothing is bound to the Operator's account but the two objects above (not the -metrics reader role either)
    names = {d["metadata"]["name"] for d in render() if d["kind"] in ("ClusterRole", "Role")}
    assert {n for n in names if n.startswith(OPERATOR)} == {
        "impact-ai-operator-manager",
        "impact-ai-operator-leader-election",
    }


def test_the_role_checks_bite():
    # Upstream's role, in the three ways it differs: it writes pods, it reads Secrets, it holds the kubelet's proxy.
    upstream_like = [
        {"apiGroups": [""], "resources": ["pods", "configmaps", "services"], "verbs": ["create", "delete", "get"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create", "patch", "watch"]},
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["nodes/proxy"], "verbs": ["get"]},
    ]
    assert writes_outside_the_allowlist(upstream_like) >= {("", "pods"), ("", "services"), ("apps", "deployments")}
    assert never_granted(upstream_like) == upstream_like[2:]
    assert never_granted([{"apiGroups": ["*"], "resources": ["pods"], "verbs": ["get"]}])
    assert never_granted([{"apiGroups": [""], "resources": ["pods"], "verbs": ["*"]}])
    assert never_granted([{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}])
    assert never_granted([{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]) == []


def test_the_ingest_key_is_in_one_secret_and_nowhere_else(docs):
    secret = find(docs, "Secret", "impact-ai-ingest-key")
    assert secret["stringData"] == {"ingest-key": "test-ingest-key"}
    outside = json.dumps([d for d in docs if d["kind"] != "Secret" or d["metadata"]["name"] != "impact-ai-ingest-key"])
    assert "test-ingest-key" not in outside
    for _, spec in pod_specs(docs):
        for container in spec["containers"]:
            assert "test-ingest-key" not in json.dumps(container)


def test_an_existing_secret_replaces_the_one_the_chart_would_create():
    documents = render("ingest.key=", "ingest.existingSecret=my-key")
    assert not [d for d in documents if d["kind"] == "Secret" and d["metadata"]["name"] == "impact-ai-ingest-key"]
    reporter = find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]
    assert reporter["volumes"][0]["secret"]["secretName"] == "my-key"
    gateway = find(documents, "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]
    assert gateway["env"][1]["valueFrom"]["secretKeyRef"] == {"name": "my-key", "key": "ingest-key"}


def test_the_key_is_trimmed(tmp_path):
    # --set-file ingest.key=/dev/stdin keeps the newline the shell adds.
    values = tmp_path / "key.yaml"
    values.write_text('ingest:\n  key: "abc\\n"\n')
    documents = render(extra=("-f", str(values)))
    assert find(documents, "Secret", "impact-ai-ingest-key")["stringData"]["ingest-key"] == "abc"


# --- reinstalling with a new key restarts what reads it -------------------------------------------------------------


def key_checksums(documents: list) -> dict[str, str | None]:
    return {
        name: find(documents, "Deployment", name)["spec"]["template"]["metadata"]["annotations"].get("checksum/key")
        for name in ("impact-ai-gateway", "impact-ai-reporter")
    }


def test_a_new_key_restarts_the_gateway_and_the_reporter():
    # The gateway reads the key from its environment at start; the reporter re-reads a mounted file only once the
    # kubelet has refreshed it. A pod template that changes with the key makes `helm upgrade --install` replace both.
    one = key_checksums(render("ingest.key=key-one"))
    two = key_checksums(render("ingest.key=key-two"))
    assert all(one.values()) and all(two.values())
    assert one["impact-ai-gateway"] != two["impact-ai-gateway"]
    assert one["impact-ai-reporter"] != two["impact-ai-reporter"]


def test_the_same_key_restarts_nothing():
    assert key_checksums(render("ingest.key=key-one")) == key_checksums(render("ingest.key=key-one"))


def test_the_checksum_is_not_the_key():
    checksums = key_checksums(render("ingest.key=key-one"))
    assert all(len(value) == 16 and "key-one" not in value for value in checksums.values())


def test_a_key_that_differs_only_in_the_newline_the_shell_adds_is_the_same_key(tmp_path):
    values = tmp_path / "key.yaml"
    values.write_text('ingest:\n  key: "key-one\\n"\n')
    assert key_checksums(render(extra=("-f", str(values)))) == key_checksums(render("ingest.key=key-one"))


def test_the_reporter_rolls_with_the_gateway_not_ahead_of_it():
    # Both annotations come from the key alone: a change of anything else (the reporter's interval) rolls only itself.
    base = render("ingest.key=key-one")
    other = render("ingest.key=key-one", "reporter.interval=2m")
    assert key_checksums(base) == key_checksums(other)


def test_a_secret_the_customer_manages_cannot_be_fingerprinted_without_a_cluster():
    # helm template has no cluster to read the Secret from; an install has, and fingerprints what is in it.
    documents = render("ingest.key=", "ingest.existingSecret=my-key")
    assert key_checksums(documents) == {"impact-ai-gateway": "", "impact-ai-reporter": ""}


# --- the gateway ---------------------------------------------------------------------------------------------------


def test_the_gateway_adds_the_cluster_id_and_forwards_with_the_key(docs):
    config = yaml.safe_load(find(docs, "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"])
    attributes = gateway_attributes(docs)
    assert attributes["k8s.cluster.uid"]["value"] == UID
    assert attributes["k8s.cluster.uid"]["action"] == "upsert"
    assert attributes["k8s.cluster.name"]["value"] == "test-cluster"
    exporter = config["exporters"]["otlp_http"]
    assert exporter["endpoint"] == "${env:IMPACT_INGEST_ENDPOINT}"
    assert exporter["headers"]["authorization"] == "Bearer ${env:IMPACT_INGEST_KEY}"
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["otlp_http"]
    assert set(config["service"]["pipelines"]) == {"traces", "logs"}  # log events: test_chart_gateway_logs.py
    deployment = find(docs, "Deployment", "impact-ai-gateway")["spec"]["template"]
    container = deployment["spec"]["containers"][0]
    assert {"name": "IMPACT_INGEST_ENDPOINT", "value": "https://ingest.example.test"} in container["env"]
    assert "checksum/key" in deployment["metadata"]["annotations"]


# The gateway keeps spans in memory while ingest is slow or down. The Go runtime collects garbage in time only when it
# knows the limit (GOMEMLIMIT); the memory limiter processor's documentation asks for 80% of the container's limit. In
# the measured outage under load (3.78 MB/s for 40 s) a gateway without it was OOM-killed in 1 of 3 runs, one with it
# in none.


def memory_bytes(quantity) -> int:
    """A Kubernetes memory quantity (256Mi, 1Gi, 512M, 123456) in bytes."""
    binary = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    factors = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, **binary}
    number, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(quantity)).groups()
    return int(float(number) * factors[unit])


def go_memory_bytes(text: str) -> int:
    """GOMEMLIMIT's own notation: an integer, with B, KiB, MiB, GiB or TiB."""
    number, unit = re.fullmatch(r"([0-9]+)(B|KiB|MiB|GiB|TiB)?", text).groups()
    return int(number) * {None: 1, "B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit]


def gateway_container(documents: list) -> dict:
    return find(documents, "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]


def gateway_env(documents: list) -> dict:
    return {e["name"]: e.get("value") for e in gateway_container(documents)["env"]}


def test_the_gateway_is_told_to_collect_garbage_before_it_reaches_its_memory_limit(docs):
    container = gateway_container(docs)
    limit = memory_bytes(container["resources"]["limits"]["memory"])
    soft = go_memory_bytes(gateway_env(docs)["GOMEMLIMIT"])
    assert soft <= 0.85 * limit  # room for what the Go runtime does not count: stacks, the runtime's own, mapped files
    assert soft >= 0.75 * limit  # and not so low that the collector runs all the time
    assert gateway_env(docs)["GOMEMLIMIT"] == "204MiB"  # 80% of the default 256Mi limit


def test_the_gateway_requests_what_it_usually_uses_and_can_grow_to_twice_that(docs):
    resources = gateway_container(docs)["resources"]
    assert resources["requests"]["memory"] == "128Mi"
    assert resources["limits"]["memory"] == "256Mi"


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("512Mi", "409MiB"),
        ("1Gi", "819MiB"),
        ("1.5Gi", "1228MiB"),
        ("512M", "390MiB"),  # decimal megabytes
        ("268435456", "204MiB"),  # bytes
        ("3Mi", "2MiB"),
        ("1000", "800"),  # below 1 MiB: bytes
    ],
)
def test_gomemlimit_follows_the_memory_limit_whatever_unit_it_is_written_in(limit, expected):
    documents = render(f"gateway.resources.limits.memory={limit}")
    assert gateway_env(documents)["GOMEMLIMIT"] == expected
    soft = go_memory_bytes(expected)
    assert soft <= 0.85 * memory_bytes(limit)
    if memory_bytes(limit) >= 64 * 2**20:  # whole MiB are coarse below that
        assert soft >= 0.75 * memory_bytes(limit)


def test_gomemlimit_follows_a_limit_that_a_values_file_writes_as_a_number(tmp_path):
    # Helm reads a number in a values file as a float: 268435456 must not become 2.68435456e+08.
    values = tmp_path / "limit.yaml"
    values.write_text("gateway:\n  resources:\n    limits:\n      memory: 268435456\n")
    assert gateway_env(render(extra=("-f", str(values))))["GOMEMLIMIT"] == "204MiB"


def test_without_a_memory_limit_there_is_nothing_to_stay_below():
    # A limit that is not set (or not a plain quantity) sets no variable rather than a wrong one.
    assert "GOMEMLIMIT" not in gateway_env(render("gateway.resources.limits=null"))
    assert "GOMEMLIMIT" not in gateway_env(render("gateway.resources.limits.memory=2e3"))


def test_the_ingest_origin_loses_a_trailing_slash():
    documents = render("ingest.endpoint=https://ingest.example.test/")
    reporter = find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]
    assert "--ingest-endpoint=https://ingest.example.test" in reporter["args"]


def test_the_endpoint_must_be_an_origin():
    result = helm(
        "template",
        "x",
        str(CHART),
        "-f",
        str(CI_VALUES),
        "--set",
        "ingest.endpoint=https://ingest.example.test/v1/traces",
        check=False,
    )
    assert result.returncode != 0


PLAIN_HTTP = "http://mock.impact-mock.svc:8080"
INSTALL_TOKEN = "impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"


def test_an_http_ingest_endpoint_is_refused_unless_the_test_switch_is_set():
    # The ingest key goes to the endpoint as a bearer token, and so does the one-time install token of the exchange:
    # over http either would cross the network in clear.
    shapes = {
        "a pasted key": [],
        "an install token": ["--set", "ingest.key=", "--set-string", "install.token=" + INSTALL_TOKEN],
    }
    for label, extra in shapes.items():
        command = ["template", "x", str(CHART), "-f", str(CI_VALUES), "--set-string", "ingest.endpoint=" + PLAIN_HTTP]
        result = helm(*command, *extra, check=False)
        assert result.returncode != 0, label
        assert "plain http" in result.stderr and "ingest.allowInsecureHttp" in result.stderr, result.stderr


def test_the_test_switch_lets_an_http_endpoint_through_and_reaches_every_pod_that_sends_a_key_or_a_token():
    shapes = {
        "a pasted key": ([], ["impact-ai-reporter", "impact-ai-uninstall"]),
        "an install token": (
            ["ingest.key=", "install.token=" + INSTALL_TOKEN],
            ["impact-ai-reporter", "impact-ai-uninstall", "impact-ai-exchange", "impact-ai-cover"],
        ),
    }
    for label, (sets, workloads) in shapes.items():
        documents = render(
            *sets, "ingest.allowInsecureHttp=true", extra=("--set-string", "ingest.endpoint=" + PLAIN_HTTP)
        )
        for name in workloads:
            kind = "Job" if name in ("impact-ai-uninstall", "impact-ai-exchange") else "Deployment"
            (container,) = find(documents, kind, name)["spec"]["template"]["spec"]["containers"]
            assert "--allow-insecure-ingest" in container["args"], (label, name)
            assert any(a == "--ingest-endpoint=" + PLAIN_HTTP for a in container["args"]), (label, name)


def test_with_https_nothing_tells_a_pod_that_plain_http_is_allowed(docs):
    for _, spec in pod_specs(docs):
        for container in spec["containers"]:
            assert "--allow-insecure-ingest" not in container.get("args", []), container["name"]


def test_the_switch_is_a_value_of_its_own_that_is_off_by_default():
    schema = json.loads((CHART / "values.schema.json").read_text())
    assert schema["properties"]["ingest"]["properties"]["allowInsecureHttp"]["type"] == "boolean"
    assert yaml.safe_load((CHART / "values.yaml").read_text())["ingest"]["allowInsecureHttp"] is False


def test_the_cluster_id_is_required_when_the_cluster_cannot_be_queried():
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--set", "cluster.uid=", check=False)
    assert result.returncode != 0
    assert "cluster.uid is required" in result.stderr


# --- the cloud identity (optional) ---------------------------------------------------------------------------------

ARN = "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks"
CLOUD = (
    "--set-string", "cloud.provider=aws",
    "--set-string", "cloud.accountId=012345678901",  # leading zero: a string, never a number
    "--set-string", "cloud.region=eu-west-1",
    "--set-string", f"cluster.arn={ARN}",
)  # fmt: skip


# The gateway sets resource attributes with the transform processor: `set(resource.attributes["k"], "v")` always,
# and the same with `where resource.attributes["k"] == nil` where a workload's own value is to win. The string is Go's
# quoted form of the value, which is also what the processor's grammar reads.
STATEMENT = re.compile(
    r'set\(resource\.attributes\["([^"]+)"\], ("(?:[^"\\]|\\.)*")\)( where resource\.attributes\["\1"\] == nil)?'
)


STATEMENTS = {"traces": "trace_statements", "logs": "log_statements"}  # the transform processor's key of each signal


def gateway_attributes(documents: list, signal: str = "traces") -> dict:
    """key -> {"key", "value", "action"}: `upsert` for a statement that always sets, `insert` for one that fills in.
    Of the spans (signal "traces") or of the log records (signal "logs") the gateway forwards."""
    config = yaml.safe_load(find(documents, "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"])
    assert "transform/cluster" in config["service"]["pipelines"][signal]["processors"]
    (block,) = config["processors"]["transform/cluster"][STATEMENTS[signal]]
    attributes = {}
    for statement in block["statements"]:
        match = STATEMENT.fullmatch(statement)
        assert match, f"not a statement the gateway is meant to hold: {statement!r}"
        key, literal, guard = match.groups()
        attributes[key] = {"key": key, "value": json.loads(literal), "action": "insert" if guard else "upsert"}
    return attributes


def reporter_args(documents: list) -> list[str]:
    return find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["args"]


def hook_args(documents: list) -> list[str]:
    return find(documents, "Job", "impact-ai-uninstall")["spec"]["template"]["spec"]["containers"][0]["args"]


CLOUD_FLAGS = ("--cloud-provider=", "--cloud-account-id=", "--cloud-region=", "--cluster-arn=")


def test_nothing_of_the_cloud_identity_is_rendered_until_it_is_given(docs):
    assert not [k for k in gateway_attributes(docs) if k.startswith("cloud.")]
    for args in (reporter_args(docs), hook_args(docs)):
        assert not [a for a in args if a.startswith(CLOUD_FLAGS)]
    for key in ("cloud.provider", "cloud.account.id", "cloud.region", "cloud.resource_id"):
        assert not [d for d in docs if key in json.dumps(d)], key


def test_the_gateway_adds_the_cloud_identity_to_the_resource_of_every_span():
    attributes = gateway_attributes(render(extra=CLOUD))
    assert {k: attributes[k]["value"] for k in attributes if k.startswith("cloud.")} == {
        "cloud.provider": "aws",
        "cloud.account.id": "012345678901",
        "cloud.region": "eu-west-1",
        "cloud.resource_id": ARN,
    }
    # Added where the workload has not said it already, so a detector that read the real node wins.
    assert {attributes[k]["action"] for k in attributes if k.startswith("cloud.")} == {"insert"}
    assert attributes["k8s.cluster.uid"]["action"] == "upsert"  # unchanged


def test_the_reporter_and_the_uninstall_notice_send_the_same_cloud_identity():
    documents = render(extra=CLOUD)
    for args in (reporter_args(documents), hook_args(documents)):
        assert "--cloud-provider=aws" in args
        assert "--cloud-account-id=012345678901" in args
        assert "--cloud-region=eu-west-1" in args
        assert f"--cluster-arn={ARN}" in args


def test_each_cloud_value_is_rendered_on_its_own():
    for flag, key, attribute in [
        ("cloud.provider=gcp", "--cloud-provider=gcp", "cloud.provider"),
        ("cloud.accountId=my-project", "--cloud-account-id=my-project", "cloud.account.id"),
        ("cloud.region=europe-west1", "--cloud-region=europe-west1", "cloud.region"),
        ("cluster.arn=//container.googleapis.com/projects/p/locations/l/clusters/c", None, "cloud.resource_id"),
    ]:
        documents = render(extra=("--set-string", flag))
        have = {k for k in gateway_attributes(documents) if k.startswith("cloud.")}
        assert have == {attribute}, (flag, have)
        flags = {a.split("=", 1)[0] for a in reporter_args(documents) if a.startswith(CLOUD_FLAGS)}
        assert len(flags) == 1, (flag, flags)
        if key:
            assert key in reporter_args(documents) and key in hook_args(documents)


def test_the_cloud_identity_is_also_sent_when_another_injector_owns_the_cluster():
    documents = render("operator.install=false", extra=CLOUD)
    assert gateway_attributes(documents)["cloud.account.id"]["value"] == "012345678901"
    assert f"--cluster-arn={ARN}" in reporter_args(documents)
    assert "--injection=disabled" in reporter_args(documents)


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        ("cloud.provider=oracle", "/cloud/provider"),
        ("cloud.region=eu west 1", "/cloud/region"),
        ("cloud.accountId=has space", "/cloud/accountId"),
        ("cluster.arn=arn with space", "/cluster/arn"),
    ],
)
def test_a_cloud_value_that_cannot_be_right_is_refused(flag, message):
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--set-string", flag, check=False)
    assert result.returncode != 0
    assert message in result.stderr


def test_an_account_id_given_as_a_number_is_refused_not_rewritten():
    # `--set cloud.accountId=012345678901` would reach the chart as the number 12345678901: the leading zero is lost.
    number = ("--set", "cloud.accountId=123456789012")
    result = helm("template", "x", str(CHART), "-f", str(CI_VALUES), *number, check=False)
    assert result.returncode != 0
    assert "/cloud/accountId" in result.stderr


# --- report only ---------------------------------------------------------------------------------------------------


def test_report_only_installs_the_reporter_and_the_gateway_only():
    documents = render("operator.install=false")
    assert {(d["kind"], d["metadata"]["name"]) for d in documents if d["kind"] == "Deployment"} == {
        ("Deployment", "impact-ai-gateway"),
        ("Deployment", "impact-ai-reporter"),
    }
    assert not kinds(documents) & {"Instrumentation", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
    assert not [d for d in documents if d["metadata"]["name"].startswith("impact-ai-operator")]
    args = find(documents, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--injection=disabled" in args


def test_injection_is_reported_as_enabled_when_the_operator_is_installed(docs):
    args = find(docs, "Deployment", "impact-ai-reporter")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--injection=enabled" in args


# --- uninstall -------------------------------------------------------------------------------------------------------


def test_the_uninstall_hook_stops_the_reporter_then_sends_the_notice(docs):
    job = find(docs, "Job", "impact-ai-uninstall")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-delete"
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] >= 150  # the reporter's worst case is about 125 s
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "impact-ai-uninstall"
    args = pod["containers"][0]["args"]
    assert "--uninstall-notice" in args
    assert "--stop-deployment=impact-ai-reporter" in args
    assert "--stop-namespace=impact-ai" in args
    selector = next(a for a in args if a.startswith("--stop-pod-selector=")).split("=", 1)[1]
    labels = find(docs, "Deployment", "impact-ai-reporter")["spec"]["template"]["metadata"]["labels"]
    assert dict(pair.split("=") for pair in selector.split(",")).items() <= labels.items()
    affinity = pod["affinity"]["podAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert affinity and "requiredDuringSchedulingIgnoredDuringExecution" not in pod["affinity"]["podAffinity"]


def test_the_hooks_objects_are_removed_when_the_hook_is_done(docs):
    hooks = [d for d in docs if "helm.sh/hook" in (d["metadata"].get("annotations") or {})]
    assert {d["kind"] for d in hooks} == {"ServiceAccount", "Role", "RoleBinding", "ClusterRoleBinding", "Job"}
    for hook in hooks:
        policy = hook["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
        assert "hook-succeeded" in policy and "before-hook-creation" in policy
        assert hook["metadata"]["annotations"]["helm.sh/hook"] == "pre-delete"
    weights = {d["kind"]: int(d["metadata"]["annotations"]["helm.sh/hook-weight"]) for d in hooks}
    assert weights["Job"] > max(w for k, w in weights.items() if k != "Job")  # the Job runs after its permissions exist


def test_the_reporter_pod_does_not_restart_on_a_rolling_update(docs):
    assert find(docs, "Deployment", "impact-ai-reporter")["spec"]["strategy"] == {"type": "Recreate"}


def test_nothing_is_privileged_and_everything_runs_as_non_root(docs):
    for resource, spec in pod_specs(docs):
        name = resource["metadata"]["name"]
        assert spec["securityContext"].get("runAsNonRoot") is True, name
        for container in spec["containers"]:
            context = container["securityContext"]
            assert not context.get("privileged"), name
            assert context["allowPrivilegeEscalation"] is False, name
            assert context["capabilities"]["drop"] == ["ALL"], name
            assert context["readOnlyRootFilesystem"] is True, name
        assert not spec.get("hostNetwork") and not spec.get("hostPID"), name


# --- one cluster, one product -----------------------------------------------------------------------------------------


def test_cluster_scoped_names_do_not_depend_on_the_release_name():
    first = render(release="impact-ai")
    other = render(release="something-else", namespace="elsewhere")
    cluster_scoped = lambda documents: {  # noqa: E731
        (d["kind"], d["metadata"]["name"])
        for d in documents
        if d["kind"]
        in ("ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")
    }
    assert cluster_scoped(first) == cluster_scoped(other)


def test_the_cluster_role_carries_the_product_for_the_second_product_check(docs):
    role = find(docs, "ClusterRole", "impact-ai-kit-reporter")
    assert role["metadata"]["annotations"]["impact.ai/product-id"] == "prod_test"


def test_the_guard_names_the_first_product():
    text = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "is already connected to the impact.ai product" in text
    assert "impact.ai/product-id" in text


# --- values ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (["product.id="], "/product/id"),
        (["ingest.endpoint="], "/ingest/endpoint"),
        (["ingest.key="], "ingest.key is required"),
    ],
)
def test_missing_required_values_are_named(sets, message):
    result = helm(
        "template", "x", str(CHART), "-f", str(CI_VALUES), *[a for s in sets for a in ("--set", s)], check=False
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_kubernetes_1_27_and_later_only():
    old = helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--kube-version", "1.26.9", check=False)
    assert old.returncode != 0
    for version in ("1.27.0", "v1.31.2-eks-abcdef", "v1.30.5-gke.1014001"):
        assert (
            helm("template", "x", str(CHART), "-f", str(CI_VALUES), "--kube-version", version, check=False).returncode
            == 0
        )


def test_the_vendored_operator_chart_is_the_pinned_one():
    chart = yaml.safe_load((CHART / "charts" / "opentelemetry-operator" / "Chart.yaml").read_text())
    dependency = next(
        d
        for d in yaml.safe_load((CHART / "Chart.yaml").read_text())["dependencies"]
        if d["name"] == "opentelemetry-operator"
    )
    assert chart["version"] == dependency["version"] == "0.128.0"
    assert chart["appVersion"] == "0.162.0"
    assert (CHART / "charts" / "opentelemetry-operator" / "LICENSE").is_file()
    for template in (CHART / "charts" / "opentelemetry-operator" / "templates").glob("*.yaml"):
        assert template.read_text().startswith('{{- if include "opentelemetry-operator.impact.enabled" . }}'), (
            template.name
        )


def unknown_fields(schema: dict, value, path: str = "") -> list[str]:
    """Fields of `value` that the CRD's structural schema does not define (the API server would prune them)."""
    if not isinstance(value, dict) or schema.get("x-kubernetes-preserve-unknown-fields"):
        return []
    properties = schema.get("properties")
    if properties is None:
        extra = schema.get("additionalProperties")
        return [m for k, v in value.items() if isinstance(extra, dict) for m in unknown_fields(extra, v, f"{path}.{k}")]
    found = []
    for key, item in value.items():
        if key not in properties:
            found.append(f"{path}.{key}")
        else:
            child = properties[key]
            if isinstance(item, list) and "items" in child:
                for index, element in enumerate(item):
                    found += unknown_fields(child["items"], element, f"{path}.{key}[{index}]")
            else:
                found += unknown_fields(child, item, f"{path}.{key}")
    return found


def test_the_instrumentation_only_uses_fields_its_crd_defines(docs):
    crd = yaml.safe_load((CHART / "crds" / "instrumentations.opentelemetry.io.yaml").read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    instrumentation = find(docs, "Instrumentation")
    assert unknown_fields(schema, {"spec": instrumentation["spec"]}) == []
    assert unknown_fields(schema, {"spec": {"python": {"resources": {}}}}) == [
        ".spec.python.resources"
    ]  # the checker bites


def test_the_injected_init_container_passes_the_restricted_pod_security_level(docs):
    context = find(docs, "Instrumentation")["spec"]["initContainerSecurityContext"]
    assert context["runAsNonRoot"] is True and context["allowPrivilegeEscalation"] is False
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert context["seccompProfile"] == {"type": "RuntimeDefault"}
