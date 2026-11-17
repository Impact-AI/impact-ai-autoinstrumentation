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
#            fixtures on every Python and libc and every Node it claims, the returns assistant's Starlette one included.
#            It is core-python and core-nodejs, the halves `make ci-python` and `make ci-nodejs` build
#   e2e      what the end-to-end suite runs on the cluster and in the Node.js matrix: the same without the fixtures it does
#            not use
#   kind     what the kind cluster runs, and nothing else: the archives `scripts/e2e.sh images` leaves for the shards
#   overhead what the overhead measurements need: python, nodejs, the mock, the workloads of every Python and Node, and
#            those of the libraries with a workload of their own (python/tests/overhead/combos.py)
#   tracers  fixtures that carry a tracer of their own (Datadog, Traceloop)
#   agents   the agent framework fixtures, the largest: for AGENTS_PYTHONS (default "3.12 3.13 3.14") and a LangGraph one
#   cells    what the cells of the Python qualification run in, for the libraries in CELL_LIBRARIES (default all of them):
#            the fixtures scripts/qualify_python.py names, on the Pythons the selection names
#   cell-fixtures  the same fixtures without the images under test, only those not built yet
#   newest   the same fixtures with their libraries upgraded to the newest releases, for the weekly run
#   langsmith  the fixtures that trace with LangSmith (Python and Node.js), and the images under test beside them
#   java     what `make test-java` tests: the Java image, the mock, and the Java fixtures (java_image_jobs)
#   lambda   what `make test-lambda` needs besides AWS's Lambda images: the mock (model provider and ingest)
#   dotnet   what `make test-dotnet` runs: the .NET proofs' payload (and its amd64 and arm64 builds, whose files the proofs read),
#            the mock, and the .NET fixture on glibc and musl (dotnet_image_jobs)
# cell_image <kind> <python>: the fixture image a cell of that kind runs in (scripts/qualify_python.py fixtures)
cell_image() {
  case "$1" in
    app) echo "$IMAGE_PREFIX/fixture-app:$2" ;;
    *) echo "$IMAGE_PREFIX/fixture-app-$1:$2" ;;
  esac
}

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
  local ddtrace="--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=ddtrace==4.16.1"
  local py node
  # core is the two halves CI builds on runners of their own (`make ci-python`, `make ci-nodejs`)
  local -a groups=()
  for group in "$@"; do
    if [ "$group" = core ]; then groups+=(core-python core-nodejs); else groups+=("$group"); fi
  done
  for group in "${groups[@]}"; do
    case "$group" in
      core-python)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job reporter reporter/Dockerfile reporter "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/reporter:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        add_job fixture-app-3.12 $apps testing/apps "--build-arg PYTHON=3.12" "$IMAGE_PREFIX/fixture-app:3.12"
        add_job fixture-app-otel $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=$otel" "$IMAGE_PREFIX/fixture-app-otel:3.12"
        # The release builds the images for arm64 as well. The reporter is cross-compiled, and a step that only works
        # for the build platform would show only in the release, so it is built here too. The python image has no
        # step that runs for the target, and the payload tests assemble its arm64 payload.
        add_job reporter-arm64 reporter/Dockerfile reporter "--build-arg VERSION=$VERSION --build-context root=. --platform linux/arm64" "$IMAGE_PREFIX/reporter:$TAG-arm64"
        for py in 3.11 3.13 3.14 3.12-alpine; do
          add_job "fixture-app-$py" $apps testing/apps "--build-arg PYTHON=$py" "$IMAGE_PREFIX/fixture-app:$py"
        done
        # A returns assistant: Starlette, uvicorn and boto3 on the Python it runs, nothing else.
        add_job fixture-app-starlette $apps testing/apps "--build-arg PYTHON=3.13 --build-arg REQUIREMENTS=requirements-starlette.txt" "$IMAGE_PREFIX/fixture-app-starlette:3.13"
        ;;
      core-nodejs)
        add_job nodejs nodejs/Dockerfile nodejs "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/nodejs:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        # The Node.js fixtures of the matrix and the overhead: one image per Node, every library version in each.
        for node in 22 24 26; do
          add_job "fixture-node-$node" $node_apps testing/apps/nodejs "--build-arg NODE=$node" "$IMAGE_PREFIX/fixture-node:$node"
        done
        ;;
      e2e | kind)
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
        else
          # kind: exactly the images the cluster runs (scripts/e2e.sh loads them), the tracers group's Datadog one included
          add_job fixture-app-ddtrace $apps testing/apps "$ddtrace" "$IMAGE_PREFIX/fixture-app-ddtrace:3.12"
        fi
        add_job fixture-app-otel $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=$otel" "$IMAGE_PREFIX/fixture-app-otel:3.12"
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
        # The workload of each library that joined with cells of its own (python/tests/overhead/combos.py).
        add_job fixture-app-google-genai-3.12 $apps testing/apps "--build-arg PYTHON=3.12 --build-arg REQUIREMENTS=requirements-google-genai.txt" "$IMAGE_PREFIX/fixture-app-google-genai:3.12"
        for node in 22 24 26; do
          add_job "fixture-node-$node" $node_apps testing/apps/nodejs "--build-arg NODE=$node" "$IMAGE_PREFIX/fixture-node:$node"
        done
        ;;
      cells | cell-fixtures)
        if [ "$group" = cells ]; then
          add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
          add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        fi
        local kind cell_fixtures
        # shellcheck disable=SC2086 # CELL_LIBRARIES is a list of names
        cell_fixtures="$(cd "$root" && python3 scripts/qualify_python.py fixtures ${CELL_LIBRARIES:-})"
        while read -r kind py; do
          [ -n "$kind" ] || continue
          # cell-fixtures: only the fixtures not already there (the end-to-end job builds them after its other suites'
          # images, once the cluster and the build cache are gone)
          if [ "$group" = cell-fixtures ] && "$CONTAINER" image inspect "$(cell_image "$kind" "$py")" >/dev/null 2>&1; then
            continue
          fi
          case "$kind" in
            app) add_job "fixture-app-$py" $apps testing/apps "--build-arg PYTHON=$py" "$IMAGE_PREFIX/fixture-app:$py" ;;
            # the same image as the agents group's, so that whichever group adds it first builds the same thing
            agents) add_job "fixture-app-agents-$py" $apps testing/apps "--build-arg PYTHON=$py $agents" "$IMAGE_PREFIX/fixture-app-agents:$py" ;;
            *) add_job "fixture-app-$kind-$py" $apps testing/apps "--build-arg PYTHON=$py --build-arg REQUIREMENTS=requirements-$kind.txt" "$IMAGE_PREFIX/fixture-app-$kind:$py" ;;
          esac
        done <<<"$cell_fixtures"
        ;;
      newest)
        # the cells' fixtures with their libraries upgraded to the newest releases (scripts/python-cells.sh newest)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        local kind upgrade base
        for kind in $(cd "$root" && python3 scripts/qualify_python.py fixtures --newest | cut -d' ' -f1 | sort -u); do
          upgrade="$(cd "$root" && python3 scripts/qualify_python.py upgrades "$kind")"
          case "$kind" in
            app) base="" ;;
            agents) base="$agents" ;;
            *) base="--build-arg REQUIREMENTS=requirements-$kind.txt" ;;
          esac
          for py in 3.11 3.12 3.13 3.14; do
            add_job "fixture-app-newest-$kind-$py" $apps testing/apps "--build-arg PYTHON=$py $base --build-arg UPGRADE=${upgrade// /+}" "$IMAGE_PREFIX/fixture-app-newest-$kind:$py"
          done
        done
        ;;
      langsmith)
        add_job python python/Dockerfile python "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/python:$TAG"
        add_job nodejs nodejs/Dockerfile nodejs "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/nodejs:$TAG"
        add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
        add_job fixture-app-langsmith $apps testing/apps "--build-arg PYTHON=3.12 --build-arg REQUIREMENTS=requirements-langsmith.txt" "$IMAGE_PREFIX/fixture-app-langsmith:3.12"
        add_job fixture-node-22 $node_apps testing/apps/nodejs "--build-arg NODE=22" "$IMAGE_PREFIX/fixture-node:22"
        ;;
      tracers)
        add_job fixture-app-ddtrace $apps testing/apps "$ddtrace" "$IMAGE_PREFIX/fixture-app-ddtrace:3.12"
        add_job fixture-app-traceloop $apps testing/apps "--build-arg PYTHON=3.12 --build-arg EXTRA_PACKAGES=traceloop-sdk==0.62.4+httpx" "$IMAGE_PREFIX/fixture-app-traceloop:3.12"
        ;;
      java) java_image_jobs ;;
      lambda) add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG" ;;
      dotnet) dotnet_image_jobs ;;
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

