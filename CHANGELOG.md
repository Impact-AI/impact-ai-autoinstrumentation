# Changelog

All notable changes to the chart `impact-ai` and the images it installs. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions use [Semantic Versioning](https://semver.org)
numbers. Before 1.0, a minor release may contain breaking changes: they are listed first, under **Breaking**, with the
upgrade step. A release that changes the resource definition `instrumentations.opentelemetry.io` says so; apply it as
the [chart README](chart/impact-ai/README.md#upgrade) shows.

## [Unreleased]

## [0.3.0] - 2026-11-14

### Added

- The user, session and use case an application sets: the Python and Node.js packages copy `user.id`, `session.id`,
  `gen_ai.conversation.id` and `impact.use_case` from the request's OpenTelemetry baggage onto every span of that
  request, model calls included. Values a caller sends in its `baggage` header are left out unless the pod trusts
  its callers (`impact.ai/trust-caller-baggage: "true"`).
- AI content written as OpenTelemetry log events: the gateway forwards log events to `<ingest.endpoint>/v1/logs` with
  the same key, retries and cluster attributes as spans, in a queue of its own.
- `global.imageRegistry` also covers the images the Operator names in its arguments (`--collector-image` and the like),
  so one value moves every image to a mirror.
- The steps for giving the ingest key yourself read it at a prompt that shows nothing and hand it to `helm` on
  standard input.
- The cover pod skips list entries in a language this version does not inject and counts them in its log, so a list
  made for a newer version still covers what this one can.
- A Java image, `autoinstrumentation-java` (the OpenTelemetry Java agent, unchanged, inside a launcher that stands down
  beside another agent), built and tested. Releases do not publish it yet and the chart does not inject it.
- Stand-down beside LangSmith's OpenTelemetry mode, Elastic's EDOT, Arize AX, Phoenix's Node.js package and Logfire for
  Node.js, and beside Impact AI's own SDK (`impact` on PyPI, `impact-ai` and `@impact-ai/sdk` on npm), so that an
  application that uses the SDK has its model calls traced once. LangSmith's default tracing runs beside the package
  and receives the same runs.
- The reporter has a `forward` command that relays a workload's spans to its own tracer's backend first and copies them
  to Impact AI, with decoders for Datadog, New Relic and Zipkin payloads. The chart does not run it.
- Documentation pages under `docs/`: install options, how injection works, coexistence, what leaves the cluster, failure
  modes, security model, troubleshooting and compatibility. `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and issue forms.
- Each release is a GitHub release: its notes from this changelog, and as its files the chart package, the SBOM of each
  image and platform, `LICENSE`, `NOTICE`, and `SHA256SUMS` of them with its cosign signature and build provenance.
- The chart carries what Artifact Hub shows of a release: an icon, its links, the version's changes and its images by
  digest.
- A Lambda layer for Python functions, built and tested, not published yet (`lambda/README.md`): one change to a
  function traces it as the image traces a workload, its handler wrapped, what it holds sent at the end of each
  invocation within a bounded wait, standing down beside another tracer's wrapper and failing open; tested in AWS's
  Lambda images for Python 3.11 to 3.14, with its cold-start cost measured.
- Gemini, through Google's `google-genai` SDK 1 to 2: the Gemini API and Vertex AI, sync, async and streaming, as
  `generate_content` spans with prompt and answer, model and tokens, proven on Python 3.11 to 3.14 by cells of the
  qualification, with a workload of its own in the overhead measurement.
- The proofs of a .NET image, built and tested: the OpenTelemetry .NET automatic instrumentation in the Operator's
  layout with a startup hook of the kit's that traces Microsoft.Extensions.AI chat clients without code, stands down
  beside another tracer and fails open, on .NET 8 and 10, glibc and musl (`dotnet/README.md`). Releases do not publish
  it and the chart does not inject it.

### Changed

- A Python AI library is traced only in the versions its qualification cells prove (`python/qualification.json`),
  which includes every version traced before. A workload with another version runs without that library's
  instrumentation, and the setup report says what Impact AI traces and what the workload runs.
- The Operator switches off every language it can inject other than Python and Node.js by name.
- A release is published only from the public repository, and only after the end-to-end suite passed on the tagged
  commit. A final release can follow its release candidate on the same tree.
- The public repository is `Impact-AI/impact-ai-autoinstrumentation` (it was `Impact-AI/impact-ai-k8s`). The signing
  identity of a release is that repository's release workflow; the chart and image names under `ghcr.io/impact-ai` do
  not change. A release arrives there as a pull request that CI checks and a maintainer reviews, and is published only
  from a tag on main. Contributions are signed off (Developer Certificate of Origin) instead of a contributor agreement.
- The chart README is shorter; the detail moved to `docs/`. Documents now say that releases publish three images, not
  four, and which Kubernetes versions CI tests.
- Dependency updates: the OpenTelemetry Python packages, `opentelemetry-instrumentation-bedrock` 0.63.0,
  `@arizeai/openinference-instrumentation-openai` 4.4.3, and the reporter's Go base image.
- The setup report also names the tool beside the package: `impact.setup.tool`, `impact.setup.tool.version`,
  `impact.setup.exporter` and `impact.setup.alongside`, names and a flag, never a setting's value. LangSmith's
  default tracing is reported beside the package.
- Each package ships a coexistence list: the package starts beside a tracer only where a proven cell of that list
  says so. The lists are empty, so every tracer keeps the stand-down.

### Fixed

- The Python payload copies its packages instead of linking them from the build cache.

## [0.3.0-rc1] - 2026-11-05

### Breaking

- The chart is named `impact-ai` (it was `cluster-kit`), and the images are published as `autoinstrumentation-python`,
  `autoinstrumentation-nodejs` and `kubernetes-reporter` under `ghcr.io/impact-ai`. Earlier versions used other names.

### Added

- Install with a one-time install token: a pre-install hook trades it inside the cluster for the cluster's own ingest
  key, so no key is copied, pasted or kept in a Helm value. A refused token prints Impact AI's reason with Helm 3.18 or
  later.
- Automatic coverage: the cover pod adds the injection annotation to new pods of the workloads Impact AI lists, at
  their next rollout. Its webhook has its own timeout of 2 seconds and fails open; it is off behind an image mirror
  unless asked for.
- Node.js workloads that call OpenAI or Anthropic are traced, on Node.js 22, 24 and 26, as CommonJS and as ES modules
  where the qualification proves them.
- The reporter reports why the pods of an automatically covered workload do not start (`stall`).
- Releases are signed with cosign by the release workflow's identity, with signed SPDX SBOMs and build provenance;
  `make verify-release` checks them the way a customer does.
- `SECURITY.md` and `SUPPORT.md`.

### Changed

- The gateway runs the OpenTelemetry Collector's core distribution (`otelcol`) instead of contrib.
- The Operator runs with a narrow role written by the chart; the upstream chart's role is not used.
- Uninstall is five steps: the pre-delete hook stops the reporter and the cover pod, sends a final notice and deletes
  the key Secret; the marks on pods come off in a step of the uninstall command, under the user's own rights, so no
  role of the kit writes to pods.
- A covered Node.js process holds at most 16 MiB of unsent spans and waits at most 5 seconds for the gateway at exit.

## [0.2.1] - 2026-11-04

### Added

- Python 3.14.
- Every image carries the license and notice files of everything it bundles, generated by the build.

### Changed

- The kit is licensed under Apache-2.0.
- A build fails on a fixable CRITICAL or HIGH vulnerability in an image, and on an internal detail in the source, the
  chart or an image. A release checks and scans before anything is pushed.

## [0.2.0] - 2026-11-03

### Added

- Agents are traced as agents: the OpenAI Agents SDK, LangChain and LangGraph, with the agent step, each tool step and
  each model call in one trace.
- The setup report lists the AI libraries and agent frameworks a workload has (`impact.setup.frameworks`) and those the
  image does not trace (`impact.setup.untraced`).

### Changed

- Reinstalling with a new key restarts the gateway and the reporter, so the cluster reports again at once.

## [0.1.0] - 2026-11-03

### Added

- The cluster kit: one Helm release with the OpenTelemetry Operator, the Python instrumentation image, a gateway
  collector and a names-only reporter.
- Python workloads that call OpenAI, Anthropic or Amazon Bedrock (Converse) are covered with one annotation, with no
  code change and no new image.
- The reporter sends the cluster's workloads by name, never by value, every 5 minutes, and a final notice when the kit
  is uninstalled.
- The package stands down when the workload already has a tracer, and the webhook fails open.
- Optional cloud identity values for spans and the inventory.
