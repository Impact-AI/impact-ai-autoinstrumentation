#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The first gate of a release, run in a checkout: the tagged commit has to be on main, where only a merged pull request
# puts it. A tag pushed before its release's pull request was merged publishes nothing; once it is merged, run the
# workflow again.
#
#   scripts/release-reviewed.sh          the commit is GITHUB_SHA (else HEAD), main is RELEASE_MAIN (else origin/main)
#
# Needs main's history: actions/checkout with fetch-depth: 0.
set -euo pipefail

commit="${GITHUB_SHA:-$(git rev-parse HEAD)}"
main="${RELEASE_MAIN:-origin/main}"
if ! git rev-parse --verify --quiet "$main^{commit}" >/dev/null; then
  echo "release-reviewed: $main is not here: check out with the history of main (fetch-depth: 0)" >&2
  exit 1
fi
if git merge-base --is-ancestor "$commit" "$main"; then
  echo "$commit is on $main: its pull request was reviewed and merged"
else
  echo "release-reviewed: $commit is not on $main, so it was not merged: merge the release's pull request, then run" \
    "the release workflow again" >&2
  exit 1
fi
