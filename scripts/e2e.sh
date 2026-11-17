#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# End to end on a kind cluster: build the images, create the cluster, load the images, run e2e/ (and the container tests
# that need the heavier fixtures: the tracers, the agent frameworks, the Node.js qualification matrix, the AI gateways and
# the cells of the Python qualification of the libraries the change touches), delete the cluster. E2E_KEEP=1 keeps it;
# E2E_CLUSTER names it.
#
#   scripts/e2e.sh          all of it, on one cluster, in order (make e2e)
#   scripts/e2e.sh images   only build the images the cluster runs, and leave their archives in build/images
#   scripts/e2e.sh kind     only the suite on the cluster, from the archives `images` left in build/images; E2E_TESTS
#                           names the modules to run, in order (default: all of e2e/)
#   scripts/e2e.sh python   only the tracer, agent framework and AI gateway container tests
#   scripts/e2e.sh nodejs   only the Node.js qualification matrix
#
# CI runs the parts as jobs of their own, each under ten minutes: the images once, the suite in shards of modules on
# clusters of their own (scripts/ci_jobs.py names them), the two container parts, and the cells of the Python
# qualification as `make test-python-cells`.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

part="${1:-all}"
case "$part" in
  all | images | kind | python | nodejs) ;;
  *) echo "usage: scripts/e2e.sh [images|kind|python|nodejs]" >&2; exit 2 ;;
esac

CLUSTER="${E2E_CLUSTER:-impact-e2e}"
# The images the cluster runs (the kind group of scripts/lib.sh), and the archive each is loaded from.
KIND_IMAGES=(python:"$TAG" nodejs:"$TAG" reporter:"$TAG" mock:"$TAG" fixture-app:3.12 fixture-app-ddtrace:3.12 fixture-app-otel:3.12 fixture-node:22)
archive_of() { echo "build/images/$(echo "$1" | tr ':/' '__').tar"; }

cleanup() {
  case "$part" in
    all | kind) [ -n "${E2E_KEEP:-}" ] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true ;;
  esac
  # all makes, loads and removes the archives one by one; those of `images` are what it is for, and `kind` reads them
  if [ "$part" = all ]; then rm -rf build/images; fi
}
trap cleanup EXIT

started=$(date +%s)
step() { echo "=== $(( $(date +%s) - started ))s  $*"; }

# The Kubernetes version of the test cluster is the node image of e2e/kind.yaml. E2E_NODE_IMAGE replaces it, to run the suite
# on another version, for example the oldest one the chart supports (1.27):
#   E2E_NODE_IMAGE=docker.io/kindest/node:v1.27.16@sha256:2d21a61643eafc439905e18705b8186f3296384750a835ad7a005dceb9546d20 make e2e
# Another version tests the chart and the Operator on that version: the container suites that follow (tracers, agents,
# the Node.js matrix, the gateways) do not use the cluster, so they are not run again, and their images are not built.
node_image=()
if [ -n "${E2E_NODE_IMAGE:-}" ]; then node_image=(--image "$E2E_NODE_IMAGE"); fi

save=(save -o)
if [ "$CONTAINER" = podman ]; then save=(save --format docker-archive -o); fi
save_archive() { "$CONTAINER" "${save[@]}" "$(archive_of "$1")" "$IMAGE_PREFIX/$1" >/dev/null; }

create_cluster() {
  # kind runs kubelet, the control plane and every controller in the same user; the default limit (128 inotify instances)
  # is easily used up on a busy machine and then the Operator cannot start ("too many open files"). Raise it where we may.
  if [ "$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)" -lt 512 ]; then
    sudo -n sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288 >/dev/null 2>&1 \
      || echo "warning: fs.inotify.max_user_instances is below 512 and cannot be raised here; pods may restart with 'too many open files'" >&2
  fi
  step "create cluster $CLUSTER (node image: ${E2E_NODE_IMAGE:-the one in e2e/kind.yaml})"
  kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  kind create cluster --name "$CLUSTER" --config e2e/kind.yaml ${node_image[@]+"${node_image[@]}"} --wait 180s
  echo "   Kubernetes $(kubectl --context "kind-$CLUSTER" get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
  # Fewer pods, fewer inotify instances (each pod's runtime shim uses some), and the suite needs neither: the cluster is
  # one node with no volumes, and one DNS replica is enough.
  kubectl --context "kind-$CLUSTER" -n local-path-storage scale deployment local-path-provisioner --replicas=0 >/dev/null
  kubectl --context "kind-$CLUSTER" -n kube-system scale deployment coredns --replicas=1 >/dev/null
}

