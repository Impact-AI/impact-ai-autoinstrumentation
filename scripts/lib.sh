#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Shared by the scripts that build and run containers. CONTAINER=docker (CI) or CONTAINER=podman (a workstation).
# shellcheck shell=bash

# shellcheck source=scripts/names.sh
source "$(dirname "${BASH_SOURCE[0]}")/names.sh"

CONTAINER="${CONTAINER:-$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)}"
IMAGE_PREFIX="${IMAGE_PREFIX:-localhost/impact-instrumentation}"
TAG="${TAG:-dev}"
VERSION="${VERSION:-0.0.0-dev}"
export CONTAINER IMAGE_PREFIX TAG VERSION

if [ "$CONTAINER" = podman ]; then
  export KIND_EXPERIMENTAL_PROVIDER=podman
fi

# build_images <group>...: build the images the tests use, in parallel, with a log per image.
#   core     what `make ci` tests: the images under test (python, nodejs), the reporter, the mock, and the application
#            fixtures on every Python and libc and every Node it claims, the returns assistant's Starlette one included
#   e2e      what the kind end-to-end suite deploys: the same without the fixtures it does not use
#   overhead what the overhead measurements need: python, nodejs, the mock, and the workloads of every Python and Node
#   tracers  fixtures that carry a tracer of their own (Datadog, Traceloop)
#   agents   the agent framework fixtures, the largest: for AGENTS_PYTHONS (default "3.12 3.13 3.14") and a LangGraph one
build_images() {
  local root logs name
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  logs="$root/build/logs"
  mkdir -p "$logs"
  local -a jobs=()
  # add_job <name> <Dockerfile> <context> <build args, '+' for a space inside a value> <image>
  add_job() {
    local existing
    for existing in "${jobs[@]}"; do [ "${existing%%|*}" = "$1" ] && return 0; done
    jobs+=("$1|$2|$3|$4|$5")
  }
  local apps=testing/apps/Dockerfile node_apps=testing/apps/nodejs/Dockerfile
  local otel="opentelemetry-distro==0.66b2+opentelemetry-exporter-otlp-proto-http==1.45.2+opentelemetry-instrumentation-fastapi==0.66b2"
  # The agent frameworks (OpenAI Agents SDK, LangChain, LangGraph, MCP); CrewAI and LlamaIndex as bare metadata.
  local agents="--build-arg REQUIREMENTS=requirements-agents.txt --build-arg STUB_DISTRIBUTIONS=crewai-1.15.35+llama_index_core-0.14.25"
  local py
  for group in "$@"; do
    case "$group" in
      core | e2e)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job nodejs nodejs/Dockerfile nodejs "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/nodejs:$TAG"
        add_job reporter reporter/Dockerfile reporter "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/reporter:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        add_job fixture-app-3.12 $apps testing/apps "--build-arg PYTHON=3.12" "$IMAGE_PREFIX/fixture-app:3.12"
        add_job fixture-node-22 $node_apps testing/apps/nodejs "--build-arg NODE=22" "$IMAGE_PREFIX/fixture-node:22"
        if [ "$group" = e2e ]; then
          # the qualification matrix runs in the end-to-end job, on every Node it claims
          for node in 24 26; do
            add_job "fixture-node-$node" $node_apps testing/apps/nodejs "--build-arg NODE=$node" "$IMAGE_PREFIX/fixture-node:$node"
          done
        fi
        add_job fixture-app-otel $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=$otel" "$IMAGE_PREFIX/fixture-app-otel:3.12"
        if [ "$group" = core ]; then
          # The release builds the images for arm64 as well. The reporter is cross-compiled, and a step that only works
          # for the build platform would show only in the release, so it is built here too. The python image has no
          # step that runs for the target, and the payload tests assemble its arm64 payload.
          add_job reporter-arm64 reporter/Dockerfile reporter "--build-arg VERSION=$VERSION --build-context root=. --platform linux/arm64" "$IMAGE_PREFIX/reporter:$TAG-arm64"
          for py in 3.11 3.13 3.14 3.12-alpine; do
            add_job "fixture-app-$py" $apps testing/apps "--build-arg PYTHON=$py" "$IMAGE_PREFIX/fixture-app:$py"
          done
          # The Node.js fixtures of the matrix and the overhead: one image per Node, every library version in each.
          for node in 24 26; do
            add_job "fixture-node-$node" $node_apps testing/apps/nodejs "--build-arg NODE=$node" "$IMAGE_PREFIX/fixture-node:$node"
          done
          # A returns assistant: Starlette, uvicorn and boto3 on the Python it runs, nothing else.
          add_job fixture-app-starlette $apps testing/apps "--build-arg PYTHON=3.13 --build-arg REQUIREMENTS=requirements-starlette.txt" "$IMAGE_PREFIX/fixture-app-starlette:3.13"
        fi
        ;;
      agents)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        for py in ${AGENTS_PYTHONS:-3.12 3.13 3.14}; do
          add_job "fixture-app-agents-$py" $apps testing/apps "--build-arg PYTHON=$py $agents" "$IMAGE_PREFIX/fixture-app-agents:$py"
        done
        # LangGraph without the langchain package.
        add_job fixture-app-langgraph $apps testing/apps "--build-arg PYTHON=3.12 --build-arg REQUIREMENTS=requirements-langgraph.txt" "$IMAGE_PREFIX/fixture-app-langgraph:3.12"
        ;;
      overhead)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job nodejs nodejs/Dockerfile nodejs "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/nodejs:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        for py in 3.11 3.12 3.13 3.14; do
          add_job "fixture-app-$py" $apps testing/apps "--build-arg PYTHON=$py" "$IMAGE_PREFIX/fixture-app:$py"
        done
        for node in 22 24 26; do
          add_job "fixture-node-$node" $node_apps testing/apps/nodejs "--build-arg NODE=$node" "$IMAGE_PREFIX/fixture-node:$node"
        done
        ;;
      tracers)
        add_job fixture-app-ddtrace $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=ddtrace==4.16.1" "$IMAGE_PREFIX/fixture-app-ddtrace:3.12"
        add_job fixture-app-traceloop $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=traceloop-sdk==0.62.4+httpx" "$IMAGE_PREFIX/fixture-app-traceloop:3.12"
        ;;
      *) echo "unknown image group: $group" >&2; return 2 ;;
    esac
  done
  local -a pids=() names=()
  for job in "${jobs[@]}"; do
    IFS='|' read -r name dockerfile context args image <<<"$job"
    # '+' stands for a space inside one build argument value.
    local -a extra=()
    # shellcheck disable=SC2086
    for word in $args; do extra+=("${word//+/ }"); done
    (
      cd "$root"
      "$CONTAINER" build -q -f "$dockerfile" -t "$image" "${extra[@]}" "$context" >"$logs/$name.log" 2>&1
    ) &
    pids+=("$!"); names+=("$name")
  done
  local failed=0 i
  for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
      echo "image build failed: ${names[$i]}" >&2
      tail -30 "$logs/${names[$i]}.log" >&2
      failed=1
    fi
  done
  return "$failed"
}

