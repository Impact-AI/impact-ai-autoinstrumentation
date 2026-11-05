# SPDX-License-Identifier: Apache-2.0

"""One install per cluster, for one product. A second product is refused and the first is named."""

from __future__ import annotations

import pytest

from kube import PRODUCT, helm, install, kubectl


@pytest.fixture(autouse=True)
def clean_second_namespace():
    yield
    kubectl("delete", "namespace", "impact-other", "--ignore-not-found", "--wait=false", check=False)


def test_a_second_product_in_another_namespace_is_refused_and_names_the_first():
    result = install(release="impact-other", namespace="impact-other", product="prod_other", check=False)
    assert result.returncode != 0
    assert "already connected to the impact.ai product" in result.stderr
    assert PRODUCT in result.stderr
    assert "helm uninstall impact-ai -n impact-ai" in result.stderr


def test_a_second_product_in_the_same_release_is_refused_and_names_the_first():
    result = install(product="prod_other", check=False)
    assert result.returncode != 0
    assert PRODUCT in result.stderr and "already connected" in result.stderr


def test_the_same_product_from_another_release_is_refused_with_the_release_to_upgrade():
    result = install(release="impact-other", namespace="impact-other", check=False)
    assert result.returncode != 0
    assert "already has the impact.ai cluster kit" in result.stderr
    assert "helm upgrade impact-ai -n impact-ai" in result.stderr


def test_nothing_of_the_refused_installs_was_left_behind():
    names = {r["name"] for r in __import__("json").loads(helm("list", "--all-namespaces", "-o", "json").stdout)}
    assert names == {"impact-ai"}


def test_installing_again_for_the_same_product_replaces_it():
    result = install(extra=("--set", "gateway.replicas=1"))
    assert result.returncode == 0, result.stderr
    status = __import__("json").loads(helm("status", "impact-ai", "-n", "impact-ai", "-o", "json").stdout)
    assert status["version"] >= 2 and status["info"]["status"] == "deployed"
