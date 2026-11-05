# SPDX-License-Identifier: Apache-2.0

"""The end-to-end suite runs on one kind cluster, in order: install, cover, fail open, one product, uninstall.

scripts/e2e.sh builds and loads the images and creates the cluster; E2E_CONTEXT names its kubectl context.
"""

from __future__ import annotations

import os

import pytest

from kube import KEY, MANIFESTS, Mock, get, kubectl, rollout, run


class World:
    def __init__(self) -> None:
        self.mock = Mock("mock")  # impact.ai: ingest (behind the key) and the model providers
        self.theirs = Mock("theirs")  # the customer's own backend
        self.cluster_uid = ""

    def start(self) -> None:
        kubectl("apply", "-f", str(MANIFESTS / "mock.yaml"))
        rollout("deployment/mock", "impact-mock")
        rollout("deployment/theirs", "impact-mock")
        self.mock.control(require_key=KEY)
        self.cluster_uid = get("namespace", "kube-system")["metadata"]["uid"]

    def deploy_workloads(self) -> None:
        """The customer's workloads, applied after the kit is installed: they appear in the next snapshot."""
        kubectl("apply", "-f", str(MANIFESTS / "workloads.yaml"))


@pytest.fixture(scope="session")
def world() -> World:
    w = World()
    w.start()
    return w


@pytest.fixture
def helm4(monkeypatch, capsys):
    """Helm 4 first on the PATH for one test, its install and its uninstall.

    mise.toml lists Helm 4 beside Helm 3, and scripts/e2e.sh puts the directory of Helm 4 in E2E_HELM4_DIR. The one live
    install the suite makes with it replaces an install that Helm 3 would have made: it costs no time. The oldest
    Kubernetes version (E2E_NODE_IMAGE) is older than Helm 4 supports: there the same install is made with Helm 3.
    """
    if os.environ.get("E2E_NODE_IMAGE"):
        return "Helm 3: the cluster is older than Helm 4 supports"
    directory = os.environ.get("E2E_HELM4_DIR")
    if not directory:
        if os.environ.get("CI"):
            pytest.fail("no Helm 4 on this runner: E2E_HELM4_DIR is empty (scripts/e2e.sh finds it with mise)")
        pytest.skip("no Helm 4: E2E_HELM4_DIR names its directory (scripts/e2e.sh finds it with mise)")
    monkeypatch.setenv("PATH", directory + os.pathsep + os.environ["PATH"])
    version = run(["helm", "version", "--short"]).stdout.strip()
    assert version.startswith("v4."), f"the helm on the PATH is {version}, not Helm 4"
    with capsys.disabled():  # in the CI log, where the run is read
        print(f"\n   Helm {version} for this install and its uninstall")
    return version
