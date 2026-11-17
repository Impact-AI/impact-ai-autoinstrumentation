#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Scan built images: nothing internal in their files, no fixable CRITICAL or HIGH vulnerability (scan_archive in
# scripts/lib.sh). Without arguments: the four images that are released, as `make images` and `make images-java` build
# them. A name without a registry or a tag is one of them, as built here: python, nodejs, java or reporter (the parts of
# `make ci` scan what they build).
#
#   scripts/scan-images.sh [<image>...]        (CONTAINER=docker|podman)
#
# trivy downloads its vulnerability database on the first run.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

if [ "$#" -eq 0 ]; then
  set -- python nodejs java reporter
fi
images=()
for name in "$@"; do
  case "$name" in
    */* | *:*) images+=("$name") ;;
    *) images+=("$IMAGE_PREFIX/$name:$TAG") ;;
  esac
done
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
for image in "${images[@]}"; do
  echo "== scan $image"
  save_image "$image" "$scratch/image.tar"
  scan_archive "$scratch/image.tar"
  rm -f "$scratch/image.tar"
done
