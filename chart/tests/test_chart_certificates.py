# SPDX-License-Identifier: Apache-2.0

"""The certificates of the two webhooks the kit serves: the Operator's and the cover pod's.

An install or upgrade reuses the certificate the chart made earlier (`cluster-kit.webhookCertificate` looks the Secret
up), so that the CA bundle of a webhook configuration and the certificate its pod serves never differ. A new certificate
on every upgrade left the pod serving the old one for the minute the kubelet needs to hand a new Secret to a running
pod; for that minute the API server could not verify the webhook, it failed open, and pods started uninjected.

`helm template` has no cluster to look in, so the reuse itself is proved on a cluster (e2e/test_8_auto_cover.py). What
is checked here is what a render shows: which service each certificate is for, that it verifies against the CA every
webhook of that service carries, and that nothing but the one helper makes certificates.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess

import pytest
from test_chart import CHART
from test_chart_exchange import render

OPENSSL = shutil.which("openssl") or "openssl"
TEMPLATES = CHART / "templates"
CONFIGURATIONS = ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")


def certificate_secrets(documents: list) -> list:
    return [d for d in documents if d["kind"] == "Secret" and d.get("type") == "kubernetes.io/tls"]


def decoded(secret: dict, key: str) -> str:
    return base64.b64decode(secret["data"][key]).decode()


@pytest.mark.parametrize(("release", "namespace"), [("impact-ai", "impact-ai"), ("kit", "platform")])
def test_each_certificate_is_for_a_service_and_verifies_against_the_ca_its_webhooks_carry(release, namespace, tmp_path):
    documents = render("cover.enabled=true", release=release, namespace=namespace)
    services = {d["metadata"]["name"] for d in documents if d["kind"] == "Service"}
    secrets = certificate_secrets(documents)
    assert len(secrets) == 2, "one for the Operator's webhooks and one for the cover pod's"
    served = set()
    for secret in secrets:
        # The Secret says for which service the certificate was made: an upgrade reuses it for that service only.
        made_for = secret["metadata"]["annotations"]["impact.ai/service"]
        service = made_for.removesuffix(f".{namespace}")
        assert made_for == f"{service}.{namespace}" and service in services, made_for
        crt, ca = decoded(secret, "tls.crt"), decoded(secret, "ca.crt")
        text = subprocess.run(  # noqa: S603
            [OPENSSL, "x509", "-noout", "-text"], input=crt, capture_output=True, text=True, check=True
        ).stdout  # noqa: S607
        assert f"DNS:{service}.{namespace}.svc" in text, (service, text)
        (tmp_path / f"{service}-ca.pem").write_text(ca)
        (tmp_path / f"{service}.pem").write_text(crt)
        verified = subprocess.run(  # noqa: S603
            [OPENSSL, "verify", "-CAfile", str(tmp_path / f"{service}-ca.pem"), str(tmp_path / f"{service}.pem")],  # noqa: S607
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert verified.returncode == 0, verified.stdout + verified.stderr
        # Every webhook that calls this service carries this CA, and there is at least one.
        carried = [
            webhook["clientConfig"]["caBundle"]
            for d in documents
            if d["kind"] in CONFIGURATIONS
            for webhook in d["webhooks"]
            if webhook["clientConfig"]["service"]["name"] == service
        ]
        assert carried and set(carried) == {secret["data"]["ca.crt"]}, service
        served.add(service)
    assert len(served) == 2


def helper_body() -> str:
    text = (TEMPLATES / "_helpers.tpl").read_text()
    match = re.search(
        r'define "cluster-kit\.webhookCertificate" -\}\}(.*?\{\{- toJson \$data -\}\}\n)\{\{- end -\}\}', text, re.S
    )
    assert match, "the helper that makes the webhook certificates is gone"
    return match[1]


def test_certificates_are_made_in_one_helper_which_reuses_the_secret_of_the_cluster():
    # A render cannot tell reuse from a new certificate; this keeps the one place that does it from being bypassed.
    makers = sorted(
        path.name
        for path in TEMPLATES.iterdir()
        if re.search(r"\b(genCA|genSignedCert|genSelfSignedCert)\b", path.read_text())
    )
    assert makers == ["_helpers.tpl"], f"certificates are made in {makers}"
    body = helper_body()
    lookup, generate = body.find('lookup "v1" "Secret"'), body.find("genCA")
    assert 0 <= lookup < generate, "the cluster is looked at before a new certificate is made"
    # reused only for the service it was made for, and only whole
    assert '"impact.ai/service"' in body
    assert all(f'hasKey $existing.data "{key}"' in body for key in ("tls.crt", "tls.key", "ca.crt"))
    for template, helper in (
        ("cover.yaml", "cluster-kit.coverCertificate"),
        ("operator-webhooks.yaml", "cluster-kit.operatorCertificate"),
    ):
        assert helper in (TEMPLATES / template).read_text(), f"{template} makes its own certificate"