# java_image_jobs: the jobs of the java group, for build_images. The Java image, the mock, and a fixture workload for
# every Java the image is built for (17, 21, 25) and one outside it (11).
java_image_jobs() {
  local java
  add_job java java/Dockerfile java "--build-arg VERSION=$VERSION --build-context root=." "$IMAGE_PREFIX/java:$TAG"
  add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
  for java in 11 17 21 25; do
    add_job "fixture-java-$java" testing/apps/java/Dockerfile testing/apps/java "--build-arg JAVA=$java" "$IMAGE_PREFIX/fixture-java:$java"
  done
}

# dotnet_image_jobs: the jobs of the dotnet group, for build_images. The proofs' payload for this machine's architecture,
# and for amd64 and arm64 by name (nothing in it runs for the target, so it builds without emulation; the proofs read
# its files), with the .NET versions the kit is proved on (DOTNET_VERSIONS, default 10.0) in its startup hook; the mock;
# the fixture workload on each C library for every .NET in DOTNET_VERSIONS; and a service on .NET 8, which the kit is
# not proved on.
dotnet_image_jobs() {
  local dotnet libc proved
  proved="${DOTNET_VERSIONS:-10.0}"
  proved="${proved// /+}" # one build argument: build_images turns + back into spaces
  add_job dotnet dotnet/Dockerfile dotnet "--build-arg PROVED=$proved" "$IMAGE_PREFIX/dotnet:$TAG"
  add_job dotnet-x64 dotnet/Dockerfile dotnet "--platform linux/amd64 --build-arg PROVED=$proved" "$IMAGE_PREFIX/dotnet:$TAG-x64"
  add_job dotnet-arm64 dotnet/Dockerfile dotnet "--platform linux/arm64 --build-arg PROVED=$proved" "$IMAGE_PREFIX/dotnet:$TAG-arm64"
  add_job mock testing/mock/Dockerfile testing/mock "" "$IMAGE_PREFIX/mock:$TAG"
  for dotnet in ${DOTNET_VERSIONS:-10.0}; do
    for libc in glibc musl; do
      add_job "fixture-dotnet-$dotnet-$libc" testing/apps/dotnet/Dockerfile testing/apps/dotnet \
        "--build-arg DOTNET=$dotnet --build-arg LIBC=$libc" "$IMAGE_PREFIX/fixture-dotnet:$dotnet-$libc"
    done
  done
  add_job fixture-dotnet-older testing/apps/dotnet/Dockerfile testing/apps/dotnet "--target older" "$IMAGE_PREFIX/fixture-dotnet:older-8.0"
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

# release_images: the images a release builds, scans, pushes, signs and stamps into the chart, by key. The Java image only
# with RELEASE_JAVA=1: the release workflow records the build provenance of what release.sh pushed, and verify-release.sh
# requires it of every image in the chart, so the workflow sets RELEASE_JAVA=1 in the same change that attests the Java
# image. Without it the chart's images.java keeps its empty digest, and the release publishes nothing of Java.
release_images() {
  if [ "${RELEASE_JAVA:-}" = 1 ]; then
    echo python nodejs java reporter
  else
    echo python nodejs reporter
  fi
}

# sign_release <publish root> <out dir> <platform>...: sign what a release pushed, by digest, and attach a signed SPDX
# bill of materials of each platform to each image. Reads the digests release.sh wrote: <out>/<key>.digest for each of
# release_images, and <out>/chart.digest. The images are under the names scripts/names.sh gives them.
sign_release() {
  local root=$1 out=$2 name ref platform file
  shift 2
  for name in $(release_images); do
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
