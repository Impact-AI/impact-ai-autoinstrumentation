#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Publish a release: multi-architecture images (linux/amd64 and linux/arm64) with provenance and an SBOM, then the chart
# with the images' digests written into it, and the overhead of the injected workloads measured by this very build.
#
#   REGISTRY=ghcr.io/Impact-AI/impact-ai-autoinstrumentation VERSION=v0.3.0 REGISTRY_USER=... REGISTRY_TOKEN=... make release
#
# REGISTRY is the repository the release runs in (the workflow's ghcr.io/${{ github.repository }}, with the owner's
# capitals): the host to log in to, and where the publish root comes from. The release pushes at the root of the
# organisation, the registry host and the lowercased owner, ghcr.io/impact-ai (PUBLISH_ROOT replaces it; scripts/names.sh):
#   $PUBLISH_ROOT/autoinstrumentation-python:<version>, $PUBLISH_ROOT/autoinstrumentation-nodejs:<version>,
#   $PUBLISH_ROOT/autoinstrumentation-java:<version>, $PUBLISH_ROOT/kubernetes-reporter:<version> and
#   oci://$PUBLISH_ROOT/charts/impact-ai:<version>.
# It writes the digests to build/release/<key>.digest and the names to build/release/<key>.name (python, nodejs, java,
# reporter and chart), which the workflow reads for the build provenance. The Java image is released only with
# RELEASE_JAVA=1, which the workflow sets once it records the Java image's provenance too (release_images in
# scripts/lib.sh); it is then published and signed, and its digest is written into the chart's values, but the chart
# does not inject it yet (images.java has no section of the Instrumentation), and the overhead measurement does not
# cover it yet.
#
# Nothing is pushed until every check has passed:
#   1. nothing internal in the source and the chart (scripts/check_internal_details.py), and the version's dated section
#      of CHANGELOG.md, which the GitHub release and the chart's Artifact Hub annotations are made from
#   2. each image, built for each platform: nothing internal in its files, no fixable CRITICAL or HIGH vulnerability
#      (scan_archive in scripts/lib.sh; accepted findings are named in .trivyignore)
#   3. the overhead of every runtime and server the images claim, injected and not, within the overhead budget (+1.5 ms of CPU
#      per model call, +40 MiB, +2 s to start; scripts/overhead.sh full)
#   4. every cell of the Python qualification says what python/qualification.json says it did (scripts/python-cells.sh
#      all): the image traces a library only where a cell proves it, so a cell that changed stops the release
#   5. a way to sign: the release workflow's identity (keyless cosign, `permissions: id-token: write`), or COSIGN_KEY
# then the images are pushed, the chart is stamped with their digests and the measured overhead (OVERHEAD.md and an
# annotation of Chart.yaml), checked again, and pushed. Then the images and the chart are signed by digest, a signed
# SPDX bill of materials is attached to each image for each platform (cosign attest), and the signatures are verified the
# way a customer does (scripts/verify-release.sh). The workflow adds the build provenance of GitHub's artifact
# attestations afterwards, from the digests in build/release/*.digest.
#
# A release is made in one place: the release workflow of the public repository, Impact-AI/impact-ai-autoinstrumentation, on a tree that
# scripts/publish-public.sh put there. A tag in the development repository starts the same workflow, and this script
# would push the images and the chart under that organisation's name, so it refuses, before it builds anything, to run
# where GITHUB_REPOSITORY is not the public repository. RELEASE_REHEARSAL=1 lifts that for a rehearsal against a registry of
# your own, and so does INSECURE_REGISTRY (below), which is a registry on your machine.
#
# CONTAINER=docker (CI): buildx with the docker-container driver. The builds that are scanned are exported as archives;
# the push is the same build again, from the builder's cache, with provenance and an SBOM attached.
# CONTAINER=podman: the manifest list is made of the very images that were scanned. It cannot attach attestations, so
# it refuses unless ALLOW_UNATTESTED=1 (a rehearsal against a local registry: INSECURE_REGISTRY=1).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"
if [ "${RELEASE_REHEARSAL:-}" != 1 ] && [ -z "${INSECURE_REGISTRY:-}" ] && [ "${GITHUB_REPOSITORY,,}" != "${RELEASE_REPOSITORY,,}" ]; then
  echo "refusing to release from ${GITHUB_REPOSITORY:-outside GitHub Actions}: a release is made in $RELEASE_REPOSITORY, by its release workflow, on a tree that scripts/publish-public.sh published there (the script commits it and tags it, and the tag starts the workflow). To rehearse a release against a registry of your own, set RELEASE_REHEARSAL=1." >&2
  exit 1
fi

