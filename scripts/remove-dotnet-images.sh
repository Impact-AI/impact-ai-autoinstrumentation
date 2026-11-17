#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Remove what `make images-dotnet` built, after the .NET proofs: the CI runner's disk is shared with the image builds and
# the vulnerability scan that follow in `make ci`. In CI the build cache goes too (the SDK image is most of it).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
images="$("$CONTAINER" images --format '{{.Repository}}:{{.Tag}}' | grep -E "^$IMAGE_PREFIX/(dotnet|fixture-dotnet):" || true)"
if [ -n "$images" ]; then
  # shellcheck disable=SC2086 # a list of image names
  "$CONTAINER" rmi $images >/dev/null 2>&1 || true
fi
if [ -n "${CI:-}" ] && [ "$CONTAINER" = docker ]; then docker builder prune -af >/dev/null 2>&1 || true; fi
df -h / | tail -1
