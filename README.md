# Impact AI auto-instrumentation

[![ci](https://github.com/Impact-AI/impact-autoinstrumentation/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Impact-AI/impact-autoinstrumentation/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/Impact-AI/impact-autoinstrumentation/badge)](https://scorecard.dev/viewer/?uri=github.com/Impact-AI/impact-autoinstrumentation)
[![release](https://img.shields.io/github/v/release/Impact-AI/impact-autoinstrumentation)](https://github.com/Impact-AI/impact-autoinstrumentation/releases)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A Helm chart that instruments the Python and Node.js workloads of a Kubernetes cluster that call AI models, without a
code change or a new image, and sends their traces and a names-only inventory of the cluster to Impact AI.

**Zero-code, or in your code.** This repository is the zero-code route: nothing in the application changes. To trace
in the application's code instead, or outside Kubernetes, send OpenTelemetry traces from it yourself: point its OTLP
exporter at your ingest endpoint with your product's key, both of which Impact AI shows when you create a product. A
workload that already has a tracer of its own keeps it: the kit stands down ([coexistence](docs/coexistence.md)).

**Prompts and answers are captured by default.** A covered workload's model-call spans carry the prompt, the answer,
and the arguments and results of tool calls. Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` on a
workload to send none of it. [What leaves the cluster](docs/what-leaves-the-cluster.md) lists every field.

## What it installs

One release per cluster, in the namespace `impact-ai`:

* the OpenTelemetry Operator 0.162.0, with Python and Node.js injection only, and an `Instrumentation` resource that
  points it at Impact AI's images;
* a gateway (OpenTelemetry Collector 0.164.0, core distribution) that adds the cluster's identity to spans and log
  events and forwards them to Impact AI;
* a reporter that sends the cluster's workloads by name every 5 minutes: names of workloads, images, environment
  variables and Secret references, never their values;
* with an install token, a cover pod that annotates new pods of the workloads Impact AI lists, and Helm hooks that make
  the cluster's ingest key at install and remove it at uninstall.

The pods request 90m CPU and 224 Mi in total (100m and 248 Mi with the cover pod). Nothing is privileged, and every
webhook fails open: a workload starts uninstrumented rather than wait for the kit. Details:
[chart README](chart/impact-ai/README.md#what-it-installs).

## Requirements

| | |
|---|---|
| Kubernetes | 1.27 or later, Linux nodes. CI tests 1.27, 1.34 and 1.37 ([compatibility](docs/compatibility.md#kubernetes)) |
| Helm | 3.18 or later (tested with 3.22 and 4.3) |
| Installing user | `cluster-admin`: the chart creates a CRD, ClusterRoles and webhook configurations |
| Network | Egress on 443 to the Impact AI ingest host; image pulls from `ghcr.io` or a mirror; the API server to port 9443 in the release namespace |
| Workloads | Python 3.11 to 3.14; Node.js 22.15 or later |

## Install

Cover your AI in Impact AI shows this command with its values filled in. The install token is valid for 15 minutes and
for one use; the cluster trades it for its own ingest key.

```sh
helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai --version <version> \
  --namespace impact-ai --create-namespace --wait \
  --set-string product.id=<product> \
  --set-string ingest.endpoint=<ingest origin, no path> \
  --set-string cluster.name=<name> \
  --set-string install.token=<one-time install token>
```

To install without a token (GitOps, a key you manage), see [install options](docs/install.md).

## Verify a release

The chart and the images `autoinstrumentation-python`, `autoinstrumentation-nodejs` and `kubernetes-reporter` are
signed with cosign by the release workflow's identity, with signed SBOMs and GitHub build provenance. For the chart:

```sh
version=<version>
digest=$(helm pull oci://ghcr.io/impact-ai/charts/impact-ai --version "$version" 2>&1 | sed -n 's/^Digest: //p')
cosign verify "ghcr.io/impact-ai/charts/impact-ai@$digest" \
  --certificate-identity "https://github.com/Impact-AI/impact-autoinstrumentation/.github/workflows/release.yml@refs/tags/v$version" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The images, the SBOMs and the provenance: [verify what you install](chart/impact-ai/README.md#verify-what-you-install).
Each [GitHub release](https://github.com/Impact-AI/impact-autoinstrumentation/releases) carries the chart package, the
SBOM of each image and platform, and `SHA256SUMS` with its cosign signature `SHA256SUMS.sigstore.json` and its build
provenance; its notes give the command that checks them.
This repository also builds a Java image, `autoinstrumentation-java`; releases do not publish it yet.

## Documentation

* [Chart README](chart/impact-ai/README.md): requirements, install, values, verify, upgrade, uninstall
* [Install options](docs/install.md): the install token, giving the key yourself, cloud identity, image mirrors
* [How injection works](docs/how-injection-works.md): the annotation, automatic coverage, what is traced
* [Coexistence and stand-down](docs/coexistence.md): beside another injector or tracer
* [What leaves the cluster](docs/what-leaves-the-cluster.md)
* [Failure modes](docs/failure-modes.md)
* [Security model](docs/security-model.md): RBAC, network flows, admission rules
* [Troubleshooting](docs/troubleshooting.md)
* [Compatibility](docs/compatibility.md): runtimes, libraries, Kubernetes, overhead, versions
* [Changelog](CHANGELOG.md) and [how a release is made](RELEASING.md)

## Support, security and contributing

* Bugs and questions: [SUPPORT.md](SUPPORT.md). Vulnerabilities: [SECURITY.md](SECURITY.md), never a public issue.
* Releases come here from Impact AI's development repository, one reviewed pull request each. Issues and pull
  requests are welcome; sign off your commits (DCO). How the kit is built and tested: [CONTRIBUTING.md](CONTRIBUTING.md).
* [Code of conduct](CODE_OF_CONDUCT.md).

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Every image carries both in `/licenses`, with the
license and notice files of everything it bundles (the Python distributions, the npm packages, the Go modules and
runtime, BusyBox) and an index, all collected by the build from the packages themselves; the chart carries them too, with the Operator chart's own. Every source file of ours has an
`SPDX-License-Identifier: Apache-2.0` header, which `make lint` checks.
