#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Scan built images: nothing internal in their files, no fixable CRITICAL or HIGH vulnerability (scan_archive in
# scripts/lib.sh). Without arguments: the three images that are released, as `make images` builds them.
#
#   scripts/scan-images.sh [<image>...]        (CONTAINER=docker|podman)
#
# trivy downloads its vulnerability database on the first run.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

if [ "$#" -eq 0 ]; then
  set -- "$IMAGE_PREFIX/python:$TAG" "$IMAGE_PREFIX/nodejs:$TAG" "$IMAGE_PREFIX/reporter:$TAG"
fi
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
for image in "$@"; do
  echo "== scan $image"
  save_image "$image" "$scratch/image.tar"
  scan_archive "$scratch/image.tar"
  rm -f "$scratch/image.tar"
done