# save_image <image> <archive>: write a local image to an archive. `podman save` reports every blob it copies, which
# is kept out of the logs unless the save fails.
save_image() {
  local log
  log="$(mktemp)"
  if ! "$CONTAINER" save -o "$2" "$1" >"$log" 2>&1; then
    cat "$log" >&2
    rm -f "$log"
    return 1
  fi
  rm -f "$log"
}

# scan_archive <image archive>: what must hold for an image before it is used or pushed. Run from the repository root.
#   - nothing internal in its file names or its configuration (scripts/check_internal_details.py)
#   - no fixable CRITICAL or HIGH vulnerability (trivy). Unfixed ones cannot be acted on; anything accepted on purpose
#     is named in .trivyignore with the reason. The findings are listed one per line, nothing is printed when clean.
scan_archive() {
  local archive=$1
  python3 scripts/check_internal_details.py --archive "$archive"
  trivy image --input "$archive" --scanners vuln --severity CRITICAL,HIGH --ignore-unfixed --exit-code 1 \
    --no-progress --quiet --ignorefile .trivyignore --format template \
    --template '{{ range . }}{{ range .Vulnerabilities }}{{ .Severity }} {{ .VulnerabilityID }} {{ .PkgName }} {{ .InstalledVersion }} (fixed in {{ .FixedVersion }}) {{ .PrimaryURL }}{{ "\n" }}{{ end }}{{ end }}'
  echo "   $(basename "$archive"): nothing internal in its files, no fixable CRITICAL or HIGH vulnerability"
}

