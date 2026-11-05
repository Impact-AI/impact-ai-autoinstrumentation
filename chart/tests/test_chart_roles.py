# SPDX-License-Identifier: Apache-2.0

"""Every right the kit gives anything, in every shape of install, pinned.

The kit runs in a customer's cluster and is read by their security team first. Everything its pods, its hooks and the
Operator may do is listed here, role by role, for each kind of install: a pasted key, a Secret of the customer's, an
install token with automatic coverage and the Node.js image, and the kit beside another injector. A new right has to
change this file, so it is argued for in review. The invariants below hold for every role of every shape:

* no wildcard, no `nodes/proxy`, no `pods/exec`, no rights to hand out rights;
* a Secret is named in exactly two rules, and each names the one Secret the key lives in: the install hook may `get`,
  `patch` and `delete` it (it deletes it when a first install is refused), the uninstall hook may `delete` it. No rule
  creates, lists, watches or updates a Secret, and no rule names any other;
* the only writes on cluster-wide resources are the Operator's, on `Instrumentation` resources and on events: no role of
  the kit, a hook's included, writes a pod or anything that reaches one;
* a role is bound only to service accounts of the kit.
"""

from __future__ import annotations

import pytest
from test_chart import NEVER_RESOURCES, NEVER_VERBS, OPERATOR, WRITES, find, granted, render, roles_bound_to

TOKEN = "impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"
NODEJS = ("images.nodejs.tag=v0.0.0-test", "images.nodejs.digest=sha256:" + "c" * 64)
KEY_SECRET = "impact-ai-install-key"  # noqa: S105 - the name of a Secret, not a password

SHAPES = {
    "a pasted key": [],
    "a Secret of the customer's": ["ingest.key=", "ingest.existingSecret=their-key"],
    "an install token with automatic coverage and Node.js": ["ingest.key=", f"install.token={TOKEN}", *NODEJS],
    "an install token without automatic coverage": ["ingest.key=", f"install.token={TOKEN}", "cover.enabled=false"],
    "beside another injector": ["operator.install=false"],
    "an install token beside another injector": ["operator.install=false", "ingest.key=", f"install.token={TOKEN}"],
}


@pytest.fixture(scope="module", params=list(SHAPES))
def shape(request):
    return request.param, render(*SHAPES[request.param])


def roles(documents: list) -> list[dict]:
    return [d for d in documents if d["kind"] in ("Role", "ClusterRole")]


def grants(role: dict) -> set[tuple]:
    """(API group, resource, verb, the names the rule is limited to) for every grant of the role."""
    return {
        (group, resource, verb, tuple(sorted(rule.get("resourceNames", []))))
        for rule in role["rules"]
        for group in rule.get("apiGroups", [])
        for resource in rule.get("resources", [])
        for verb in rule["verbs"]
    }


def rows(group: str, resources: list[str], verbs: list[str], names=()) -> set[tuple]:
    return {(group, resource, verb, tuple(sorted(names))) for resource in resources for verb in verbs}


READ_ONLY = ["get", "list", "watch"]
REPORTER = (
    rows("", ["namespaces"], ["get", "list"])
    | rows("", ["nodes", "pods"], ["list"])
    | rows("apps", ["deployments", "statefulsets", "daemonsets"], ["list"])
    | rows("batch", ["cronjobs", "jobs"], ["list"])
    | rows("admissionregistration.k8s.io", ["mutatingwebhookconfigurations"], ["list"])
)
OPERATOR_CLUSTER = (
    rows("", ["namespaces"], READ_ONLY)
    | rows("apps", ["replicasets"], READ_ONLY)
    | rows("batch", ["jobs"], READ_ONLY)
    | rows("discovery.k8s.io", ["endpointslices"], READ_ONLY)
    | rows("opentelemetry.io", ["instrumentations"], [*READ_ONLY, "update", "patch"])
    | rows("opentelemetry.io", ["instrumentations/status"], ["get", "update", "patch"])
    | rows("", ["events"], ["create", "patch"])
    | rows("events.k8s.io", ["events"], ["create", "patch"])
    | rows("authentication.k8s.io", ["tokenreviews"], ["create"])
    | rows("authorization.k8s.io", ["subjectaccessreviews"], ["create"])
)
OPERATOR_NAMESPACE = rows("coordination.k8s.io", ["leases"], ["create", "get", "list", "update"])


def expected_roles(name: str, documents: list) -> dict[tuple[str, str], set[tuple]]:
    """The roles the shape of the install renders, and what each may do."""
    token = "token" in name
    injector = "beside another injector" not in name
    covered = token and injector and "without automatic" not in name  # the cover pod needs the Operator's webhook
    scaled = ["impact-ai-reporter", *(["impact-ai-cover"] if covered else [])]
    uninstall = rows("apps", ["deployments/scale"], ["patch"], scaled)
    if token:
        uninstall |= rows("", ["secrets"], ["delete"], [KEY_SECRET])
    out = {
        ("ClusterRole", "impact-ai-kit-reporter"): REPORTER,
        ("Role", "impact-ai-uninstall"): uninstall,
    }
    if token:
        out[("Role", "impact-ai-exchange")] = rows("", ["secrets"], ["get", "patch", "delete"], [KEY_SECRET])
    if injector:
        out[("ClusterRole", "impact-ai-operator-manager")] = OPERATOR_CLUSTER
        out[("Role", "impact-ai-operator-leader-election")] = OPERATOR_NAMESPACE
    return out


