# SPDX-License-Identifier: Apache-2.0

"""The Operator runs on the kit's own narrow role (chart/impact-ai/templates/operator-rbac.yaml).

Everything it did in the tests before this one, it did with that role: it injected three workloads, with the names of
their Deployments as attributes. Here the API server answers what the role does and does not allow, and the Operator's
log is searched for "forbidden". A "forbidden" would mean the role lacks a read the Operator needs, and the chart would
have to add that read and nothing more. The suite runs on the Kubernetes version of the cluster: 1.34 by default, and
1.27 when E2E_NODE_IMAGE says so.
"""

from __future__ import annotations

import pytest

from kube import kubectl

ACCOUNT = "system:serviceaccount:impact-ai:impact-ai-operator"


def can(verb: str, resource: str, namespace: str = "shop") -> bool:
    """Whether the API server lets the Operator's account do it; `resource` may name a subresource: nodes/proxy."""
    resource, _, subresource = resource.partition("/")
    command = ["auth", "can-i", verb, resource, "--as", ACCOUNT, "-n", namespace]
    result = kubectl(*command, *([f"--subresource={subresource}"] if subresource else []), check=False)
    return result.stdout.strip() == "yes"


def test_the_operator_log_holds_no_forbidden(capsys):
    log = kubectl("-n", "impact-ai", "logs", "deploy/impact-ai-operator", "--tail=-1").stdout
    lines = log.splitlines()
    assert any("Starting the OpenTelemetry Operator" in line for line in lines), "this is not the Operator's log"
    forbidden = [line for line in lines if "forbidden" in line.lower()]
    assert forbidden == [], "the role lacks a read the Operator needs:\n" + "\n".join(forbidden[:5])
    version = kubectl("get", "nodes", "-o", "jsonpath={.items[0].status.nodeInfo.kubeletVersion}").stdout
    with capsys.disabled():  # in the CI log, where the run is read
        print(f"\n   Operator log: {len(lines)} lines, none with 'forbidden' (Kubernetes {version})")


@pytest.mark.parametrize(
    ("verbs", "resource", "namespace"),
    [
        (("get", "list", "watch"), "namespaces", ""),
        (("get", "list", "watch"), "replicasets.apps", "shop"),
        (("get", "list", "watch"), "jobs.batch", "edge"),
        (("get", "list", "watch", "update", "patch"), "instrumentations.opentelemetry.io", "impact-ai"),
        (("get", "update", "patch"), "instrumentations.opentelemetry.io/status", "impact-ai"),
        (("create", "patch"), "events", "shop"),
        (("create", "patch"), "events.events.k8s.io", "shop"),
        (("get", "list", "watch"), "endpointslices.discovery.k8s.io", "default"),
        (("create",), "tokenreviews.authentication.k8s.io", ""),
        (("create",), "subjectaccessreviews.authorization.k8s.io", ""),
        (("create", "get", "update"), "leases.coordination.k8s.io", "impact-ai"),
    ],
)
def test_the_operator_may_do_what_injecting_needs(verbs, resource, namespace):
    assert [v for v in verbs if not can(v, resource, namespace)] == []


@pytest.mark.parametrize(
    ("verbs", "resource", "namespace"),
    [
        # Secrets: the ingest key is one, and so is every Helm release record
        (("get", "list", "watch", "create", "update", "delete"), "secrets", "impact-ai"),
        (("get", "list", "watch"), "secrets", "shop"),
        (("get", "list", "watch"), "configmaps", "shop"),
        # the customer's pods and workloads: no write at all
        (("create", "update", "patch", "delete"), "pods", "shop"),
        (("create",), "pods", "kube-system"),
        (("create", "update", "patch", "delete"), "deployments.apps", "shop"),
        (("create", "update", "patch", "delete"), "statefulsets.apps", "shop"),
        (("create", "update", "patch", "delete"), "daemonsets.apps", "edge"),
        (("create", "update", "patch", "delete"), "cronjobs.batch", "edge"),
        (("create", "update", "patch", "delete"), "services", "shop"),
        (("create", "update", "patch", "delete"), "networkpolicies.networking.k8s.io", "shop"),
        (("create", "update", "delete"), "serviceaccounts", "shop"),
        # the node, and a shell in a pod
        (("get", "create"), "nodes/proxy", ""),
        (("create", "get"), "pods/exec", "shop"),
        (("get", "list"), "nodes", ""),
        # rights to hand out, and the Operator's own Lease elsewhere
        (("create", "update", "bind", "escalate"), "clusterroles.rbac.authorization.k8s.io", ""),
        (("create", "update"), "rolebindings.rbac.authorization.k8s.io", "shop"),
        (("create", "update"), "leases.coordination.k8s.io", "shop"),
    ],
)
def test_the_operator_may_not_do_anything_else(verbs, resource, namespace):
    assert [v for v in verbs if can(v, resource, namespace)] == []


def test_the_operator_may_not_do_everything():
    result = kubectl("auth", "can-i", "*", "*", "--as", ACCOUNT, check=False)
    assert result.stdout.strip() == "no"
