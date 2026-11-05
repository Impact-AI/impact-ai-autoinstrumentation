# SPDX-License-Identifier: Apache-2.0

"""The public names of the chart, and what the rename left exactly as it was.

The chart is called `impact-ai` and lives in a directory of that name (`helm lint` wants the two to match); its images
are `autoinstrumentation-python`, `autoinstrumentation-nodejs` and `kubernetes-reporter`, published at the root of the
organisation. The label `app.kubernetes.io/part-of` says `impact-ai`. Nothing else moved: every object keeps the name it
had, and every selector keeps its labels, because the names come from the release name and fixed names, and an upgrade
cannot change a selector. The sets below were taken from the render before the rename.
"""

from __future__ import annotations

import yaml
from test_chart import CHART, find, render
from test_chart_readme import WITH_TOKEN

NODEJS = ("images.nodejs.tag=v0.0.0-test", "images.nodejs.digest=sha256:" + "c" * 64)

KEY_INSTALL_OBJECTS = [
    ("ClusterRole", "impact-ai-kit-reporter"),
    ("ClusterRole", "impact-ai-operator-manager"),
    ("ClusterRoleBinding", "impact-ai-kit-reporter"),
    ("ClusterRoleBinding", "impact-ai-kit-uninstall"),
    ("ClusterRoleBinding", "impact-ai-operator-manager"),
    ("ConfigMap", "impact-ai-gateway"),
    ("Deployment", "impact-ai-gateway"),
    ("Deployment", "impact-ai-operator"),
    ("Deployment", "impact-ai-reporter"),
    ("Instrumentation", "impact"),
    ("Job", "impact-ai-uninstall"),
    ("MutatingWebhookConfiguration", "impact-ai-operator-mutation"),
    ("Role", "impact-ai-operator-leader-election"),
    ("Role", "impact-ai-uninstall"),
    ("RoleBinding", "impact-ai-operator-leader-election"),
    ("RoleBinding", "impact-ai-uninstall"),
    ("Secret", "impact-ai-ingest-key"),
    ("Secret", "impact-ai-operator-webhook-cert"),
    ("Service", "impact-ai-gateway"),
    ("Service", "impact-ai-operator"),
    ("Service", "impact-ai-operator-webhook"),
    ("ServiceAccount", "impact-ai-operator"),
    ("ServiceAccount", "impact-ai-reporter"),
    ("ServiceAccount", "impact-ai-uninstall"),
    ("ValidatingWebhookConfiguration", "impact-ai-operator-validation"),
]

KEY_INSTALL_SELECTORS = {
    "Deployment/impact-ai-gateway": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "gateway"},
    "Deployment/impact-ai-operator": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
    "Deployment/impact-ai-reporter": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "reporter"},
    "Job/impact-ai-uninstall": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "uninstall"},
    "Service/impact-ai-gateway": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "gateway"},
    "Service/impact-ai-operator": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
    "Service/impact-ai-operator-webhook": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
}

TOKEN_INSTALL_OBJECTS = [
    ("ClusterRole", "impact-ai-kit-reporter"),
    ("ClusterRole", "impact-ai-operator-manager"),
    ("ClusterRoleBinding", "impact-ai-kit-reporter"),
    ("ClusterRoleBinding", "impact-ai-kit-uninstall"),
    ("ClusterRoleBinding", "impact-ai-operator-manager"),
    ("ConfigMap", "impact-ai-gateway"),
    ("Deployment", "impact-ai-cover"),
    ("Deployment", "impact-ai-gateway"),
    ("Deployment", "impact-ai-operator"),
    ("Deployment", "impact-ai-reporter"),
    ("Instrumentation", "impact"),
    ("Job", "impact-ai-exchange"),
    ("Job", "impact-ai-uninstall"),
    ("MutatingWebhookConfiguration", "impact-ai-cover"),
    ("MutatingWebhookConfiguration", "impact-ai-operator-mutation"),
    ("Role", "impact-ai-exchange"),
    ("Role", "impact-ai-operator-leader-election"),
    ("Role", "impact-ai-uninstall"),
    ("RoleBinding", "impact-ai-exchange"),
    ("RoleBinding", "impact-ai-operator-leader-election"),
    ("RoleBinding", "impact-ai-uninstall"),
    ("Secret", "impact-ai-cover-cert"),
    ("Secret", "impact-ai-install-key"),
    ("Secret", "impact-ai-install-token"),
    ("Secret", "impact-ai-operator-webhook-cert"),
    ("Service", "impact-ai-cover"),
    ("Service", "impact-ai-gateway"),
    ("Service", "impact-ai-operator"),
    ("Service", "impact-ai-operator-webhook"),
    ("ServiceAccount", "impact-ai-cover"),
    ("ServiceAccount", "impact-ai-exchange"),
    ("ServiceAccount", "impact-ai-operator"),
    ("ServiceAccount", "impact-ai-reporter"),
    ("ServiceAccount", "impact-ai-uninstall"),
    ("ValidatingWebhookConfiguration", "impact-ai-operator-validation"),
]

