#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The Python layer in AWS's Lambda images for Python, under the Runtime Interface Emulator they carry. Builds the mock,
# pulls the images by digest (lambda/rie-images.json), runs, and removes the images again unless LAMBDA_KEEP_IMAGES=1:
# they are large, and the jobs that run this have little disk to spare.
#
#   scripts/test-lambda.sh            the cells (python/tests/image/test_lambda_rie.py), then a smoke of the timings
#   scripts/test-lambda.sh timings    the timings (scripts/lambda_timings.py rie) with LAMBDA_TIMINGS_ARGS, by default
#                                     every Python at 512 and 1,024 MB, 30 cold starts a side
#
# Both after `make lambda-layers`; CONTAINER=docker|podman.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

what="${1:-cells}"
mapfile -t images < <(python3 -c 'import json; print("\n".join(json.load(open("lambda/rie-images.json"))["images"].values()))')
[ "${#images[@]}" -gt 0 ] || { echo "no Lambda images in lambda/rie-images.json" >&2; exit 2; }
cleanup() {
  if [ "${LAMBDA_KEEP_IMAGES:-0}" != 1 ]; then
    "$CONTAINER" rmi "${images[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for image in "${images[@]}"; do
  "$CONTAINER" pull -q "$image" >/dev/null
done
case "$what" in
  cells)
    build_images lambda
    uv run --project python --frozen pytest python/tests/image -m lambda_rie -n 3 -v -p no:cacheprovider --tb=short
    # The harness end to end, small: it must measure, and it reports against the proposed budget without failing on it.
    python3 scripts/lambda_timings.py rie --pythons 3.12 --memory 512 --cold-starts 3 --warm 5 --out build/lambda-timings
    ;;
  timings)
    # shellcheck disable=SC2086 # a list of arguments
    python3 scripts/lambda_timings.py rie --out build/lambda-timings ${LAMBDA_TIMINGS_ARGS:-}
    ;;
  *) echo "usage: $0 [cells|timings]" >&2; exit 2 ;;
esac
