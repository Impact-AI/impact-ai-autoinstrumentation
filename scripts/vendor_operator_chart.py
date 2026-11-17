#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Vendor the OpenTelemetry Operator's Helm chart into the cluster kit chart.

    python3 scripts/vendor_operator_chart.py            # (re)create the vendored copy
    python3 scripts/vendor_operator_chart.py --check    # fail if the vendored copy differs from what this script makes

The upstream chart (open-telemetry/opentelemetry-helm-charts, Apache-2.0) is fetched at a pinned version, verified by
SHA-256, and reduced to what the kit needs. Nothing else is changed except:

  * every object template gets a guard, `opentelemetry-operator.impact.enabled`, so the chart can leave the Operator
    out when another injector owns the cluster (operator.install: auto | "true" | "false");
  * the image helper honours global.imageRegistry (one value points every image at a mirror), and so does every image
    argument of the manager (--collector-image=..., --auto-instrumentation-<language>-image=...): they go through the
    same helper, so that the Deployment names no image outside the mirror either;
  * the webhook configurations, their certificate and the Instrumentation CRD are NOT taken from upstream: the kit
    renders them itself (namespace selection, fail-open settings, CRD kept on uninstall), see chart/impact-ai/templates;
  * the Operator's roles and their bindings are NOT taken from upstream either. Upstream's ClusterRole is made for the
    collector, target allocator and OpAMP features, which the kit does not install: it can create pods and workloads in
    any namespace, and below Kubernetes 1.33 it reaches nodes/proxy. The kit writes a role of its own for the one thing
    it uses, injecting an Instrumentation (chart/impact-ai/templates/operator-rbac.yaml). When the Operator is bumped,
    read its kubebuilder:rbac markers for the pod webhook and update that file; the end-to-end suite fails on a
    "forbidden" in the Operator's log.