TOKEN_INSTALL_SELECTORS = {
    "Deployment/impact-ai-cover": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "cover"},
    "Deployment/impact-ai-gateway": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "gateway"},
    "Deployment/impact-ai-operator": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
    "Deployment/impact-ai-reporter": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "reporter"},
    "Job/impact-ai-exchange": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "exchange"},
    "Job/impact-ai-uninstall": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "uninstall"},
    "Service/impact-ai-cover": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "cover"},
    "Service/impact-ai-gateway": {"app.kubernetes.io/instance": "impact-ai", "app.kubernetes.io/name": "gateway"},
    "Service/impact-ai-operator": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
    "Service/impact-ai-operator-webhook": {
        "app.kubernetes.io/component": "controller-manager",
        "app.kubernetes.io/name": "opentelemetry-operator",
    },
}


def objects(documents: list) -> list[tuple[str, str]]:
    return sorted({(d["kind"], d["metadata"]["name"]) for d in documents})


def selectors(documents: list) -> dict:
    out = {}
    for d in documents:
        spec = d.get("spec") or {}
        if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet"):
            out[f"{d['kind']}/{d['metadata']['name']}"] = spec["selector"]["matchLabels"]
        elif d["kind"] == "Service":
            out[f"Service/{d['metadata']['name']}"] = spec.get("selector")
        elif d["kind"] == "Job":
            labels = spec["template"]["metadata"].get("labels", {})
            out[f"Job/{d['metadata']['name']}"] = {
                k: v for k, v in labels.items() if k in ("app.kubernetes.io/name", "app.kubernetes.io/instance")
            }
    return out


def test_the_chart_is_called_impact_ai_and_its_directory_has_the_same_name():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["name"] == "impact-ai" and CHART.name == "impact-ai"
    assert chart["home"] == "https://github.com/impact-ai/impact-ai-k8s"
    assert chart["sources"] == ["https://github.com/impact-ai/impact-ai-k8s"]


def test_the_images_are_published_under_their_public_names_at_the_root_of_the_organisation():
    images = yaml.safe_load((CHART / "values.yaml").read_text())["images"]
    assert images["python"]["repository"] == "ghcr.io/impact-ai/autoinstrumentation-python"
    assert images["nodejs"]["repository"] == "ghcr.io/impact-ai/autoinstrumentation-nodejs"
    assert images["reporter"]["repository"] == "ghcr.io/impact-ai/kubernetes-reporter"


def test_every_object_the_kit_makes_says_it_is_part_of_impact_ai():
    labelled = [d for d in render(*WITH_TOKEN, *NODEJS) if "labels" in d["metadata"]]
    parts = {d["metadata"]["labels"].get("app.kubernetes.io/part-of") for d in labelled}
    # the kit's objects, and the Operator's own, which its chart labels as the Operator's
    assert parts == {"impact-ai", "opentelemetry-operator"}, parts
    ours = [d for d in labelled if d["metadata"]["labels"].get("app.kubernetes.io/part-of") == "impact-ai"]
    assert {d["kind"] for d in ours} >= {"Deployment", "Service", "ClusterRole", "Job", "Secret", "ConfigMap"}
    assert "impact-ai-cluster-kit" not in yaml.safe_dump(render(*WITH_TOKEN, *NODEJS))


def test_the_names_of_a_key_install_are_the_ones_it_had():
    documents = render()
    assert objects(documents) == KEY_INSTALL_OBJECTS
    assert selectors(documents) == KEY_INSTALL_SELECTORS


def test_the_names_of_a_token_install_are_the_ones_it_had():
    documents = render(*WITH_TOKEN, *NODEJS)
    assert objects(documents) == TOKEN_INSTALL_OBJECTS
    assert selectors(documents) == TOKEN_INSTALL_SELECTORS


def test_the_selector_of_a_deployment_does_not_hold_the_label_that_changed():
    for name in ("impact-ai-gateway", "impact-ai-reporter", "impact-ai-operator"):
        match = find(render(), "Deployment", name)["spec"]["selector"]["matchLabels"]
        assert "app.kubernetes.io/part-of" not in match, (name, match)


def test_the_hook_selector_of_the_uninstall_script_finds_the_cluster_scoped_objects_by_the_new_label():
    documents = render(*WITH_TOKEN, *NODEJS)
    scoped = [
        d for d in documents if d["kind"] in ("ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration")
    ]
    assert scoped
    for d in scoped:
        labels = d["metadata"]["labels"]
        assert (
            labels["app.kubernetes.io/part-of"] == "impact-ai" and labels["app.kubernetes.io/instance"] == "impact-ai"
        )
