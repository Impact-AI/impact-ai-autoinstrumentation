#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The GitHub release of a version that make release has pushed and signed, in two steps the release workflow runs:
#
#   scripts/github-release.sh prepare <version> [<release dir>]   the files, SHA256SUMS of them and its signature
#   scripts/github-release.sh publish <version> [<release dir>]   a draft release with those files, then published
#
# <release dir> is what make release wrote (default build/release). prepare puts in <release dir>/github:
#   impact-ai-<number>.tgz                                      the chart package that was pushed
#   <image>-<number>-linux-<arch>.spdx.json                     the SPDX bill of materials of each image and platform
#   LICENSE, NOTICE
#   SHA256SUMS                                                  the SHA-256 of each of them
#   SHA256SUMS.sigstore.json                                    cosign's signature of SHA256SUMS, verified at once
# and the notes, <release dir>/NOTES.md: the version's section of CHANGELOG.md (prepare stops without one), what was
# published and how to verify it (scripts/release_notes.py). Between the two steps the workflow records the build
# provenance of the files listed in SHA256SUMS; publish attaches it as provenance.sigstore.json when PROVENANCE_BUNDLE
# names it. publish makes a draft release of the tag v<number> (which has to exist), uploads the files, and publishes
# it: with immutable releases on, the release and its files never change after that. A pre-release version (0.3.0-rc1)
# is published as a pre-release.
#
# Signing is keyless with the job's id-token permission, as for the images, or with COSIGN_KEY in a rehearsal
# (scripts/lib.sh), whose public key COSIGN_PUBLIC_KEY verifies it. publish needs gh with GH_TOKEN allowed to write
# contents. GITHUB_REPOSITORY names the repository (RELEASE_REPOSITORY of scripts/names.sh outside Actions).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

die() {
  echo "github-release: $*" >&2
  exit 1
}

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "usage: scripts/github-release.sh prepare|publish <version> [<release dir>]" >&2
  exit 2
fi
step=$1
number="${2#v}"
[[ "$number" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] || die "'$2' is not a version"
tag="v$number"
out="${3:-build/release}"
files="$out/github"
repository="${GITHUB_REPOSITORY:-$RELEASE_REPOSITORY}"
identity="https://github.com/$repository/.github/workflows/release.yml@refs/tags/$tag"

prepare() {
  python3 scripts/release_notes.py "$tag" "$out" --repository "$repository" >"$out/NOTES.md" ||
    die "no notes for $tag (above): add its section to CHANGELOG.md"
  rm -rf "$files"
  mkdir -p "$files"
  cp "$out/$CHART_NAME-$number.tgz" "$files/"
  local name platform arch
  for name in $(release_images); do
    for platform in linux/amd64 linux/arm64; do
      arch="${platform#linux/}"
      cp "$out/$name-$arch.spdx.json" "$files/$(published_image "$name")-$number-linux-$arch.spdx.json"
    done
  done
  cp LICENSE NOTICE "$files/"
  (cd "$files" && sha256sum -- * >SHA256SUMS)

  set_cosign_args
  cosign sign-blob "${COSIGN_ARGS[@]}" --bundle "$files/SHA256SUMS.sigstore.json" "$files/SHA256SUMS"
  # verified the way the release notes tell a customer to
  local -a who=(--certificate-identity "$identity"
    --certificate-oidc-issuer https://token.actions.githubusercontent.com)
  if [ "$(signing_mode)" = key ]; then
    [ -n "${COSIGN_PUBLIC_KEY:-}" ] || die "COSIGN_PUBLIC_KEY is needed to verify what COSIGN_KEY signed"
    who=(--key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true)
  fi
  cosign verify-blob "$files/SHA256SUMS" --bundle "$files/SHA256SUMS.sigstore.json" "${who[@]}"
  (cd "$files" && sha256sum --check --quiet SHA256SUMS)
  echo "prepared the files of $tag in $files"
}

publish() {
  [ -s "$files/SHA256SUMS.sigstore.json" ] || die "$files has no signed SHA256SUMS: run prepare first"
  [ -s "$out/NOTES.md" ] || die "$out/NOTES.md is missing: run prepare first"
  if [ -n "${PROVENANCE_BUNDLE:-}" ]; then
    cp "$PROVENANCE_BUNDLE" "$files/provenance.sigstore.json"
  fi
  local -a flags=(--repo "$repository" --draft --verify-tag --title "$number" --notes-file "$out/NOTES.md")
  if [[ "$number" == *-* ]]; then flags+=(--prerelease); fi
  gh release create "$tag" "${flags[@]}" "$files"/*
  gh release edit "$tag" --repo "$repository" --draft=false
  echo "published the GitHub release $tag of $repository"
}

case "$step" in
  prepare) prepare ;;
  publish) publish ;;
  *) die "'$step' is neither prepare nor publish" ;;
esac
