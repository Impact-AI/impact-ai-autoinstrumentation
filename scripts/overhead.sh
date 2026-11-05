#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Measure the overhead of the injected workloads (AE-17) and judge it by the budget: +1.5 ms of CPU per model call, +40 MiB of
# memory and +2 s to start, each on the median of paired differences, a failing workload measured once more.
#
#   scripts/overhead.sh canary             what `make ci` runs: Python 3.12 under uvicorn and Node 22, one round each
#   scripts/overhead.sh covering           seven workloads, two rounds each: every Python, every Node and both servers at least
#                                          once (for `make ci`, if the full matrix is not a job of its own)
#   scripts/overhead.sh full [DIRECTORY]   what `make overhead` and `make release` run: all eleven workloads, three rounds each
#
# The records, results.json and OVERHEAD.md are written to DIRECTORY (default build/overhead). The images are built first
# (scripts/build-images.sh overhead) unless OVERHEAD_BUILD=0 says they are there. Exits non-zero when a workload is over its
# budget, after the report has been made. One measurement at a time: it needs the machine quiet, and no other container run
# or cluster beside it.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

mode="${1:?usage: scripts/overhead.sh canary|covering|full [directory]}"
out="${2:-build/overhead}"
rm -rf "$out"
mkdir -p "$out/combos"

case "$mode" in
  canary) export OVERHEAD_ROUNDS=1 OVERHEAD_COMBOS="python3.12-uvicorn,node22-express" ;;
  covering)
    export OVERHEAD_ROUNDS="${OVERHEAD_ROUNDS:-2}"
    export OVERHEAD_COMBOS="python3.11-uvicorn,python3.12-gunicorn,python3.13-uvicorn,python3.14-gunicorn,node22-express,node24-express,node26-express"
    ;;
  full) export OVERHEAD_ROUNDS="${OVERHEAD_ROUNDS:-3}" ;;
  *) echo "unknown mode: $mode (canary, covering or full)" >&2; exit 2 ;;
esac
export OVERHEAD_DIR="$out"

if [ "${OVERHEAD_BUILD:-1}" != 0 ]; then
  scripts/build-images.sh overhead
fi

status=0
uv run --project python --frozen pytest -c python/pyproject.toml --rootdir . python/tests/overhead -m overhead -v -p no:cacheprovider --tb=short || status=$?
# the report is made whether or not the budget held: it is what shows by how much
python3 scripts/overhead_report.py "$out" --version "$VERSION" || status=$?
exit "$status"
