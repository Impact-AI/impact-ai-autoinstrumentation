# SPDX-License-Identifier: Apache-2.0

# Impact AI auto-instrumentation: everything CI and releases run. Tool versions come from mise.toml
# (jdx/mise-action in CI).
#
#   make ci        unit tests, chart checks, image builds and image tests, a canary of the overhead: `make ci-checks`,
#                  `make ci-python` and `make ci-nodejs`, which CI runs as jobs of their own (CI job "test", 5 to 8
#                  minutes a part)
#   make e2e       build, create a kind cluster, run e2e/ and the agent, tracer, Node.js matrix and gateway container
#                  tests and the Python qualification's cells of the libraries the change touches, delete the cluster
#                  (about 25 minutes). CI runs it in parts, each under ten minutes, once a day on main and on a
#                  release before it is published (CI job "e2e"): `make e2e-images`, then the suite in shards on
#                  clusters of their own (`make e2e-kind`), `make e2e-python`, `make e2e-nodejs` and
#                  `make test-python-cells`
#                  E2E_NODE_IMAGE=<kindest/node image> runs the Kubernetes part on another version (CI job "e2e-oldest",
#                  Kubernetes 1.27, the same shards)
#   make ci-jobs   which of those jobs a change needs, and why: scripts/ci_jobs.py, which CI runs first
#   make overhead  the overhead of every runtime and server the images claim, against the overhead budget
#   make release   multi-architecture images and the chart, pushed to the publish root of $(REGISTRY) (its host and the
#                  lowercased owner: ghcr.io/impact-ai) and signed (cosign); measures the overhead and runs every cell of
#                  the Python qualification first  (release workflow; it refuses to run outside the public repository
#                  unless RELEASE_REHEARSAL=1)
#   make verify-release   a published release, checked the way a customer does: signatures, SBOMs, provenance
#
# CONTAINER=docker (CI) or CONTAINER=podman (a workstation without Docker); the default is docker when it exists.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
export CONTAINER

UV := uv run --project python --frozen

.PHONY: lambda-layers test-lambda lambda-timings
.PHONY: help ci ci-checks ci-python ci-nodejs ci-jobs lint lint-python lint-reporter lint-chart lint-license lint-internal lint-workflows licenses test-python test-reporter test-chart test-nodejs-unit images images-python images-nodejs images-java test-image test-nodejs test-nodejs-matrix test-java test-agents test-python-cells test-python-matrix test-python-newest scan overhead overhead-canary overhead-covering qualify qualify-python e2e e2e-images e2e-kind e2e-python e2e-nodejs release publish-ci release-reviewed check-dco release-files github-release verify-release lock clean test-langsmith test-datadog test-newrelic

help: ## list the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | column -t -s $$'\t'

ci: ci-checks ci-python ci-nodejs ## unit tests, chart checks, image builds, the vulnerability scan, image tests and an overhead canary: the three parts below

ci-checks: lint test-python test-reporter test-chart test-nodejs-unit images-java test-java ## part of `make ci`: style and static checks, unit tests, chart checks, and the Java image built, scanned and tested
	scripts/scan-images.sh java

ci-python: test-lambda images-python test-image ## part of `make ci`: the Lambda layer in AWS's images for Python (first: it removes the images it pulls), then the python image, the reporter and the Python fixtures built, scanned and tested, and the overhead canary's Python workload
	scripts/scan-images.sh python reporter
	OVERHEAD_BUILD=0 OVERHEAD_COMBOS=python3.12-uvicorn scripts/overhead.sh canary build/overhead-canary-python

ci-nodejs: images-nodejs test-nodejs test-dotnet ## part of `make ci`: the nodejs image and the Node.js fixtures built, scanned and tested, and the overhead canary's Node.js workload; and the .NET proofs (`make test-dotnet`), in the shortest part until they have a part of their own
	scripts/scan-images.sh nodejs
	OVERHEAD_BUILD=0 OVERHEAD_COMBOS=node22-express scripts/overhead.sh canary build/overhead-canary-nodejs

