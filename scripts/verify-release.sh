#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Check a published release the way a customer does: the signature of the chart and of each image it carries (python,
# nodejs, reporter and, from the release that first publishes it, java), the signed SBOM of each image, and the build
# provenance GitHub recorded for all of them. These are the commands of the chart README
# ("Verify what you install"); chart/tests/test_release.py keeps the two in step.
#
#   VERSION=v0.3.0 scripts/verify-release.sh           (make verify-release)
#
# REGISTRY defaults to ghcr.io/Impact-AI/impact-ai-autoinstrumentation, the repository the release runs in, which is also where the
# signing identity comes from (REPOSITORY overrides it). The chart and the images are looked for under the publish root:
# the registry host and the lowercased owner, ghcr.io/impact-ai (PUBLISH_ROOT overrides it; scripts/names.sh). Needs helm,
# cosign and, for the provenance, the GitHub CLI signed in (GH_TOKEN). VERIFY_GITHUB_ATTESTATION=0 leaves the provenance
# out: the release script runs this before the workflow has recorded it. COSIGN_PUBLIC_KEY=<file> checks signatures made
# with a key (a rehearsal against a registry of your own) instead of the release workflow's identity.
set -euo pipefail

# shellcheck source=scripts/names.sh
source "$(dirname "$0")/names.sh"

: "${VERSION:?VERSION is required, for example v0.3.0}"
REGISTRY="${REGISTRY:-ghcr.io/Impact-AI/impact-ai-autoinstrumentation}"
number="${VERSION#v}"
repository="${REPOSITORY:-${REGISTRY#*/}}" # Impact-AI/impact-ai-autoinstrumentation
root="$(publish_root "$REGISTRY")"
chart="oci://$root/charts/$CHART_NAME"
plain=()
registry_flags=()
if [ -n "${INSECURE_REGISTRY:-}" ]; then
  plain=(--plain-http)
  registry_flags=(--allow-insecure-registry)
fi

if [ -n "${COSIGN_PUBLIC_KEY:-}" ]; then
  who=(--key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true)
else
  who=(
    --certificate-identity "https://github.com/$repository/.github/workflows/release.yml@refs/tags/$VERSION"
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  )
fi

# check <what> <command>...: run a check and show its output only when it fails
check() {
  local what=$1 output
  shift
  if output="$("$@" 2>&1)"; then
    echo "   ok   $what"
  else
    echo "   FAIL $what" >&2
    echo "$output" >&2
    exit 1
  fi
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# The chart: its digest, then everything else comes from the values it carries.
helm pull "$chart" --version "$number" --destination "$scratch" ${plain[@]+"${plain[@]}"} >"$scratch/pull.txt" 2>&1 \
  || { cat "$scratch/pull.txt" >&2; exit 1; }
chart_digest="$(sed -n 's/^Digest: //p' "$scratch/pull.txt")"
[ -n "$chart_digest" ] || { echo "helm pull printed no digest for $chart $number" >&2; exit 1; }

echo "== $chart $number ($chart_digest)"
check "signature of the chart" cosign verify "$root/charts/$CHART_NAME@$chart_digest" ${who[@]+"${who[@]}"} ${registry_flags[@]+"${registry_flags[@]}"}
if [ "${VERIFY_GITHUB_ATTESTATION:-1}" != 0 ]; then
  check "build provenance of the chart" gh attestation verify "oci://$root/charts/$CHART_NAME@$chart_digest" \
    --repo "$repository" --signer-workflow "$repository/.github/workflows/release.yml"
fi

# The digests of the images are in the values of the chart, which is signed.
values="$(helm show values "$chart" --version "$number" ${plain[@]+"${plain[@]}"} 2>"$scratch/show.txt")" \
  || { cat "$scratch/show.txt" >&2; exit 1; }
# A chart may have no Java image: releases before it have no images.java, and a release made without RELEASE_JAVA leaves
# its digest empty, `digest: ""` (scripts/lib.sh, release_images). Such a chart installs nothing of Java: nothing to check.
for image in python nodejs java reporter; do
  digest="$(awk -v name="  $image:" '/^images:/ {inside = 1; next} /^[^ #]/ {inside = 0} inside && $0 == name {found = 1; next} found && /^    digest: / {gsub(/"/, "", $2); print $2; exit}' <<<"$values")"
  if [ -z "$digest" ] && [ "$image" = java ]; then
    echo "== no $image image in $VERSION"
    continue
  fi
  [ -n "$digest" ] || { echo "the chart has no digest for $image" >&2; exit 1; }
  name="$(published_image "$image")"
  echo "== $root/$name@$digest"
  check "signature of $name" cosign verify "$root/$name@$digest" ${who[@]+"${who[@]}"} ${registry_flags[@]+"${registry_flags[@]}"}
  check "signed SBOM of $name" cosign verify-attestation --type spdxjson "$root/$name@$digest" ${who[@]+"${who[@]}"} ${registry_flags[@]+"${registry_flags[@]}"}
  if [ "${VERIFY_GITHUB_ATTESTATION:-1}" != 0 ]; then
    check "build provenance of $name" gh attestation verify "oci://$root/$name@$digest" \
      --repo "$repository" --signer-workflow "$repository/.github/workflows/release.yml"
  fi
done
echo "== verified $VERSION"
