# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# The in-cluster reporter. Static binary, no shell, non-root. Build context: this directory, plus the repository root as
# the named context `root` (--build-context root=.): the LICENSE and NOTICE live there, and the image carries them in
# /licenses with the license of what the binary contains.
# Cross-compiled on the build platform, so multi-architecture builds need no emulation.
FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.27.3@sha256:e519d3b0a5b285e5aa53ab9eef02bc6bbb9cb04608389b743a136a9ce75f0d1e AS build
ARG TARGETOS=linux
ARG TARGETARCH
ARG VERSION=dev
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
    go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/reporter ./cmd/reporter
# The license and notice files of the modules the binary links (none today) and of the Go runtime, from the module
# cache and GOROOT, and an index. Fails when a linked module has no license file. The tool is built for the build
# platform and runs there; GOOS and GOARCH only make `go list` name the packages of the target.
RUN go build -o /tmp/licenses ./tools/licenses \
    && GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" /tmp/licenses -pkg ./cmd/reporter -out /out/licenses/go \
    && chmod -R a+rX /out/licenses

FROM gcr.io/distroless/static:nonroot@sha256:25ac8f0c3b2334a9eb05452266bd4697864a8c8ef0ef71e5e6480d7f92868329
ARG VERSION=dev
LABEL org.opencontainers.image.title="impact.ai cluster reporter" \
      org.opencontainers.image.description="Reports a cluster's workloads by name (never values) to impact.ai" \
      org.opencontainers.image.source="https://github.com/Impact-AI/impact-ai-autoinstrumentation" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build /out/reporter /reporter
COPY --from=build /out/licenses/go /licenses/go
COPY --from=root --chmod=0644 LICENSE NOTICE /licenses/
USER 65532:65532
ENTRYPOINT ["/reporter"]
