#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The unit tests of the Node.js package, in a container (nodejs/tests/Dockerfile): the pinned Node of the build, and the
# test-only dependencies of nodejs/tests/package.json.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

image="$IMAGE_PREFIX/nodejs-unit:$TAG"
"$CONTAINER" build -q -f nodejs/tests/Dockerfile -t "$image" nodejs >/dev/null
"$CONTAINER" run --rm "$image"
