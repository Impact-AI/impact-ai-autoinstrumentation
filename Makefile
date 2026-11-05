# SPDX-License-Identifier: Apache-2.0

# impact.ai instrumentation: everything CI and releases run. Tool versions come from mise.toml (jdx/mise-action in CI).
#
#   make ci        unit tests, chart checks, image builds and image tests, a canary of the overhead   (CI job "test", about 12 minutes)
#   make e2e       build, create a kind cluster, run e2e/ and the agent, tracer, Node.js matrix and gateway container
#                  tests, delete the cluster                                         (CI job "e2e", about 25 minutes)
#                  E2E_NODE_IMAGE=<kindest/node image> runs the Kubernetes part on another version (CI job "e2e-oldest",
#                  Kubernetes 1.27, about 18 minutes)
#   make overhead  the overhead of every runtime and server the images claim, against the budget of AE-17
#   make release   multi-architecture images and the chart, pushed to the publish root of $(REGISTRY) (its host and the
#                  lowercased owner: ghcr.io/impact-ai) and signed (cosign); measures the overhead first  (release workflow)
#   make verify-release   a published release, checked the way a customer does: signatures, SBOMs, provenance
#
# CONTAINER=docker (CI) or CONTAINER=podman (a workstation without Docker); the default is docker when it exists.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
export CONTAINER

UV := uv run --project python --frozen

.PHONY: help ci lint lint-python lint-reporter lint-chart lint-license lint-internal licenses test-python test-reporter test-chart test-nodejs-unit images test-image test-nodejs test-nodejs-matrix test-agents scan overhead overhead-canary overhead-covering qualify e2e release verify-release lock clean

help: ## list the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | column -t -s $$'\t'

ci: lint test-python test-reporter test-chart test-nodejs-unit images scan test-image test-nodejs overhead-canary ## unit tests, chart checks, image builds, the vulnerability scan, image tests and an overhead canary

lint: lint-python lint-reporter lint-chart lint-license lint-internal ## style and static checks

lint-python: ## ruff (python, nodejs tests, chart tests, e2e, test fixtures)
	$(UV) ruff check --config python/pyproject.toml python nodejs chart/tests e2e testing scripts
	$(UV) ruff format --check --config python/pyproject.toml python nodejs chart/tests e2e testing scripts

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

images: ## build the images the tests use (CONTAINER=docker|podman)
	scripts/build-images.sh core

test-image: ## the Operator's injection reproduced with containers: Python 3.11 to 3.14, uvicorn and gunicorn, stand-down, ingest down
	$(UV) pytest python/tests/image -m "image and not tracers and not agents" -n 3 -v -p no:cacheprovider --tb=short

test-nodejs: ## the Node.js image: layout, licenses, never throws on Node 16 to 26, spans with content, stand-down, ingest down, ES modules
	$(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m "nodejs and not matrix" -n 3 -v -p no:cacheprovider --tb=short

test-nodejs-matrix: ## the qualification matrix against nodejs/qualification.json: Node 22, 24, 26 by CommonJS and ES modules by the SDKs' versions
	$(UV) pytest -c python/pyproject.toml --rootdir . nodejs/tests/image -m matrix -n 3 -v -p no:cacheprovider --tb=short

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

overhead: ## the overhead of all eleven runtimes and servers, three rounds each, against the budget of AE-17: build/overhead/OVERHEAD.md
	scripts/overhead.sh full

test-agents: ## the agent frameworks (OpenAI Agents SDK, LangChain, LangGraph) in injected containers; AGENTS_PYTHONS="3.11 3.12 3.13 3.14" for all four
	scripts/build-images.sh agents
	$(UV) pytest python/tests/image -m agents -n 3 -v -p no:cacheprovider --tb=short

e2e: ## kind end to end (AE-1, AE-3, AE-4, AE-5, AE-7, AE-8, AE-10), then the tracer, agent, Node.js matrix and gateway container tests; E2E_NODE_IMAGE=<kindest/node image> for another Kubernetes version
	scripts/e2e.sh

release: ## push images (amd64, arm64; provenance, SBOM) and the chart to the publish root of REGISTRY (PUBLISH_ROOT=<lowercase root> replaces it), sign them (cosign); needs REGISTRY VERSION REGISTRY_USER REGISTRY_TOKEN and the workflow's id-token permission (or COSIGN_KEY)
	scripts/release.sh

verify-release: ## check a published release (VERSION=vX.Y.Z; REGISTRY, default ghcr.io/Impact-AI/impact-ai-k8s): cosign signatures against the release workflow's identity, SBOMs, GitHub build provenance (needs cosign, helm, gh)
	scripts/verify-release.sh

lock: ## resolve python/requirements.in into python/requirements.lock with hashes
	cd python && uv pip compile requirements.in --universal --generate-hashes --python-version 3.11 --no-header --no-annotate -o requirements.lock

clean: ## remove build output and the containers, volumes and networks the image tests may have left
	rm -rf build
	-$(CONTAINER) ps -aq --filter name=i019- | xargs -r $(CONTAINER) rm -f
	-$(CONTAINER) network ls -q --filter name=i019- | xargs -r $(CONTAINER) network rm
	-$(CONTAINER) volume ls -q --filter name=i019- | xargs -r $(CONTAINER) volume rm -f
