# SPDX-License-Identifier: Apache-2.0

"""Report only: when another injector already owns the cluster, only the reporter and the gateway are installed."""

from __future__ import annotations

import base64
import json

import pytest

from kube import CONTEXT, UNINSTALL_SCRIPT, get, install, install_with_token, kubectl, run, wait_until

OWNERS = {
    "aws-cloudwatch": (
        "amazon-cloudwatch-observability-mutating-webhook-configuration",
        "mpod.kb.io",
        "amazon-cloudwatch",
        "/mutate-v1-pod",
    ),
    "datadog": ("datadog-webhook", "datadog.webhook.auto.instrumentation", "datadog", "/injectlib"),
    "dynatrace": ("dynatrace-webhook", "webhook.pod.dynatrace.com", "dynatrace", "/inject"),
    "opentelemetry-operator": (
        "opentelemetry-operator-mutating-webhook-configuration",
        "mpod.kb.io",
        "opentelemetry-operator",
        "/mutate-v1-pod",
    ),
}


def fake_webhook(owner: str) -> dict:
    """What the owner's injector registers; a selector that matches nothing, so the fake never acts."""
    name, webhook, namespace, path = OWNERS[owner]
    service = "opentelemetry-operator-webhook-service" if owner == "opentelemetry-operator" else "webhook-service"
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": name},
        "webhooks": [
            {
                "name": webhook,
                "admissionReviewVersions": ["v1"],
                "sideEffects": "None",
                "failurePolicy": "Ignore",
                "namespaceSelector": {"matchLabels": {"impact.ai/e2e-never": "true"}},
                "clientConfig": {"service": {"name": service, "namespace": namespace, "path": path, "port": 443}},
                "rules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE"],
                        "resources": ["pods"],
                        "scope": "Namespaced",
                    }
                ],
            }
        ],
    }


def apply(owner: str) -> str:
    kubectl("apply", "-f", "-", input=json.dumps(fake_webhook(owner)))
    return OWNERS[owner][0]


@pytest.mark.parametrize("owner", OWNERS)
def test_the_chart_leaves_the_operator_out_beside_each_known_injector(owner):
    name = apply(owner)
    try:
        result = install(extra=("--dry-run=server",), check=False)
        assert result.returncode == 0, result.stderr
        # What Helm would create is under MANIFEST (and the hooks under HOOKS); the rest is the release's header.
        _, _, rest = result.stdout.partition("MANIFEST:")
        manifest, _, notes = rest.partition("NOTES:")
        assert manifest.strip() and notes.strip(), result.stdout
        assert "kind: Instrumentation" not in manifest
        assert "impact-ai-operator" not in manifest
        assert "--injection=disabled" in manifest
        assert f"({owner} already owns this cluster's injection)" in notes
        assert "kind: Deployment" in manifest
        assert "name: impact-ai-reporter" in manifest and "name: impact-ai-gateway" in manifest
    finally:
        kubectl("delete", "mutatingwebhookconfiguration", name, "--ignore-not-found")


def test_installed_beside_datadog_the_page_can_name_the_injector(world, helm4):
    # The one live install of the suite that Helm 4 makes (and removes), made with an install token, as the command
    # Cover your AI shows is: the chart, its schema, the lookups of the guard and of the other injector, the CRD, the
    # exchange hook and the Secret it fills, the Deployments, the pre-delete hook and its roles. Flux embeds Helm 4 and
    # runs the same command at every reconcile, so the same command again, and a fresh token that impact.ai refuses,
    # are made too: neither changes the key the cluster holds.
    name = apply("datadog")
    try:
        mark = world.mock.last_seq()
        token = world.mock.token()
        result = install_with_token(token)
        assert result.returncode == 0, result.stderr
        deployments = {d["metadata"]["name"] for d in get("-n", "impact-ai", "deployments")["items"]}
        assert deployments == {"impact-ai-gateway", "impact-ai-reporter"}
        assert not [
            m for m in get("mutatingwebhookconfiguration")["items"] if m["metadata"]["name"].startswith("impact-ai")
        ]

        def key_secret() -> dict:
            return get("-n", "impact-ai", "secret", "impact-ai-install-key")

        def key_of(secret: dict) -> str:
            return base64.b64decode(secret["data"]["ingest-key"]).decode()

        first = key_secret()
        assert key_of(first), "the exchange stored the key the cluster made"
        assert len(world.mock.exchanges(mark)) == 1

        def reported():
            for snapshot in reversed(world.mock.inventories()):
                if snapshot["reporter"]["clusterOwner"] == "datadog":
                    return snapshot
            return None

        snapshot = wait_until(reported, timeout=120, message="a snapshot naming the injector")
        assert snapshot["reporter"]["mode"] == "report-only"

        # The same command again: the hook sees the token it already exchanged, calls nobody and touches nothing.
        again = install_with_token(token)
        assert again.returncode == 0, again.stderr
        second = key_secret()
        assert (second["metadata"]["uid"], second["metadata"]["resourceVersion"]) == (
            first["metadata"]["uid"],
            first["metadata"]["resourceVersion"],
        )
        assert len(world.mock.exchanges(mark)) == 1, "nothing was sent to impact.ai"

        # A fresh token that impact.ai refuses: the command fails, and the working key stays as it was.
        refused = install_with_token(world.mock.token(state="used"), check=False)
        assert refused.returncode != 0
        third = key_secret()
        assert third["metadata"]["uid"] == first["metadata"]["uid"] and key_of(third) == key_of(first)
        assert "pending-key" not in third["data"], "the key stored ahead for the refused token is taken back"
        assert len(world.mock.exchanges(mark)) == 2
    finally:
        if CONTEXT:
            kubectl("config", "use-context", CONTEXT)
        run(["sh", str(UNINSTALL_SCRIPT)], timeout=900)
        kubectl("delete", "mutatingwebhookconfiguration", name, "--ignore-not-found")
    assert kubectl("get", "namespace", "impact-ai", check=False).returncode != 0
