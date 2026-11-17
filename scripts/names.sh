#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The names a release publishes under, in one place. Sourced by the release, the signing and the verification scripts.
# values.yaml, the chart README and SECURITY.md repeat them; chart/tests/test_release.py keeps the copies in step.
#
#   chart    oci://<root>/charts/impact-ai
#   images   <root>/autoinstrumentation-python, <root>/autoinstrumentation-nodejs, <root>/autoinstrumentation-java,
#            <root>/kubernetes-reporter
#
# <root> is the registry and the owner, at the root of the organisation: ghcr.io/impact-ai. A registry reference names the
# repository the release runs in, with the owner's capitals (ghcr.io/Impact-AI/impact-ai-autoinstrumentation, which is what GitHub's
# workflows call ghcr.io/${{ github.repository }}), and an image reference has to be lowercase. So the root is the registry
# host and the lowercased owner of REGISTRY, unless PUBLISH_ROOT says otherwise.
# shellcheck shell=bash

CHART_NAME=impact-ai

# The repository releases are made in: its release workflow runs make release, on a tree that scripts/publish-public.sh put
# there. release.sh refuses to run anywhere else (GITHUB_REPOSITORY, compared without regard to case).
RELEASE_REPOSITORY=Impact-AI/impact-ai-autoinstrumentation

# published_image <key>: the name an image is published under, for the key it has in the values of the chart
published_image() {
  case "$1" in
    python) echo autoinstrumentation-python ;;
    nodejs) echo autoinstrumentation-nodejs ;;
    java) echo autoinstrumentation-java ;;
    reporter) echo kubernetes-reporter ;;
    *) echo "unknown image: $1" >&2; return 1 ;;
  esac
}

# publish_root <registry>: the host and the lowercased owner of the registry reference; PUBLISH_ROOT, which has to be
# lowercase, replaces it
publish_root() {
  local registry=$1 rest owner
  if [ -n "${PUBLISH_ROOT:-}" ]; then
    if [ "$PUBLISH_ROOT" != "${PUBLISH_ROOT,,}" ]; then
      echo "PUBLISH_ROOT $PUBLISH_ROOT has capitals: an image reference has to be lowercase" >&2
      return 1
    fi
    echo "${PUBLISH_ROOT%/}"
    return 0
  fi
  case "$registry" in
    */*) ;;
    *) echo "REGISTRY $registry has no owner: expected host/owner[/repository], for example ghcr.io/Impact-AI/impact-ai-autoinstrumentation" >&2; return 1 ;;
  esac
  rest="${registry#*/}"
  owner="${rest%%/*}"
  [ -n "$owner" ] || { echo "REGISTRY $registry has no owner" >&2; return 1; }
  echo "${registry%%/*}/${owner,,}"
}