# run_suite <pytest argument>...: the modules of e2e/ on the cluster
run_suite() {
  export E2E_CONTEXT="kind-$CLUSTER"
  # mise.toml lists Helm 4 after Helm 3: one install of the suite is made, and removed, with it (e2e/conftest.py, helm4).
  if command -v mise >/dev/null 2>&1; then
    helm4="$(mise exec helm@4 -- sh -c 'command -v helm' 2>/dev/null || true)"
    if [ -n "$helm4" ]; then E2E_HELM4_DIR="$(dirname "$helm4")"; export E2E_HELM4_DIR; fi
  fi
  [ -n "${E2E_HELM4_DIR:-}" ] || echo "warning: no Helm 4 found with mise; the install that is made with Helm 4 is skipped" >&2
  step "run e2e/${E2E_TESTS:+ (${E2E_TESTS})}"
  uv run --project python --frozen pytest "$@" -v -p no:cacheprovider --tb=short
}

run_tracers() {
  step "run the tracer container tests"
  uv run --project python --frozen pytest python/tests/image -m tracers -v -p no:cacheprovider --tb=short
}
run_agents() {
  step "run the agent container tests"
  uv run --project python --frozen pytest python/tests/image -m agents -n 3 -v -p no:cacheprovider --tb=short
}
run_matrix() {
  step "run the Node.js qualification matrix"
  uv run --project python --frozen pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m matrix -n 3 -v -p no:cacheprovider --tb=short
}
run_gateways() {
  step "run the AI gateway container tests"
  uv run --project python --frozen pytest python/tests/image -m gateways -v -p no:cacheprovider --tb=short
}

case "$part" in
  images)
    step "build the images the cluster runs"
    scripts/build-images.sh kind
    step "save their archives to build/images"
    mkdir -p build/images
    for image in "${KIND_IMAGES[@]}"; do save_archive "$image"; done
    du -sh build/images
    step "done"
    exit 0
    ;;
  kind)
    read -r -a tests <<<"${E2E_TESTS:-e2e}"
    for image in "${KIND_IMAGES[@]}"; do
      if [ ! -f "$(archive_of "$image")" ]; then
        echo "no $(archive_of "$image"): build the archives first (scripts/e2e.sh images, make e2e-images)" >&2
        exit 1
      fi
    done
    create_cluster
    step "load images"
    for image in "${KIND_IMAGES[@]}"; do kind load image-archive "$(archive_of "$image")" --name "$CLUSTER"; done
    run_suite "${tests[@]}"
    step "done"
    exit 0
    ;;
  python)
    step "build images"
    scripts/build-images.sh agents tracers
    # the gateways' images are large: in CI the build cache goes first, as in the full run
    if [ -n "${CI:-}" ] && [ "$CONTAINER" = docker ]; then docker builder prune -af >/dev/null 2>&1 || true; fi
    run_tracers
    run_agents
    run_gateways
    step "done"
    exit 0
    ;;
  nodejs)
    step "build images"
    scripts/build-images.sh core-nodejs
    run_matrix
    step "done"
    exit 0
    ;;
esac

# all: the cells of the Python qualification (python/qualification.json) of the libraries this change touches: none for most
# changes, every cell for a change to the package or to what every cell runs on (scripts/qualify_python.py select). A CI
# runner's disk holds their fixtures only once the cluster and the build cache are gone, so they are built after e2e/,
# run, and removed again before the other container suites (the gateways' images are large too).
cells=""
if [ -z "${E2E_NODE_IMAGE:-}" ]; then
  cells="$(python3 scripts/qualify_python.py select)"
fi

step "build images"
if [ -n "${E2E_NODE_IMAGE:-}" ]; then
  scripts/build-images.sh e2e tracers
else
  scripts/build-images.sh e2e tracers agents
fi

create_cluster

step "load images"
mkdir -p build/images
for image in "${KIND_IMAGES[@]}"; do
  save_archive "$image"
  kind load image-archive "$(archive_of "$image")" --name "$CLUSTER"
  rm -f "$(archive_of "$image")"
done

run_suite e2e
if [ -n "${E2E_NODE_IMAGE:-}" ]; then
  step "done (the Kubernetes suite only, on the node image above)"
  exit 0
fi
if [ -z "${E2E_KEEP:-}" ]; then
  step "delete the cluster (and in CI the build cache): the container suites that follow need neither"
  kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  if [ -n "${CI:-}" ] && [ "$CONTAINER" = docker ]; then docker builder prune -af >/dev/null 2>&1 || true; fi
  df -h / | tail -1
fi
step "run the Python qualification's cells of the libraries this change touches: ${cells:-none}"
if [ -n "$cells" ]; then
  images() { "$CONTAINER" images --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | sort -u; }
  before="$(images)"
  CELL_LIBRARIES="$cells" scripts/build-images.sh cell-fixtures
  CELLS="$cells" CELLS_BUILD=0 make --no-print-directory test-python-cells
  # only the fixtures built for the cells go; the images of the suites below stay
  added="$(comm -13 <(echo "$before") <(images))"
  if [ -n "$added" ]; then
    # shellcheck disable=SC2086 # a list of image names
    "$CONTAINER" rmi $added >/dev/null 2>&1 || true
  fi
  if [ -n "${CI:-}" ] && [ "$CONTAINER" = docker ]; then docker builder prune -af >/dev/null 2>&1 || true; fi
  df -h / | tail -1
fi
run_tracers
run_agents
run_matrix
run_gateways
step "done"
