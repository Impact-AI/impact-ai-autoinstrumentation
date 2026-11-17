# Releasing

How a version of the kit is published, and the settings of this repository that the release relies on. For the
maintainers at Impact AI; anyone can follow what each step checks.

## Steps

1. **The changelog.** In the development repository, the version's section of [CHANGELOG.md](CHANGELOG.md) is dated,
   `## [X.Y.Z] - YYYY-MM-DD`, under an empty `## [Unreleased]`. Nothing is released without it: the release workflow's
   build stops first, and the release's notes and the chart's Artifact Hub annotations are made from it.
2. **Propose.** In Impact AI's development repository, anyone on the team runs the workflow `publish` on `main` with
   the version and the step `propose`. It makes the release commit of `main`'s tree (`scripts/publish-public.sh`: the
   public tree, checked for internal details with built-in and any configured extra patterns), pushes it to the branch
   `release/X.Y.Z` here and opens its pull request into `main` as Impact AI's release App, titled `Release X.Y.Z`, with the version's
   changelog section. Nothing of a release runs on anyone's machine. To repair an open proposal, merge the fixes into
   the development repository and run `propose` again with the same version. It appends a checked release commit and
   updates the same PR, without force-pushing. Closed or merged proposals and unexpected outside commits are refused;
   tagged versions are never replaced. After review and merge, `tag` selects the latest merged revision.
3. **Review.** CI runs on the pull request. A maintainer reviews it, approves it and merges it **with a merge commit**,
   which keeps the release commit on `main`. Review the tree for company-specific names the generic checks cannot identify.
4. **Tag.** The same workflow's step `tag` tags the merged release commit `vX.Y.Z` as the release identity and pushes
   the tag, which starts the workflow `release` here. It publishes nothing unless the tagged commit is on `main`
   (`reviewed`); then it runs the end-to-end suite on Kubernetes 1.27, 1.34 and 1.37 (`e2e`); builds, scans, pushes and
   signs the images and the chart, and verifies them as a customer does (`publish`); and publishes the GitHub release
   with its signed files (`github-release`).
5. **Check it** from a clone, the way a customer does: `make verify-release VERSION=vX.Y.Z`.

A final version can follow its release candidate on the same tree: `publish-public.sh X.Y.Z` after `X.Y.Z-rc1` makes
an empty commit on top of the candidate.

## Repository settings the release relies on

An owner makes the release App by hand, then applies the rest once from Impact AI's development repository with
`scripts/setup-public-repository.sh`. The packages are made public in their own settings once the first release has
pushed them.

* **Rulesets.** On `main`: changes through a pull request with one approval and its review threads resolved, approvals
  dismissed by a new push, the check `test` required, no force push, no deletion, and the merge commit as the merge
  method. On the tags `v*`: no update and no deletion, and only the release App creates them, so a release starts from
  the workflow `publish` alone.
* **Actions.** The default `GITHUB_TOKEN` is read-only; actions are pinned to full commit SHAs; workflows of outside
  contributors wait for a maintainer's approval.
* **The release App** (`Impact AI releases`) is installed on this repository alone and may write its contents, pull
  requests and workflows. The development repository's workflow `publish` mints its token for each run, from a key that
  only that workflow on `main` can use.
* **Releases** are immutable once published.
* **Security.** Private vulnerability reporting ([SECURITY.md](SECURITY.md)), secret scanning with push protection,
  Dependabot alerts and security updates. CodeQL runs from its workflow, so its default setup stays off.
* **Packages.** `ghcr.io/impact-ai/autoinstrumentation-python`, `autoinstrumentation-nodejs`, `kubernetes-reporter` and
  `charts/impact-ai` are public and linked to this repository (the images' `org.opencontainers.image.source` label and
  the chart's `sources`).

## Optional extra exclusion patterns

Built-in internal-details checks always run. Setup and publishing need no local exclusion file. A release maintainer
may explicitly supply `DENYLIST=<file>` to setup to store extra `category: regular expression` patterns in the
`public-release` environment's `INTERNAL_DETAILS_DENYLIST` secret. Use only a list intended for company releases;
personal tooling and its files are not company configuration. No file is searched for or imported automatically.
Omitting `DENYLIST` leaves an existing secret unchanged; delete that optional environment secret to stop using it.
A supplied file that is missing or malformed still fails rather than silently dropping its checks.

These optional content patterns do not control Git identity. Release commits and tags always use the company release
identity, and publication uses the release App. Those controls do not depend on an exclusion list. Generic checks
cannot identify every personal name or private repository reference; maintainers review the proposed public tree too.
