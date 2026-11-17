# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# impact.ai Java auto-instrumentation image, in the layout the OpenTelemetry Operator expects:
#   /javaagent.jar   copied by the Operator's init container (`cp /javaagent.jar /otel-auto-instrumentation-java/javaagent.jar`);
#                    the workload then starts with JAVA_TOOL_OPTIONS=-javaagent:/otel-auto-instrumentation-java-<container>/javaagent.jar
# The jar is the kit's launcher with the upstream OpenTelemetry Java agent and the kit's extension nested in it (java/build.sh
# says why and how). The launcher stands down beside another agent, declines on a Java outside JAVA_VERSIONS and reports
# either; otherwise it starts the upstream agent with the kit's scope and settings.
# Build context: this directory, plus the repository root as the named context `root` (--build-context root=.): the LICENSE
# and NOTICE live there, and the image carries them in /licenses with those of everything it bundles.
# Every download is pinned by its SHA-256 (ADD --checksum) and comes from Maven Central; there is no build tool and no
# dependency resolution. The jar holds bytecode only, so one build serves both architectures.
FROM --platform=$BUILDPLATFORM docker.io/library/eclipse-temurin:21-jdk-noble@sha256:114cabb73033ca4ce7ceec3deb62396bc1b716282bae051ed3c1d82e38fcab2d AS build
ARG VERSION=0.0.0-dev
# The Java feature releases the image starts on; a JVM of another release starts as it would without the kit.
ARG JAVA_VERSIONS=17,21,25
# The upstream agent, https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/tag/v2.33.1. Its
# SHA-256 is written twice, in ADD and in AGENT_SHA256 (which the launcher checks the unpacked copy against);
# java/tests/image/test_java_image.py keeps them equal.
ARG AGENT_SHA256=e43a270df53c6f423e0ab157be9c27ccc01d5dc3232e162fe54777573db2f149
ADD --checksum=sha256:e43a270df53c6f423e0ab157be9c27ccc01d5dc3232e162fe54777573db2f149 https://repo1.maven.org/maven2/io/opentelemetry/javaagent/opentelemetry-javaagent/2.33.1/opentelemetry-javaagent-2.33.1.jar /build/deps/opentelemetry-javaagent.jar
# What the extension compiles against: the OpenTelemetry API and SDK of the version the agent bundles (1.67.0). They are
# not in the image: the agent brings its own copy.
ADD --checksum=sha256:538d1dff2f9a37aecbc0ef98fd17273a3affb058e734e6763a3a07a38a5fdc59 https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-api/1.67.0/opentelemetry-api-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:40fb813fb5e27ec2691daa29c81b572b5b1541ef9b41e62490c3ee3e6407c55e https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-context/1.67.0/opentelemetry-context-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:da97bdb395418e16ecdcbe7ad3497f45b9e767ceda4ae7e86133e7a757ac377f https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-common/1.67.0/opentelemetry-common-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:628a4c00db9561ff4c608a650d5e7fd66a1e153ccdf1ba3cdc5ac0b7c6518fea https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-sdk-common/1.67.0/opentelemetry-sdk-common-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:14d9615c8a5dcf471dcbb74902b771dcfa8656b755ac38dce904042289ccd198 https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-sdk-trace/1.67.0/opentelemetry-sdk-trace-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:dd0f0b625e08787af49d7e270abc6def4c2eabc2f18f5f145b7924024a3311e3 https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-sdk-logs/1.67.0/opentelemetry-sdk-logs-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:c157ff10da59a87a57e5236d9f3290792d9b749433be5e5b449a7d1604c320a9 https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-sdk-metrics/1.67.0/opentelemetry-sdk-metrics-1.67.0.jar /build/deps/compile/
ADD --checksum=sha256:41eb19f6caf2c655ac3139d331f5c34283f2efdb0f0b0e58765a6cc9e27f66bc https://repo1.maven.org/maven2/io/opentelemetry/opentelemetry-sdk-extension-autoconfigure-spi/1.67.0/opentelemetry-sdk-extension-autoconfigure-spi-1.67.0.jar /build/deps/compile/
WORKDIR /build/src
COPY build.sh scope.txt ./
COPY launcher ./launcher
COPY extension ./extension
COPY test-src ./test-src
# Builds /out/javaagent.jar and /out/licenses, and runs the self tests of the launcher and the extension.
RUN VERSION="${VERSION}" JAVA_VERSIONS="${JAVA_VERSIONS}" AGENT_SHA256="${AGENT_SHA256}" bash build.sh /build/deps /out
# BusyBox's license files get their modes here, in a build stage, and reach the image as a directory: COPY --chmod
# onto a directory that COPY creates can give that directory the files' mode (0644, which no one can enter), as
# GitHub's Ubuntu 26.04 runners do.
COPY --from=root python/licenses/busybox /out/busybox-licenses
RUN chmod -R a+rX,go-w /out/busybox-licenses

# busybox provides the `cp` the Operator's init container runs. Non-root: it only reads this image and writes the
# emptyDir the Operator mounts, so it also starts under runAsNonRoot.
FROM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="impact.ai Java auto-instrumentation" \
      org.opencontainers.image.description="OpenTelemetry Operator auto-instrumentation image for Java: the OpenTelemetry Java agent 2.33.1, cut to request roots and AI" \
      org.opencontainers.image.source="https://github.com/Impact-AI/impact-ai-autoinstrumentation" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build --chmod=0644 /out/javaagent.jar /javaagent.jar
# /licenses: ours, the upstream agent's (its own and those of everything it bundles, as it ships them), and BusyBox's.
COPY --from=build /out/licenses /licenses/java
COPY --from=root --chmod=0644 LICENSE NOTICE /licenses/
COPY --from=build /out/busybox-licenses /licenses/busybox
USER 65532:65532
