#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# End to end on a kind cluster: build the images, create the cluster, load the images, run e2e/ (and the container tests
# that need the heavier fixtures: the tracers, the agent frameworks, the Node.js qualification matrix and the AI gateways),
# delete the cluster. E2E_KEEP=1 keeps it; E2E_CLUSTER names it.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

CLUSTER="${E2E_CLUSTER:-impact-e2e}"
cleanup() {
  [ -n "${E2E_KEEP:-}" ] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  rm -rf build/images
}
trap cleanup EXIT

# kind runs kubelet, the control plane and every controller in the same user; the default limit (128 inotify instances)
# is easily used up on a busy machine and then the Operator cannot start ("too many open files"). Raise it where we may.
if [ "$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)" -lt 512 ]; then
  sudo -n sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288 >/dev/null 2>&1 \
    || echo "warning: fs.inotify.max_user_instances is below 512 and cannot be raised here; pods may restart with 'too many open files'" >&2
fi

started=$(date +%s)
step() { echo "=== $(( $(date +%s) - started ))s  $*"; }

# The Kubernetes version of the test cluster is the node image of e2e/kind.yaml. E2E_NODE_IMAGE replaces it, to run the suite
# on another version, for example the oldest one the chart supports (1.27):
#   E2E_NODE_IMAGE=docker.io/kindest/node:v1.27.16@sha256:2d21a61643eafc439905e18705b8186f3296384750a835ad7a005dceb9546d20 make e2e
# Another version tests the chart and the Operator on that version: the container suites that follow (tracers, agents,
# the Node.js matrix, the gateways) do not use the cluster, so they are not run again, and their images are not built.
node_image=()
if [ -n "${E2E_NODE_IMAGE:-}" ]; then node_image=(--image "$E2E_NODE_IMAGE"); fi

step "build images"
if [ -n "${E2E_NODE_IMAGE:-}" ]; then
  scripts/build-images.sh e2e tracers
else
  scripts/build-images.sh e2e tracers agents
fi

step "create cluster $CLUSTER (node image: ${E2E_NODE_IMAGE:-the one in e2e/kind.yaml})"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --config e2e/kind.yaml ${node_image[@]+"${node_image[@]}"} --wait 180s
echo "   Kubernetes $(kubectl --context "kind-$CLUSTER" get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"

# Fewer pods, fewer inotify instances (each pod's runtime shim uses some), and the suite needs neither: the cluster is
# one node with no volumes, and one DNS replica is enough.
kubectl --context "kind-$CLUSTER" -n local-path-storage scale deployment local-path-provisioner --replicas=0 >/dev/null
kubectl --context "kind-$CLUSTER" -n kube-system scale deployment coredns --replicas=1 >/dev/null

step "load images"
mkdir -p build/images
save=(save -o)
if [ "$CONTAINER" = podman ]; then save=(save --format docker-archive -o); fi
for image in python:"$TAG" nodejs:"$TAG" reporter:"$TAG" mock:"$TAG" fixture-app:3.12 fixture-app-ddtrace:3.12 fixture-app-otel:3.12 fixture-node:22; do
  file="build/images/$(echo "$image" | tr ':/' '__').tar"
  "$CONTAINER" "${save[@]}" "$file" "$IMAGE_PREFIX/$image" >/dev/null
  kind load image-archive "$file" --name "$CLUSTER"
  rm -f "$file"
done

export E2E_CONTEXT="kind-$CLUSTER"
# mise.toml lists Helm 4 after Helm 3: one install of the suite is made, and removed, with it (e2e/conftest.py, helm4).
if command -v mise >/dev/null 2>&1; then
  helm4="$(mise exec helm@4 -- sh -c 'command -v helm' 2>/dev/null || true)"
  if [ -n "$helm4" ]; then E2E_HELM4_DIR="$(dirname "$helm4")"; export E2E_HELM4_DIR; fi
fi
[ -n "${E2E_HELM4_DIR:-}" ] || echo "warning: no Helm 4 found with mise; the install that is made with Helm 4 is skipped" >&2
step "run e2e/"
uv run --project python --frozen pytest e2e -v -p no:cacheprovider --tb=short
if [ -n "${E2E_NODE_IMAGE:-}" ]; then
  step "done (the Kubernetes suite only, on the node image above)"
  exit 0
fi
step "run the tracer container tests"
uv run --project python --frozen pytest python/tests/image -m tracers -v -p no:cacheprovider --tb=short
step "run the agent container tests"
uv run --project python --frozen pytest python/tests/image -m agents -n 3 -v -p no:cacheprovider --tb=short
step "run the Node.js qualification matrix"
uv run --project python --frozen pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m matrix -n 3 -v -p no:cacheprovider --tb=short
step "run the AI gateway container tests"
uv run --project python --frozen pytest python/tests/image -m gateways -v -p no:cacheprovider --tb=short
step "done"