: "${REGISTRY:?REGISTRY is required, for example ghcr.io/Impact-AI/impact-ai-autoinstrumentation}"
: "${VERSION:?VERSION is required, for example v0.3.0}"
: "${REGISTRY_USER:?REGISTRY_USER is required}"
: "${REGISTRY_TOKEN:?REGISTRY_TOKEN is required}"

number="${VERSION#v}"
host="${REGISTRY%%/*}"
PUBLISH_ROOT="$(publish_root "$REGISTRY")"
platforms=(linux/amd64 linux/arm64)
platform_list="$(IFS=,; echo "${platforms[*]}")"
out="build/release"
scan_dir="$out/scan"
rm -rf "$out"
mkdir -p "$scan_dir"
export VERSION_NUMBER="$number"

if [ "$CONTAINER" = podman ] && [ -z "${ALLOW_UNATTESTED:-}" ]; then
  echo "refusing to release with podman: it cannot attach provenance or an SBOM. Use CONTAINER=docker, or set ALLOW_UNATTESTED=1 for a rehearsal." >&2
  exit 1
fi

signing="$(signing_mode)"
if [ "$signing" = none ] && [ -z "${ALLOW_UNSIGNED:-}" ]; then
  echo "refusing to release without a way to sign. In the release workflow the job needs 'permissions: id-token: write' (keyless signing with cosign). For a rehearsal against a registry of your own set COSIGN_KEY and COSIGN_PUBLIC_KEY, or ALLOW_UNSIGNED=1 to publish unsigned." >&2
  exit 1
fi
if [ "$signing" = key ] && [ -z "${COSIGN_PUBLIC_KEY:-}" ]; then
  echo "COSIGN_KEY is set: set COSIGN_PUBLIC_KEY too, the signatures are verified before the release is declared done." >&2
  exit 1
fi

tls=()
plain=()
if [ -n "${INSECURE_REGISTRY:-}" ]; then
  tls=(--tls-verify=false)
  plain=(--plain-http)
fi

# What podman keeps in its store for the push: the images that were scanned, and the manifest lists made of them.
built=()
lists=()
cleanup() {
  local name
  for name in ${lists[@]+"${lists[@]}"}; do "$CONTAINER" manifest rm "$name" >/dev/null 2>&1 || true; done
  for name in ${built[@]+"${built[@]}"}; do "$CONTAINER" rmi -f "$name" >/dev/null 2>&1 || true; done
  rm -rf "$scan_dir"
}
trap cleanup EXIT

echo "== check SECURITY.md has its contact and its response time"
if grep -n 'set by owner' SECURITY.md >&2; then
  echo "SECURITY.md still has a placeholder: write the contact and the response time before a release" >&2
  exit 1
fi

echo "== check the source and the chart for internal details"
python3 scripts/check_internal_details.py --source --chart

# A release says what changed: its section of CHANGELOG.md, which the chart's Artifact Hub annotations and the GitHub
# release are made from (a rehearsal may do without one).
if [ "${RELEASE_REHEARSAL:-}" != 1 ]; then
  echo "== check CHANGELOG.md has a dated section for $number"
  python3 scripts/release_notes.py "$number" --changes >/dev/null ||
    { echo "CHANGELOG.md has no dated section for $number (above): a release says what changed" >&2; exit 1; }
fi

if [ "$CONTAINER" = docker ]; then
  docker buildx inspect impact-release >/dev/null 2>&1 || docker buildx create --name impact-release --driver docker-container >/dev/null
fi

echo "== log in to $host"
printf '%s' "$REGISTRY_TOKEN" | "$CONTAINER" login "$host" --username "$REGISTRY_USER" --password-stdin "${tls[@]}"
printf '%s' "$REGISTRY_TOKEN" | helm registry login "$host" --username "$REGISTRY_USER" --password-stdin "${plain[@]}"

# build_and_scan <name> <dockerfile> <context>: the image for each platform, and the checks on each of them
build_and_scan() {
  local name=$1 dockerfile=$2 context=$3 platform arch archive image
  for platform in "${platforms[@]}"; do
    arch="${platform#linux/}"
    archive="$scan_dir/$name-$arch.tar"
    echo "== build $name for $platform"
    if [ "$CONTAINER" = docker ]; then
      docker buildx build --builder impact-release --platform "$platform" --build-context root=. \
        --build-arg "VERSION=$number" -f "$dockerfile" --output "type=docker,dest=$archive" "$context"
    else
      image="localhost/impact-release/$name:$number-$arch"
      built+=("$image")
      "$CONTAINER" build --platform "$platform" --build-context root=. \
        --build-arg "VERSION=$number" -f "$dockerfile" -t "$image" "$context"
      save_image "$image" "$archive"
    fi
    echo "== scan $name for $platform"
    scan_archive "$archive"
    rm -f "$archive"
  done
}

