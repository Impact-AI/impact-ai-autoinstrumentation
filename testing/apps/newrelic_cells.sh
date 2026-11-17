#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# The New Relic cells of the competitor harness. Each agent runs the canonical interaction, at the versions pinned in
# testing/apps/newrelic-requirements.txt (Python) and testing/apps/newrelic-node/package-lock.json (Node.js), against
# the collector double over TLS (newrelic_record.py), and the relay's decoder must read the canonical interaction from
# what the agent sent (TestNewRelicFreshRecordings): an agent release that changes its events fails here, before any
# golden file is rewritten. The recordings stay in build/newrelic/recordings for a look.
#
#   testing/apps/newrelic_cells.sh [python] [nodejs]      # both when none is named
#
# Python runs with uv. Node.js runs on the machine when it is 22.16 or later (what the agent needs), and otherwise in
# the pinned node:24 image ($CONTAINER, host network, so that it reaches the double on localhost).
set -euo pipefail
# Copies, not hard links into uv's cache: nothing an environment here does can change another one's files.
export UV_LINK_MODE=copy

root=$(cd "$(dirname "$0")/../.." && pwd)
out=${NEWRELIC_OUT:-$root/build/newrelic}
container=${CONTAINER:-docker}
node_image=docker.io/library/node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
cells=("$@")
[ ${#cells[@]} -gt 0 ] || cells=(python nodejs)

rm -rf "$out"
mkdir -p "$out/recordings"
cd "$root"

# The variables newrelic_record.py sets for the agent, passed into a container by name.
agent_env=(NEW_RELIC_LICENSE_KEY NEW_RELIC_APP_NAME NEW_RELIC_HOST NEW_RELIC_PORT NEW_RELIC_CA_BUNDLE_PATH
    NEW_RELIC_CERTIFICATES NEW_RELIC_AI_MONITORING_ENABLED NEW_RELIC_DISTRIBUTED_TRACING_SAMPLER_ROOT NEW_RELIC_LOG
    NEW_RELIC_LOG_LEVEL)

node_is_recent() {
    command -v node >/dev/null 2>&1 &&
        node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 16) ? 0 : 1)'
}

python_cell() {
    uv venv -q --python 3.12 "$out/python/.venv"
    VIRTUAL_ENV="$out/python/.venv" uv pip install -q -r testing/apps/newrelic-requirements.txt
    python3 testing/apps/newrelic_record.py \
        --how "testing/apps/newrelic_app.py under New Relic's Python agent, against testing/apps/newrelic_collector.py (testing/apps/newrelic_cells.sh)" \
        --package "newrelic=$(sed -n 's/^newrelic==//p' testing/apps/newrelic-requirements.txt)" \
        --output "$out/recordings/python.recording.json" \
        -- "$out/python/.venv/bin/python" testing/apps/newrelic_app.py
}

nodejs_cell() {
    mkdir -p "$out/node"
    cp testing/apps/newrelic-node/package.json testing/apps/newrelic-node/package-lock.json \
        testing/apps/newrelic-node/app.cjs "$out/node/"
    local run
    if node_is_recent; then
        (cd "$out/node" && npm ci --no-audit --no-fund --loglevel=error)
        run=(node "$out/node/app.cjs")
    else
        local in_container=("$container" run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$out/node:/app" -w /app)
        "${in_container[@]}" "$node_image" npm ci --no-audit --no-fund --loglevel=error
        run=("${in_container[@]}" --network host)
        for name in "${agent_env[@]}"; do run+=(-e "$name"); done
        run+=("$node_image" node app.cjs)
    fi
    python3 testing/apps/newrelic_record.py \
        --how "testing/apps/newrelic-node/app.cjs under New Relic's Node.js agent, against testing/apps/newrelic_collector.py (testing/apps/newrelic_cells.sh)" \
        --package "newrelic=$(node -p 'require("./testing/apps/newrelic-node/package.json").dependencies.newrelic' 2>/dev/null ||
            sed -n 's/.*"newrelic": "\(.*\)".*/\1/p' testing/apps/newrelic-node/package.json)" \
        --output "$out/recordings/nodejs.recording.json" \
        -- "${run[@]}"
}

for cell in "${cells[@]}"; do
    case "$cell" in
    python) python_cell ;;
    nodejs) nodejs_cell ;;
    *)
        echo "unknown cell $cell: python or nodejs" >&2
        exit 2
        ;;
    esac
done

cd reporter
NEWRELIC_RECORDINGS="$out/recordings" go test -count=1 -v -run TestNewRelicFreshRecordings ./internal/relay/source/