ci-jobs: ## the CI jobs this change needs and why: the end-to-end job only for what it builds, deploys, runs or reads (scripts/ci_jobs.py; against the merge base with origin/main, or CI_JOBS_BASE)
	python3 scripts/ci_jobs.py

lint: lint-python lint-reporter lint-chart lint-license lint-internal lint-workflows ## style and static checks

lint-python: ## ruff (python, the Lambda layer, nodejs tests, chart tests, e2e, test fixtures)
	$(UV) ruff check --config python/pyproject.toml python lambda nodejs java dotnet chart/tests e2e testing scripts
	$(UV) ruff format --check --config python/pyproject.toml python lambda nodejs java dotnet chart/tests e2e testing scripts

lint-reporter: ## gofmt and go vet
	cd reporter && test -z "$$(gofmt -l .)" && go vet ./...

lint-chart: ## helm lint, the vendored Operator chart is the pinned one, no HIGH or CRITICAL misconfiguration in the render (trivy), scripts parse
	helm lint chart/impact-ai -f chart/impact-ai/ci/values.yaml
	python3 scripts/vendor_operator_chart.py --check
	scripts/scan-chart.sh
	for script in scripts/*.sh; do bash -n "$$script"; done
	sh -n scripts/uninstall-cluster-kit.sh

lint-license: ## SPDX header on every source file of ours, the chart's copies of LICENSE and NOTICE
	python3 scripts/check_spdx.py
	cmp LICENSE chart/impact-ai/LICENSE
	cmp NOTICE chart/impact-ai/NOTICE

ZIZMOR_VERSION ?= 1.32.0
ACTIONLINT_PY_VERSION ?= 1.7.12.25
lint-workflows: ## the workflows: actionlint, and zizmor's security audits (no finding of medium severity or above)
	uv tool run --from actionlint-py==$(ACTIONLINT_PY_VERSION) actionlint
	uv tool run zizmor==$(ZIZMOR_VERSION) --min-severity medium .github/workflows

lint-internal: ## nothing internal (hosts, e-mail addresses, account IDs, private addresses, tokens) in the source or the rendered chart; INTERNAL_DETAILS_DENYLIST=<file> adds a company's own names
	python3 scripts/check_internal_details.py --source --chart

licenses: ## copy LICENSE and NOTICE into the chart (helm packages only the chart directory), put the SPDX header in new files
	cp LICENSE NOTICE chart/impact-ai/
	python3 scripts/check_spdx.py --add

test-python: ## unit tests of the package and the payload for every architecture and libc, the overhead gate and its report
	$(UV) pytest python/tests/unit python/tests/payload python/tests/overhead -m "not overhead" -q -p no:cacheprovider

test-reporter: ## the reporter's tests, with the race detector
	cd reporter && go test -race -count=1 ./...

test-chart: ## render checks of the chart
	$(UV) pytest chart/tests -q -p no:cacheprovider

test-nodejs-unit: ## unit tests of the Node.js package, in a container (the official OpenTelemetry encoder is the oracle of the exporter)
	scripts/test-nodejs-unit.sh

LAMBDA_LAYERS := build/lambda/impact-ai-python-x86_64.zip build/lambda/impact-ai-python-arm64.zip

lambda-layers: ## the Python layer for AWS Lambda from the image's payload, one archive per architecture with its SBOM, and build/lambda/SIZES.md (the archives and the environment a function's change adds, against Lambda's limits); publishes nothing
	python3 lambda/build_layer.py --out build/lambda --version "$${VERSION:-0.0.0-dev}"
	python3 scripts/check_internal_details.py $(addprefix --zip ,$(LAMBDA_LAYERS))
	cat build/lambda/SIZES.md

lambda-timings: lambda-layers ## cold starts and invocations of a function with and without the layer, under the emulator (every Python at 512 and 1,024 MB, 30 cold starts a side; LAMBDA_TIMINGS_ARGS narrows it): build/lambda-timings/LAMBDA-TIMINGS.md. It reports against the proposed budget and does not fail on it. On Lambda itself: scripts/lambda_timings.py aws, with a test account
	scripts/test-lambda.sh timings

test-lambda: lambda-layers ## the layer in AWS's Lambda images for Python 3.11 to 3.14 under their Runtime Interface Emulator, on this machine's architecture: Bedrock spans with the function's resource, fail open, stand-down; then a smoke of the timings (pulls the images and removes them after)
	scripts/test-lambda.sh

images: ## build the images the tests use (CONTAINER=docker|podman)
	scripts/build-images.sh core

images-python: ## the Python half of `make images`: the python image, the reporter (amd64 and arm64), the mock and the Python fixtures
	scripts/build-images.sh core-python

images-nodejs: ## the Node.js half of `make images`: the nodejs image, the mock and a fixture for every Node
	scripts/build-images.sh core-nodejs

images-java: ## build the Java image (its build runs the launcher's and the extension's self tests) and its fixtures on Java 11, 17, 21 and 25
	scripts/build-images.sh java

test-image: ## the Operator's injection reproduced with containers: Python 3.11 to 3.14, uvicorn and gunicorn, stand-down, ingest down, an unproven library version
	$(UV) pytest python/tests/image -m "image and not tracers and not agents" -n 3 -v -p no:cacheprovider --tb=short

test-nodejs: ## the Node.js image: layout, licenses, never throws on Node 16 to 26, spans with content, stand-down, ingest down, ES modules
	$(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m "nodejs and not matrix" -n 3 -v -p no:cacheprovider --tb=short

test-java: ## the Java image: layout, the pinned agent, the scope cut, spans on Java 17, 21 and 25, stand-down beside an APM agent and an OpenTelemetry agent, ingest down, a Java outside the range; needs `make images-java`
	$(UV) pytest -c python/pyproject.toml --rootdir . java/tests/image -m java -n 3 -v -p no:cacheprovider --tb=short

test-nodejs-matrix: ## the qualification matrix against nodejs/qualification.json: Node 22, 24, 26 by CommonJS and ES modules by the SDKs' versions
	$(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m matrix -n 3 -v -p no:cacheprovider --tb=short

test-python-cells: ## the cells of python/qualification.json a change touches: a library's own inputs on every Python, what every cell runs on (the package) on Python 3.12, none for most changes; CELLS=all, or CELLS=openai,langgraph@3.13, chooses them
	scripts/python-cells.sh changed

test-python-matrix: ## every cell of python/qualification.json: Python 3.11 to 3.14 by the AI libraries the image traces and their major versions; fails when a cell no longer says what it did
	scripts/python-cells.sh all

test-python-newest: ## the weekly run: every cell of python/qualification.json on the newest release of each library; fails when one fails its cell, and writes the issue's body to build/qualification-python-newest/SUMMARY.md
	scripts/python-cells.sh newest

qualify-python: ## run every cell of the Python qualification again and rewrite python/qualification.json (its excluded releases are kept)
	scripts/python-cells.sh qualify

qualify: ## run the Node.js matrix (Node 22, 24, 26 by CommonJS and ES modules by the SDKs' versions) again and rewrite nodejs/qualification.json
	rm -rf build/qualification && mkdir -p build/qualification
	QUALIFY_DIR=$(CURDIR)/build/qualification $(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image/test_nodejs_matrix.py -k test_the_cell -n 3 -q -p no:cacheprovider --tb=short
	python3 scripts/qualify_nodejs.py build/qualification nodejs/qualification.json

scan: ## the python, nodejs and reporter images: no fixable CRITICAL or HIGH vulnerability (trivy), nothing internal in their files
	scripts/scan-images.sh

overhead-canary: ## two workloads (Python 3.12 under uvicorn, Node 22), one round each, against the budget; needs `make images`
	OVERHEAD_BUILD=0 scripts/overhead.sh canary

overhead-covering: ## seven workloads, two rounds each: every Python, every Node and both servers (instead of the canary, if `overhead` is not a job of its own)
	OVERHEAD_BUILD=0 scripts/overhead.sh covering

overhead: ## the overhead of every runtime and server, and of each library with a workload of its own, three rounds each, against the overhead budget: build/overhead/OVERHEAD.md
	scripts/overhead.sh full

test-agents: ## the agent frameworks (OpenAI Agents SDK, LangChain, LangGraph) in injected containers; AGENTS_PYTHONS="3.11 3.12 3.13 3.14" for all four
	scripts/build-images.sh agents
	$(UV) pytest python/tests/image -m agents -n 3 -v -p no:cacheprovider --tb=short

test-langsmith: ## LangSmith beside the package, Python and Node.js in containers: its default tracing receives the same runs, its OpenTelemetry mode stands the package down
	scripts/build-images.sh langsmith
	$(UV) pytest python/tests/image -m langsmith -n 4 -v -p no:cacheprovider --tb=short
	$(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m langsmith -n 3 -v -p no:cacheprovider --tb=short

test-newrelic: ## New Relic's Python and Node.js agents at their pinned versions, recorded against the collector double; the relay's decoder must read the canonical interaction from what they sent (about two minutes)
	testing/apps/newrelic_cells.sh

DATADOG_CELLS ?= python,nodejs,java
test-datadog: ## Datadog's tracers (DATADOG_CELLS: python, nodejs, java) against the real Datadog Agent with a placeholder key: it forwards everything to a recording backend, and the relay's decoder reads what it received
	$(UV) pytest testing/apps/datadog/tests -p no:cacheprovider -q
	python3 testing/apps/datadog/cells.py --out build/datadog --cells $(DATADOG_CELLS)
	cd reporter && DATADOG_HARNESS_RECORDINGS=$(CURDIR)/build/datadog go test ./internal/relay/source -run TestDatadogHarness -v

e2e: ## kind end to end (install, names only, coverage, stand-down, fail open, a new key, uninstall), then the tracer, agent, Node.js matrix and gateway container tests and the Python qualification's cells of the libraries the change touches; E2E_NODE_IMAGE=<kindest/node image> for another Kubernetes version
	scripts/e2e.sh

e2e-images: ## part of `make e2e`: build the images the kind cluster runs, and leave their archives in build/images for `make e2e-kind`
	scripts/e2e.sh images

e2e-kind: ## part of `make e2e`: the kind suite on a cluster of its own, from the archives of `make e2e-images`; E2E_TESTS="e2e/test_7_exchange.py ..." runs those modules (CI runs the shards of scripts/ci_jobs.py)
	scripts/e2e.sh kind

e2e-python: ## part of `make e2e`: the tracer, agent framework and AI gateway container tests, their images built
	scripts/e2e.sh python

e2e-nodejs: ## part of `make e2e`: the Node.js qualification matrix, its images built
	scripts/e2e.sh nodejs

release: ## push images (amd64, arm64; provenance, SBOM) and the chart to the publish root of REGISTRY (PUBLISH_ROOT=<lowercase root> replaces it), sign them (cosign); only in the public repository's release workflow (RELEASE_REHEARSAL=1 elsewhere, against a registry of your own); needs REGISTRY VERSION REGISTRY_USER REGISTRY_TOKEN and the workflow's id-token permission (or COSIGN_KEY)
	scripts/release.sh

publish-ci: ## the publish workflow's step STEP (propose or tag) for VERSION: the release commit of main and its pull request in the public repository, or, once merged, its tag (needs RELEASE_TOKEN, and DENYLIST_CONTENT to propose)
	scripts/publish-ci.sh "$$STEP" "$$VERSION"

release-reviewed: ## the first gate of a release: the tagged commit (GITHUB_SHA) is on main, where only a merged pull request puts it; needs main's history
	scripts/release-reviewed.sh

check-dco: ## every commit from BASE_SHA to HEAD_SHA is signed off by its author (the Developer Certificate of Origin; dco.yml on pull requests)
	python3 scripts/check_dco.py "$$BASE_SHA" "$$HEAD_SHA"

release-files: ## the files of VERSION's GitHub release, after make release: the chart, the bills of materials, LICENSE and NOTICE, SHA256SUMS signed with cosign, and the notes from CHANGELOG.md (stops without its section)
	scripts/github-release.sh prepare "$$VERSION"

github-release: ## publish VERSION's GitHub release from a draft with the files of make release-files (PROVENANCE_BUNDLE adds the build provenance); needs GH_TOKEN allowed to write contents
	scripts/github-release.sh publish "$$VERSION"

verify-release: ## check a published release (VERSION=vX.Y.Z; REGISTRY, default ghcr.io/Impact-AI/impact-autoinstrumentation): cosign signatures against the release workflow's identity, SBOMs, GitHub build provenance (needs cosign, helm, gh)
	scripts/verify-release.sh

lock: ## resolve python/requirements.in into python/requirements.lock with hashes
	cd python && uv pip compile requirements.in --universal --generate-hashes --python-version 3.11 --no-header --no-annotate -o requirements.lock

# --- .NET proofs -------------------------------------------------------------------------------------------------------
# What the kit's .NET image will rest on, proved before it is built: a startup hook that reaches UseOpenTelemetry(), the
# stand-down beside another profiler under the Operator's environment rules, and glibc, musl and arm64 behind the
# Operator's runtime annotation, and a .NET the kit is not proved on. Nothing here is released. `make ci` runs them in its
# Node.js part (`make ci-nodejs`), on this machine's architecture; on an arm64 machine the same tests prove arm64. Their
# images are removed afterwards, so that the rest of the part has the disk.
.PHONY: images-dotnet test-dotnet
images-dotnet: ## build the .NET proofs' payload (the upstream agent 1.18.0 in the Operator's layout, for this machine and arm64, and the kit's startup hook, whose self-test runs in the build) and the ASP.NET Core fixtures on glibc and musl (DOTNET_VERSIONS, default 10.0) and on .NET 8
	scripts/build-images.sh dotnet

test-dotnet: images-dotnet ## the .NET proofs on DOTNET_VERSIONS: UseOpenTelemetry() without code, stand-down beside Datadog's profiler and an exporter of the workload's own, glibc and musl by the runtime annotation, the arm64 payload's layout, a .NET the kit is not proved on
	$(UV) pytest -c python/pyproject.toml --rootdir . dotnet/tests/image -m dotnet -n 3 -v -p no:cacheprovider --tb=short
	scripts/remove-dotnet-images.sh

.PHONY: test-dynatrace dynatrace-cells
test-dynatrace: ## apps that export OTLP to Dynatrace: the mock of its documented endpoint and the recorded copies (public documentation and open-source instrumentation only)
	$(UV) pytest -c python/pyproject.toml --rootdir . testing/apps/dynatrace -q -p no:cacheprovider

dynatrace-cells: ## run each of those apps again from its locked requirements, as Dynatrace's guide configures it, and compare it with its recording
	DYNATRACE_CELLS=1 $(UV) pytest -c python/pyproject.toml --rootdir . testing/apps/dynatrace -q -p no:cacheprovider -k cell

clean: ## remove build output and the containers, volumes and networks the image tests may have left
	rm -rf build
	-$(CONTAINER) ps -aq --filter name=kit-test- | xargs -r $(CONTAINER) rm -f
	-$(CONTAINER) network ls -q --filter name=kit-test- | xargs -r $(CONTAINER) network rm
	-$(CONTAINER) volume ls -q --filter name=kit-test- | xargs -r $(CONTAINER) volume rm -f