# --- signing ----------------------------------------------------------------------------------------------------------
# What a release pushes is signed by digest with cosign: keyless in the release workflow (the job's own identity, which
# `permissions: id-token: write` gives it), or with a key for a rehearsal against a registry of your own (COSIGN_KEY: a
# key file or a KMS URI; the public transparency log is left alone). The release script refuses to push when it has
# neither, unless it is told to publish unsigned (ALLOW_UNSIGNED=1).

# signing_mode: keyless, key or none
signing_mode() {
  if [ -n "${COSIGN_KEY:-}" ]; then
    echo key
  elif [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
    echo keyless
  else
    echo none
  fi
}

# set_cosign_args: COSIGN_ARGS, the flags the signing mode and the registry add to a cosign command
set_cosign_args() {
  COSIGN_ARGS=(--yes)
  case "$(signing_mode)" in
    key) COSIGN_ARGS+=(--key "$COSIGN_KEY" --use-signing-config=false --tlog-upload=false) ;;
    keyless) ;;
    *) echo "there is no way to sign here: no COSIGN_KEY, and the job has no id-token permission" >&2; return 1 ;;
  esac
  if [ -n "${INSECURE_REGISTRY:-}" ]; then COSIGN_ARGS+=(--allow-insecure-registry); fi
}

# cosign_sign <ref@digest>: a signature on the digest
cosign_sign() {
  set_cosign_args
  cosign sign "${COSIGN_ARGS[@]}" "$1"
}

# sbom <ref@digest> <platform> <file>: the SPDX bill of materials of one platform of a pushed image
sbom() {
  local -a flags=(--image-src remote --platform "$2" --format spdx-json --output "$3" --no-progress --quiet)
  if [ -n "${INSECURE_REGISTRY:-}" ]; then flags+=(--insecure); fi
  trivy image "${flags[@]}" "$1"
}

# cosign_attest_sbom <ref@digest> <SPDX JSON file>: the bill of materials, signed, attached to the digest
cosign_attest_sbom() {
  set_cosign_args
  cosign attest "${COSIGN_ARGS[@]}" --type spdxjson --predicate "$2" "$1"
}

# sign_release <publish root> <out dir> <platform>...: sign what a release pushed, by digest, and attach a signed SPDX
# bill of materials of each platform to each image. Reads the digests release.sh wrote: <out>/<key>.digest for python,
# nodejs and reporter, and <out>/chart.digest. The images are under the names scripts/names.sh gives them.
sign_release() {
  local root=$1 out=$2 name ref platform file
  shift 2
  for name in python nodejs reporter; do
    ref="$root/$(published_image "$name")@$(cat "$out/$name.digest")"
    cosign_sign "$ref"
    for platform in "$@"; do
      file="$out/$name-${platform#linux/}.spdx.json"
      sbom "$ref" "$platform" "$file"
      cosign_attest_sbom "$ref" "$file"
    done
  done
  cosign_sign "$root/charts/$CHART_NAME@$(cat "$out/chart.digest")"
}
