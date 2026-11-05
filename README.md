# impact.ai instrumentation

What customers install so that impact.ai receives their AI applications' traces without code changes.

- **Cluster kit** (Kubernetes): a Helm chart with the OpenTelemetry Operator, impact.ai's Python and Node.js
  instrumentation images, a gateway collector that adds placement, and a names-only reporter that lists the cluster's
  workloads. It covers Python 3.11 to 3.14 and Node.js 22.15 or later; an older Node starts the application as without
  the kit, and the setup report says why. Tested on Node.js 22, 24 and 26, CommonJS and ES modules.
- Later: Lambda layers, container init images and a launcher for virtual machines, sharing the same instrumentation
  packages.

Customers get the install command from **Cover your AI** in impact.ai; they never edit this repository. The chart's
[README](chart/impact-ai/README.md) is the reference for whoever reviews or runs the kit: what it installs and
requests, what it may do in the cluster, what leaves the cluster, what the cluster must allow, which platforms it is
tested on, how to verify a release, upgrade and uninstall.

## Layout

| Path | Holds |
|---|---|
| `python/` | The Python image in the OpenTelemetry Operator's auto-instrumentation layout, its tests, the AI gateway tests (`tests/image/test_gateways.py`) and the overhead measurements (`tests/overhead/`) |
| `nodejs/` | The Node.js image in the same layout: a preload (`src/`), a payload tree pinned by lock file (`trees/`), the build that assembles and licenses it (`tools/`), the qualification of what it traces (`qualification.json`), and its tests |
| `reporter/` | The in-cluster reporter (Go) that sends the names-only inventory to `POST /v1/inventory` |
| `chart/impact-ai/` | The Helm chart (`chart/tests/`: its render checks) |
| `e2e/` | End-to-end tests on a kind cluster |
| `testing/` | Mock ingest and model providers, and the Python and Node.js fixture workloads the container and end-to-end tests use |
| `Makefile`, `scripts/`, `mise.toml` | Everything CI and releases run; tool versions |

## Build, test, release

- `make ci`: lint (style, license headers, nothing internal in the source or the rendered chart), unit tests (the
  Node.js package's run in a container, `make test-nodejs-unit`), chart checks, image builds, the trivy scan of the
  three released images, the container tests that reproduce the Operator's injection (Python 3.11 to 3.14 under uvicorn
  and gunicorn, a Starlette and Bedrock Nova workload; Node.js 22, 24 and 26, `make test-nodejs`: layout, a
  preload that never throws on Node 16 to 26, one span with content per model call, stand-down beside another tracer,
  ingest down, ES modules where the qualification proves them), and an overhead canary (`make overhead-canary`).
  `make e2e`: the kind end-to-end suite, then the container tests that need the largest fixtures, those of the tracers, of the
  agent frameworks (OpenAI Agents SDK, LangChain, LangGraph; `make test-agents` alone, on the Pythons of
  `AGENTS_PYTHONS`, default "3.12 3.13 3.14"), the Node.js qualification matrix (`make test-nodejs-matrix`: Node 22,
  24 and 26 as CommonJS and as ES modules against OpenAI's SDK 4 to 7 and Anthropic's from 0.134) and the AI gateways (a
  LiteLLM proxy and a Portkey gateway, by digest, against the mock). The jobs and their time limits are in
  `.github/workflows/ci.yml`. All need a container engine (`CONTAINER=docker`, or `podman`), and the tools of `mise.toml` (`mise install`, trivy
  included).
- `make overhead`: what an injected workload costs more than the same workload without injection, for every runtime and
  server the images claim (Python 3.11 to 3.14 under uvicorn and gunicorn, Node.js 22, 24 and 26 under Express): three
  paired rounds each, the median of the differences held to +1.5 ms of CPU per model call, +40 MiB and +2 s to start, a
  failing workload measured once more. A Node.js workload's memory is read after both sides have collected all their
  garbage, because before a collection the reading says how far V8's heap has grown and not what the instrumentation
  holds: V8's young generation grows by doubling and does not shrink on its own, and two identical plain processes
  differ by -22 to +23 MiB (with ten times the calls the two readings are within 3 MiB). The raw reading is kept in the
  record (`memory_unsettled_mib`) beside the one judged, and the budget is unchanged at +40 MiB: five runs in a row put
  the median difference at 5.5 to 7.4 MiB on Node.js 22, 6.4 to 9.9 on 24 and 6.3 to 10.8 on 26. It needs the machine to
  itself. `make release` runs it before anything is pushed and publishes the numbers in the chart (`OVERHEAD.md` and the
  `impact.ai/overhead` annotation of `Chart.yaml`).
- `make qualify` runs the Node.js matrix again and rewrites `nodejs/qualification.json`: the preload traces an ES module
  application only where that file proves it, and `make test-nodejs-matrix` fails when a cell no longer says what it
  did. A workload can override the file with `IMPACT_NODEJS_ESM=on` (try an ES module anyway) or `off`.
- `make e2e` on a workstation: kind runs a whole cluster inside the user's own process limits, and the Linux default of
  128 inotify instances per user (`fs.inotify.max_user_instances`) is easily used up by a desktop plus a cluster; the
  Operator then crash-loops with "couldn't initialize inotify: too many open files". `scripts/e2e.sh` raises the limit
  with `sudo -n` when it may (CI can); elsewhere run `sudo sysctl -w fs.inotify.max_user_instances=1024` once.
- Releases: push a tag `vX.Y.Z`; the release workflow runs `make release`, which publishes multi-architecture images
  (`autoinstrumentation-python`, `autoinstrumentation-nodejs`, `kubernetes-reporter`) with provenance and SBOM to
  `ghcr.io/impact-ai/<image>` and the chart to `oci://ghcr.io/impact-ai/charts/impact-ai`, with image digests written into
  the chart. They go to the root of the organisation: the registry host and the lowercased owner of the repository the
  release runs in (`PUBLISH_ROOT` replaces it; `scripts/names.sh`). Before anything is pushed, every image is built for
  both platforms and scanned: a fixable CRITICAL or HIGH vulnerability (trivy), or an internal detail in its files,
  stops the release. After the push the chart and the three images are
  signed by digest with cosign (keyless: the workflow's own identity, no key to keep), each image gets a signed SPDX bill
  of materials, and GitHub records the build provenance of all four. `make verify-release VERSION=vX.Y.Z` checks a
  published release the way a customer does; the commands are in the chart README under "Verify what you install". A
  release also stops while [SECURITY.md](SECURITY.md) holds a placeholder.

## Bugs, security and contributions

* A bug, or something wrong in the documentation: open an issue in this repository. Say the chart or image version
  (`helm list -n impact-ai`) and what you saw; for a problem in a cluster, add the output of
  `kubectl -n impact-ai get pods` and `kubectl -n impact-ai logs deploy/impact-ai-reporter --tail=50`.
* A security problem: do not open an issue; use the private channel in [SECURITY.md](SECURITY.md).
* Which versions of Python, Node.js and Kubernetes the kit supports, and how it stays current: [SUPPORT.md](SUPPORT.md).
* A contribution from outside impact.ai needs a contributor agreement with impact.ai. Open an issue before you start,
  and we tell you how to sign it.

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Every image carries both in `/licenses`, with the
license and notice files of everything it bundles (the Python distributions, the npm packages, the Go modules and
runtime, BusyBox) and an index, all collected by the build from the packages themselves; the chart carries them too,
with the Operator chart's own. Every source file of ours has an `SPDX-License-Identifier: Apache-2.0` header, which
`make lint` checks.
