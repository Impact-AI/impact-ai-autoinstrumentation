# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# impact.ai Python auto-instrumentation image, in the layout the OpenTelemetry Operator expects:
#   /autoinstrumentation        copied by the Operator's init container (`cp -r /autoinstrumentation/. <volume>`)
#   /autoinstrumentation-musl   used when the workload sets instrumentation.opentelemetry.io/otel-python-platform: musl
# Build context: this directory, plus the repository root as the named context `root` (--build-context root=.): the
# LICENSE and NOTICE live there, and the image carries them in /licenses with those of everything it bundles.
# Nothing is executed for the target architecture: the payload is assembled on the build
# platform from wheels fetched by hash, and compiled to bytecode by one interpreter per supported Python version
# (bytecode does not depend on the architecture), so multi-architecture builds need no emulation.
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM --platform=$BUILDPLATFORM docker.io/library/python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS payload
COPY --from=uv /uv /usr/local/bin/uv
ARG TARGETARCH
ARG VERSION=0.0.0+dev
WORKDIR /build
COPY requirements.lock qualification.json tools/assemble_payload.py ./
COPY --from=root LICENSE NOTICE ./
COPY src ./src
# Writes /out/autoinstrumentation, /out/autoinstrumentation-musl and /out/licenses (the license and notice files of
# every distribution in the payload, from their dist-info, and an index). Each payload carries the qualification.
RUN python assemble_payload.py --lock requirements.lock --src src --out /out --arch "${TARGETARCH}" \
        --version "${VERSION}" --pythons 3.11,3.12,3.13,3.14 --license LICENSE --notice NOTICE \
        --qualification qualification.json

# Bytecode per interpreter version, as unchecked hash-based .pyc files: the Operator's init container copies the files
# (new modification times), which would invalidate timestamp-based .pyc files, and every pod would compile the SDK again.
FROM --platform=$BUILDPLATFORM docker.io/library/python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893 AS pyc311
COPY --from=payload /out /out
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /out \
    && cd /out && find . -name '*.cpython-311.pyc' -print0 | tar --null -T - -cf - | (mkdir /pyc && tar -C /pyc -xf -)

FROM --platform=$BUILDPLATFORM docker.io/library/python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS pyc312
COPY --from=payload /out /out
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /out \
    && cd /out && find . -name '*.cpython-312.pyc' -print0 | tar --null -T - -cf - | (mkdir /pyc && tar -C /pyc -xf -)

FROM --platform=$BUILDPLATFORM docker.io/library/python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS pyc313
COPY --from=payload /out /out
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /out \
    && cd /out && find . -name '*.cpython-313.pyc' -print0 | tar --null -T - -cf - | (mkdir /pyc && tar -C /pyc -xf -)

FROM --platform=$BUILDPLATFORM docker.io/library/python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS pyc314
COPY --from=payload /out /out
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /out \
    && cd /out && find . -name '*.cpython-314.pyc' -print0 | tar --null -T - -cf - | (mkdir /pyc && tar -C /pyc -xf -)

FROM payload AS merged
COPY --from=pyc311 /pyc/ /out/
COPY --from=pyc312 /pyc/ /out/
COPY --from=pyc313 /pyc/ /out/
COPY --from=pyc314 /pyc/ /out/
# BusyBox's license files get their modes here, in a build stage, and reach the image as a directory: COPY --chmod
# onto a directory that COPY creates can give that directory the files' mode (0644, which no one can enter), as
# GitHub's Ubuntu 26.04 runners do.
COPY licenses/busybox /out/licenses/busybox
RUN chmod -R a+rX,go-w /out

# busybox provides the `cp` the Operator's init container runs. Non-root: it only reads this image and writes the
# emptyDir the Operator mounts, so it also starts under runAsNonRoot.
FROM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
ARG VERSION=0.0.0+dev
LABEL org.opencontainers.image.title="impact.ai Python auto-instrumentation" \
      org.opencontainers.image.description="OpenTelemetry Operator auto-instrumentation image for Python: GenAI content on the span" \
      org.opencontainers.image.source="https://github.com/Impact-AI/impact-ai-autoinstrumentation" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=merged /out/autoinstrumentation /autoinstrumentation
COPY --from=merged /out/autoinstrumentation-musl /autoinstrumentation-musl
# /licenses: ours, the generated python/ (every bundled distribution's own files and the index), and BusyBox's.
COPY --from=merged /out/licenses/python /licenses/python
COPY --from=root --chmod=0644 LICENSE NOTICE /licenses/
COPY --from=merged /out/licenses/busybox /licenses/busybox
USER 65532:65532
