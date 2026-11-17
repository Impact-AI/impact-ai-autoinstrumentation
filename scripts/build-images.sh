#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Build the images the tests use: scripts/build-images.sh core|e2e|agents|tracers|overhead|lambda ...
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
build_images "${@:-core}"
