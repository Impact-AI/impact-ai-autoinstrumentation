# SPDX-License-Identifier: Apache-2.0

"""No Role or ClusterRole of the kit lets anyone write a pod, in any mode, hooks included.

A permission to patch pods is a permission to run code in them: a pod patch can replace a running container's image.
The uninstall hook used to hold one, on pods in every namespace, for as long as it ran, to take the cover pod's marks
off live pods. The marks are cosmetic, so the hook does not do that any more: the uninstall script takes them off,
under the operator's own rights. What the kit's roles read, and what the hooks write (the scale of two Deployments of
the kit, the one Secret of the key), is pinned in the tests of those roles.
"""

from __future__ import annotations

import pytest
from test_chart_exchange import render, token_install

READ = {"get", "list", "watch"}

MODES = {
    "a pasted key": lambda: render(),
    "an install token": lambda: token_install(),
    "an install token with Node.js": lambda: token_install(
        "images.nodejs.tag=v0", "images.nodejs.digest=sha256:" + "c" * 64
    ),
    "an install token behind a mirror": lambda: token_install("global.imageRegistry=registry.example.com/mirror"),
    "automatic coverage with a pasted key": lambda: render("cover.enabled=true"),
    "another injector owns the cluster": lambda: token_install("operator.install=false"),
    "another release in another namespace": lambda: token_install(release="kit-a", namespace="kit"),
}


def writes_pods(rule: dict) -> bool:
    """Whether a rule grants a verb beyond reading on pods or on anything that reaches them (pods/*, a wildcard)."""
    groups, resources = rule.get("apiGroups") or [], rule.get("resources") or []
    reaches = ("" in groups or "*" in groups) and any(r in ("pods", "*") or r.startswith("pods/") for r in resources)
    return reaches and bool(set(rule.get("verbs") or []) - READ)


def roles(documents: list) -> list[dict]:
    return [d for d in documents if d["kind"] in ("Role", "ClusterRole")]


@pytest.mark.parametrize("rule", [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["patch"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "delete", "update", "deletecollection"]},
    {"apiGroups": [""], "resources": ["pods/status"], "verbs": ["update"]},
    {"apiGroups": [""], "resources": ["pods/ephemeralcontainers"], "verbs": ["patch"]},
    {"apiGroups": [""], "resources": ["pods/eviction"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
    {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]},
    {"apiGroups": [""], "resources": ["*"], "verbs": ["patch"]},
    {"apiGroups": [""], "resources": ["configmaps", "pods"], "verbs": ["get", "patch"]},
])  # fmt: skip
def test_the_check_sees_every_way_to_write_a_pod(rule):
    assert writes_pods(rule)


@pytest.mark.parametrize("rule", [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
    {"apiGroups": [""], "resources": ["pods", "nodes"], "verbs": ["list"]},
    {"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["patch"]},
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "delete", "patch"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {"apiGroups": ["opentelemetry.io"], "resources": ["instrumentations"], "verbs": ["create", "patch", "update"]},
])  # fmt: skip
def test_the_check_lets_reading_and_other_resources_through(rule):
    assert not writes_pods(rule)


@pytest.mark.parametrize("mode", MODES)
def test_no_role_the_kit_renders_has_a_write_verb_on_pods(mode):
    # The Operator's role is the kit's own too (templates/operator-rbac.yaml): the upstream chart's is not rendered.
    documents = MODES[mode]()
    assert roles(documents), "the kit renders roles"
    offenders = {r["metadata"]["name"]: [x for x in r["rules"] if writes_pods(x)] for r in roles(documents)}
    assert {name: rules for name, rules in offenders.items() if rules} == {}


def test_the_roles_looked_at_include_those_of_the_hooks():
    documents = token_install()
    names = {r["metadata"]["name"] for r in roles(documents)}
    assert {"impact-ai-exchange", "impact-ai-uninstall", "impact-ai-kit-reporter"} <= names
    hooks = [r for r in roles(documents) if "helm.sh/hook" in (r["metadata"].get("annotations") or {})]
    assert {h["metadata"]["name"] for h in hooks} == {"impact-ai-exchange", "impact-ai-uninstall"}