def test_every_role_of_the_install_is_known_and_does_exactly_what_is_pinned(shape):
    name, documents = shape
    found = {(d["kind"], d["metadata"]["name"]): grants(d) for d in roles(documents)}
    expected = expected_roles(name, documents)
    assert set(found) == set(expected), f"{name}: roles {sorted(set(found) ^ set(expected))}"
    for role, wanted in expected.items():
        assert found[role] == wanted, (
            f"{name}: {role[1]} differs: +{sorted(found[role] - wanted)} -{sorted(wanted - found[role])}"
        )


def test_no_role_holds_a_wildcard_the_kubelet_proxy_a_shell_in_a_pod_or_the_right_to_hand_out_rights(shape):
    _, documents = shape
    for role in roles(documents):
        assert "aggregationRule" not in role, role["metadata"]["name"]
        for rule in role["rules"]:
            assert not set(rule.get("resources", [])) & NEVER_RESOURCES - {"secrets"}, (role["metadata"]["name"], rule)
            assert not set(rule["verbs"]) & NEVER_VERBS, (role["metadata"]["name"], rule)
            assert "*" not in rule.get("apiGroups", []), (role["metadata"]["name"], rule)
            assert not rule.get("nonResourceURLs"), (role["metadata"]["name"], rule)


def test_a_secret_is_named_in_exactly_two_rules_each_for_the_one_secret_the_key_lives_in(shape):
    # The allowlist of Secrets: the install hook gets, patches and (after a refused first install) deletes the key
    # Secret, the uninstall hook deletes it. A rule that creates, lists, watches or updates a Secret, or names another,
    # is not on it.
    name, documents = shape
    found = set()
    for role in roles(documents):
        for rule in role["rules"]:
            if "secrets" in rule.get("resources", []):
                found.add((role["metadata"]["name"], tuple(rule["verbs"]), tuple(rule.get("resourceNames", []))))
    if "token" in name:
        assert found == {
            ("impact-ai-exchange", ("get", "patch", "delete"), (KEY_SECRET,)),
            ("impact-ai-uninstall", ("delete",), (KEY_SECRET,)),
        }
    else:
        assert found == set(), "a Secret in a role of an install that has no hook to keep a key in one"


def test_the_only_writes_on_something_cluster_wide_are_the_operators_on_instrumentations_and_events(shape):
    _, documents = shape
    writes = {}
    for role in (d for d in roles(documents) if d["kind"] == "ClusterRole"):
        for group, resource, verb in granted(role["rules"]):
            if verb in WRITES:
                writes.setdefault(role["metadata"]["name"], set()).add((group, resource))
    assert set(writes) <= {"impact-ai-operator-manager"}, writes
    # what the Operator may change is pinned in test_chart.py: Instrumentation resources, their status and events. None
    # of it is a pod, a workload, a Service or a Secret.
    assert writes.get("impact-ai-operator-manager", set()) <= {
        ("opentelemetry.io", "instrumentations"),
        ("opentelemetry.io", "instrumentations/status"),
        ("", "events"),
        ("events.k8s.io", "events"),
        ("authentication.k8s.io", "tokenreviews"),
        ("authorization.k8s.io", "subjectaccessreviews"),
    }


def test_a_role_is_bound_only_to_service_accounts_of_the_kit_that_the_install_renders(shape):
    _, documents = shape
    accounts = {d["metadata"]["name"] for d in documents if d["kind"] == "ServiceAccount"}
    for binding in (d for d in documents if d["kind"] in ("RoleBinding", "ClusterRoleBinding")):
        find(
            documents, binding["roleRef"]["kind"], binding["roleRef"]["name"]
        )  # no binding to a role that is not there
        for subject in binding["subjects"]:
            assert subject["kind"] == "ServiceAccount", binding["metadata"]["name"]
            assert subject["name"] in accounts, (binding["metadata"]["name"], subject)
            assert subject["namespace"] == "impact-ai"


def test_the_cover_pod_and_the_gateway_are_bound_to_no_role_at_all(shape):
    _, documents = shape
    for account in ("impact-ai-cover", "impact-ai-gateway"):
        assert roles_bound_to(documents, account) == [], account


def test_the_operator_is_bound_to_its_two_roles_when_it_is_installed(shape):
    name, documents = shape
    bound = sorted(r["metadata"]["name"] for r in roles_bound_to(documents, OPERATOR))
    if "beside another injector" in name:
        assert bound == []
    else:
        assert bound == ["impact-ai-operator-leader-election", "impact-ai-operator-manager"]
