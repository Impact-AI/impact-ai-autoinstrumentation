# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# impact.ai Node.js auto-instrumentation image, in the layout the OpenTelemetry Operator expects:
#   /autoinstrumentation   copied by the Operator's init container (`cp -r /autoinstrumentation/. <volume>`); the workload
#                          then starts with NODE_OPTIONS=--require /otel-auto-instrumentation-nodejs/autoinstrumentation.js
# It holds one payload tree, node22 (OpenTelemetry JavaScript 3.x, Node 22.15 and later); on an older Node the preload does
# nothing but report why.
# Build context: this directory, plus the repository root as the named context `root` (--build-context root=.): the LICENSE
# and NOTICE live there, and the image carries them in /licenses with those of everything it bundles.
# Nothing is executed for the target architecture, and no package script runs at all: the tree is installed from the
# lock file, where every tarball has its integrity hash, and its packages are plain JavaScript, so one build serves both
# architectures and multi-architecture builds need no emulation.
FROM --platform=$BUILDPLATFORM docker.io/library/node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS payload
ARG VERSION=0.0.0+dev
WORKDIR /build
COPY shims ./shims
COPY trees ./trees
# --install-links copies the stand-in for openai (shims/openai) into the tree instead of linking to it.
RUN --mount=type=cache,target=/root/.npm \
    for tree in node22; do \
      (cd "trees/$tree" && npm ci --ignore-scripts --install-links --omit=dev --no-audit --no-fund --loglevel=error) || exit 1; \
    done
COPY src ./src
COPY qualification.json tools/assemble_payload.mjs ./
COPY --from=root LICENSE ./
# Writes /out/autoinstrumentation and /out/licenses (the license and notice files of every package in the payload, from the
# package itself, and an index). The build fails on a provider SDK, a native add-on, a link out of the tree, or a package
# without a license it can name.
RUN node assemble_payload.mjs --trees trees --src src --out /out --version "${VERSION}" --license LICENSE \
        --qualification qualification.json
# BusyBox's license files get their modes here, in a build stage, and reach the image as a directory: COPY --chmod
# onto a directory that COPY creates can give that directory the files' mode (0644, which no one can enter), as
# GitHub's Ubuntu 26.04 runners do.
COPY --from=root python/licenses/busybox /out/licenses/busybox
RUN chmod -R a+rX,go-w /out/licenses/busybox

# busybox provides the `cp` the Operator's init container runs. Non-root: it only reads this image and writes the
# emptyDir the Operator mounts, so it also starts under runAsNonRoot.
FROM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
ARG VERSION=0.0.0+dev
LABEL org.opencontainers.image.title="impact.ai Node.js auto-instrumentation" \
      org.opencontainers.image.description="OpenTelemetry Operator auto-instrumentation image for Node.js: model calls on the span, content included" \
      org.opencontainers.image.source="https://github.com/Impact-AI/impact-ai-autoinstrumentation" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=payload /out/autoinstrumentation /autoinstrumentation
# /licenses: ours, the generated nodejs/ (every bundled package's own files and the index), and BusyBox's.
COPY --from=payload /out/licenses/nodejs /licenses/nodejs
COPY --from=root --chmod=0644 LICENSE NOTICE /licenses/
COPY --from=payload /out/licenses/busybox /licenses/busybox
USER 65532:65532