# push_image <name> <dockerfile> <context>: push the scanned image for both platforms, write its digest to
# $out/<name>.digest
push_image() {
  local name=$1 dockerfile=$2 context=$3 ref="$PUBLISH_ROOT/$(published_image "$1"):$number" platform
  echo "== push $ref ($platform_list)"
  if [ "$CONTAINER" = docker ]; then
    docker buildx build --builder impact-release --platform "$platform_list" --build-context root=. \
      --provenance=mode=max --sbom=true \
      --build-arg "VERSION=$number" -f "$dockerfile" -t "$ref" --metadata-file "$out/$name.json" --push "$context"
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['containerimage.digest'])" "$out/$name.json" >"$out/$name.digest"
  else
    "$CONTAINER" manifest rm "$ref" >/dev/null 2>&1 || true
    "$CONTAINER" manifest create "$ref" >/dev/null
    lists+=("$ref")
    for platform in "${platforms[@]}"; do
      "$CONTAINER" manifest add "$ref" "containers-storage:localhost/impact-release/$name:$number-${platform#linux/}" >/dev/null
    done
    "$CONTAINER" manifest push --all "${tls[@]}" --digestfile "$out/$name.digest" "$ref" "docker://$ref"
  fi
  echo "$PUBLISH_ROOT/$(published_image "$name")" >"$out/$name.name"
  echo "   $name $(cat "$out/$name.digest")"
}

# python nodejs reporter, and java with RELEASE_JAVA=1; each is built from <key>/Dockerfile in <key>/
read -r -a images <<<"$(release_images)"
for name in "${images[@]}"; do
  build_and_scan "$name" "$name/Dockerfile" "$name"
done

# The images the overhead is measured on are built from the same Dockerfiles at the same version; nothing is pushed if a
# workload is over its budget (the report stays in $out/overhead).
echo "== measure the overhead of the injected workloads"
VERSION="$number" scripts/overhead.sh full "$out/overhead"

echo "== run every cell of the Python qualification: a cell that no longer says what it did stops the release"
VERSION="$number" scripts/python-cells.sh all

for name in "${images[@]}"; do
  push_image "$name" "$name/Dockerfile" "$name"
done

echo "== stamp and package the chart"
rm -rf build/chart
mkdir -p build/chart
cp -R "chart/$CHART_NAME" "build/chart/$CHART_NAME"
stamp=()
for name in "${images[@]}"; do
  stamp+=("--$name" "$(cat "$out/$name.digest")" --repository "$name=$PUBLISH_ROOT/$(published_image "$name")")
done
python3 scripts/stamp_chart.py "build/chart/$CHART_NAME" "$number" "${stamp[@]}" --overhead "$out/overhead/results.json" \
  --changelog CHANGELOG.md
helm lint "build/chart/$CHART_NAME" -f "build/chart/$CHART_NAME/ci/values.yaml" >/dev/null
python3 scripts/check_internal_details.py --chart-dir "build/chart/$CHART_NAME"
helm package "build/chart/$CHART_NAME" --destination "$out"

echo "== push the chart to oci://$PUBLISH_ROOT/charts"
# helm push says "Pushed:" and "Digest:" on standard error (and on standard output in other versions): both are kept.
helm push "$out/$CHART_NAME-$number.tgz" "oci://$PUBLISH_ROOT/charts" ${plain[@]+"${plain[@]}"} 2>&1 | tee "$out/chart-push.txt"
sed -n 's/^Digest: //p' "$out/chart-push.txt" >"$out/chart.digest"
[ -s "$out/chart.digest" ] || { echo "helm push printed no digest for the chart" >&2; exit 1; }
echo "$PUBLISH_ROOT/charts/$CHART_NAME" >"$out/chart.name"
echo "   chart    $(cat "$out/chart.digest")"

if [ "$signing" != none ]; then
  echo "== sign (cosign, $signing) and attach a signed SPDX bill of materials to each image"
  sign_release "$PUBLISH_ROOT" "$out" "${platforms[@]}"

  echo "== verify the signatures the way a customer does"
  VERIFY_GITHUB_ATTESTATION=0 REGISTRY="$REGISTRY" PUBLISH_ROOT="$PUBLISH_ROOT" VERSION="$VERSION" scripts/verify-release.sh
else
  echo "== NOT SIGNED: ALLOW_UNSIGNED is set" >&2
fi

echo "== released $VERSION"
for name in "${images[@]}"; do
  printf '   %-8s %s\n' "$name" "$PUBLISH_ROOT/$(published_image "$name"):$number@$(cat "$out/$name.digest")"
done
echo "   chart    oci://$PUBLISH_ROOT/charts/$CHART_NAME --version $number@$(cat "$out/chart.digest")"
