#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# trivy config on what the chart renders: no HIGH or CRITICAL finding, on the oldest and the newest Kubernetes minor
# version the chart is tested on (1.27 and 1.34), for the install with a pasted key and the install with a token. The
# checks are the ones built into the pinned trivy, so the result depends on mise.toml and not on the day. An accepted
# finding is named in .trivyignore, with the reason.
#
# A reader who runs trivy with every severity also sees these findings, which are not gates; the reasons:
#   KSV-0113 (MEDIUM, manage namespace secrets) on the Roles impact-ai-exchange and impact-ai-uninstall: each names the one
#            Secret that holds the cluster's ingest key (get and patch to store it, delete to remove it), exists only while
#            a Helm hook runs, and has no create, list, watch or update. The chart README ("Permissions") says the same.
#   KSV-0125 (MEDIUM, trusted registries): the images come from ghcr.io, by digest; a mirror is global.imageRegistry.
#   KSV-01010 (MEDIUM, sensitive content in a ConfigMap): the gateway configuration sets an "authorization" header whose
#            value is ${env:IMPACT_INGEST_KEY}, a reference to the Secret and not the key.
#   KSV-0011 (LOW, CPU not limited): the kit's pods request CPU and limit memory.
# No role of the kit writes a pod (chart/tests/test_chart_pod_writes.py), so KSV-0048 does not appear.
#
#   scripts/scan-chart.sh
set -euo pipefail
cd "$(dirname "$0")/.."

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# Two installs: the pasted key, and an install token with automatic coverage and the Node.js image, which renders the
# exchange hook, the cover pod, the uninstall role that patches pods and the Node.js injection.
digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
for version in 1.27.0 1.34.0; do
  helm template impact-ai chart/impact-ai --namespace impact-ai -f chart/impact-ai/ci/values.yaml \
    --kube-version "$version" >"$scratch/chart-key-$version.yaml"
  helm template impact-ai chart/impact-ai --namespace impact-ai -f chart/impact-ai/ci/values.yaml \
    --kube-version "$version" --set ingest.key= --set-string install.token=impactinstall_ci_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 \
    --set-string images.nodejs.tag=v0.0.0-test --set-string "images.nodejs.digest=$digest" >"$scratch/chart-token-$version.yaml"
done
# The findings are listed one per line, nothing is printed when clean.
trivy config --severity HIGH,CRITICAL --exit-code 1 --quiet --skip-check-update --ignorefile .trivyignore \
  --format template \
  --template '{{ range . }}{{ $target := .Target }}{{ range .Misconfigurations }}{{ .Severity }} {{ .ID }} {{ .Title }}: {{ .Message }} ({{ $target }}){{ "\n" }}{{ end }}{{ end }}' \
  "$scratch"
echo "   chart: no HIGH or CRITICAL misconfiguration at Kubernetes 1.27 and 1.34, with a pasted key and with an install token"