Bumping the Operator: change VERSION and SHA256 (from the repository's index.yaml), run the script, review the diff of
the vendored files against the previous version, run `make ci` and `make e2e`. A language the new Operator can inject
must be switched off by name in operator.manager.extraArgs (chart/impact-ai/values.yaml): chart/tests/test_chart.py
reads the languages from the vendored deployment template and the Instrumentation CRD and fails on one left to a default.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.128.0"
SHA256 = "9b312684c178c7721664dba0153c03b396bba0da2da89de80997d40ee1e22d6a"
URL = f"https://github.com/open-telemetry/opentelemetry-helm-charts/releases/download/opentelemetry-operator-{VERSION}/opentelemetry-operator-{VERSION}.tgz"
LICENSE_URL = (
    "https://raw.githubusercontent.com/open-telemetry/opentelemetry-helm-charts/opentelemetry-operator-{v}/LICENSE"
)

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "chart" / "impact-ai"
VENDORED = CHART / "charts" / "opentelemetry-operator"
CRD_FILE = CHART / "crds" / "instrumentations.opentelemetry.io.yaml"

KEEP_TEMPLATES = (
    "_helpers.tpl",
    "deployment.yaml",
    "service.yaml",
    "serviceaccount.yaml",
)
GUARDED = tuple(t for t in KEEP_TEMPLATES if t != "_helpers.tpl")
GUARD_OPEN = '{{- if include "opentelemetry-operator.impact.enabled" . }}\n'
GUARD_CLOSE = "\n{{- end }}\n"

HELPERS_ADDITION = """
{{/*
impact.ai: whether this release installs the Operator. operator.install is "true" (always), "false" (never) or "auto"
(unless another injector owns the cluster, see cluster-kit.foreignInjector in the parent chart). Evaluated with the
subchart's values, where the parent's `operator:` block is `.Values`.
*/}}
{{- define "opentelemetry-operator.impact.enabled" -}}
{{- $mode := "auto" -}}
{{- if hasKey .Values "install" -}}{{- $mode = toString .Values.install -}}{{- end -}}
{{- if eq $mode "true" -}}true
{{- else if and (eq $mode "auto") (not (include "cluster-kit.foreignInjector" .)) -}}true
{{- end -}}
{{- end }}
"""

IMAGE_HELPER = """{{- define "opentelemetry-operator.image" -}}
{{- include "cluster-kit.imageReference" (dict "repository" .Values.manager.image.repository "tag" (default .Chart.AppVersion .Values.manager.image.tag) "registry" (default "" .Values.global.imageRegistry)) }}
{{- end }}"""

# An image argument of the manager as upstream writes it, `--<name>-image={{ .Values.manager.<image>.repository }}:{{
# .Values.manager.<image>.tag }}`, and as the kit renders it: through the same helper as the manager's own image.
IMAGE_ARGUMENT = re.compile(
    r"(?P<flag>--[a-z0-9-]+-image)=\{\{ (?P<image>\.Values\.manager\.[A-Za-z0-9.]+)\.repository \}\}:\{\{ (?P=image)\.tag \}\}"
)
IMAGE_ARGUMENT_HELPER = '{flag}={{{{ include "cluster-kit.imageReference" (dict "repository" {image}.repository "tag" {image}.tag "registry" (default "" .Values.global.imageRegistry)) }}}}'


def mirror_image_arguments(deployment: str) -> str:
    """The deployment template with every image argument of the manager going through the kit's image helper."""
    mirrored, count = IMAGE_ARGUMENT.subn(lambda m: IMAGE_ARGUMENT_HELPER.format(**m.groupdict()), deployment)
    if not count or re.search(r"-image=\{\{ \.Values", mirrored):
        raise SystemExit("upstream image arguments not found as expected; the vendoring script needs an update")
    return mirrored


OPERATOR_VALUES_NOTE = """# Vendored from open-telemetry/opentelemetry-helm-charts, chart opentelemetry-operator {version}
# (Apache-2.0). See NOTICE and scripts/vendor_operator_chart.py. The kit sets these values in chart/impact-ai/values.yaml
# under `operator:`; this file only supplies the defaults it does not override.
"""


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL, verified below
        return response.read()


def build(target: Path, crd_target: Path, archive: bytes) -> None:
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise SystemExit("the downloaded chart does not match the pinned SHA-256; refusing to vendor it")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}

        def read(name: str) -> str:
            return tar.extractfile(members[f"opentelemetry-operator/{name}"]).read().decode()  # type: ignore[union-attr]

        files = {name: read(f"templates/{name}") for name in KEEP_TEMPLATES}
        chart_yaml = read("Chart.yaml")
        values_yaml = read("values.yaml")
        crd = read("conf/crds/crd-opentelemetryinstrumentation.yaml")

    out = {}
    for name, text in files.items():
        if name in GUARDED:
            if name == "deployment.yaml":
                text = mirror_image_arguments(text)
            out[f"templates/{name}"] = GUARD_OPEN + text.rstrip("\n") + GUARD_CLOSE
        else:
            helpers = re.sub(
                r'\{\{- define "opentelemetry-operator\.image" -\}\}.*?\{\{- end \}\}',
                lambda _: IMAGE_HELPER,
                text,
                flags=re.S,
            )
            if helpers == text:
                raise SystemExit("upstream image helper not found; the vendoring script needs an update")
            out[f"templates/{name}"] = helpers.rstrip("\n") + "\n" + HELPERS_ADDITION
    out["Chart.yaml"] = chart_yaml
    out["values.yaml"] = OPERATOR_VALUES_NOTE.format(version=VERSION) + values_yaml
    license_text = fetch(LICENSE_URL.format(v=VERSION)).decode()
    out["LICENSE"] = license_text
    out["NOTICE"] = (
        f"This directory contains the OpenTelemetry Operator Helm chart {VERSION} from\n"
        "https://github.com/open-telemetry/opentelemetry-helm-charts (Apache License 2.0, see LICENSE),\n"
        "reduced and guarded by scripts/vendor_operator_chart.py. Upstream copyright: The OpenTelemetry Authors.\n"
    )

    if target.exists():
        shutil.rmtree(target)
    for relative, text in out.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # The Instrumentation CRD as a static file for the chart's crds/ directory: Helm installs it before anything that
    # uses it and never deletes it, so uninstalling the kit keeps the definition and a reinstall adopts it.
    lines = [line for line in crd.splitlines() if "{{" not in line]
    crd_target.parent.mkdir(parents=True, exist_ok=True)
    crd_target.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    archive = fetch(URL)
    if not args.check:
        build(VENDORED, CRD_FILE, archive)
        print(f"vendored opentelemetry-operator {VERSION} into {VENDORED.relative_to(ROOT)}", file=sys.stderr)
        return 0
    with tempfile.TemporaryDirectory() as scratch:
        expected, expected_crd = Path(scratch) / "chart", Path(scratch) / "crd.yaml"
        build(expected, expected_crd, archive)
        problems = []
        for path in sorted(expected.rglob("*")):
            if path.is_file() and (VENDORED / path.relative_to(expected)).read_text() != path.read_text():
                problems.append(str(path.relative_to(expected)))
        extra = {str(p.relative_to(VENDORED)) for p in VENDORED.rglob("*") if p.is_file()} - {
            str(p.relative_to(expected)) for p in expected.rglob("*") if p.is_file()
        }
        if CRD_FILE.read_text() != expected_crd.read_text():
            problems.append(str(CRD_FILE.relative_to(ROOT)))
        if problems or extra:
            print(
                f"vendored files differ from the script's output: {problems} (extra: {sorted(extra)})", file=sys.stderr
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
