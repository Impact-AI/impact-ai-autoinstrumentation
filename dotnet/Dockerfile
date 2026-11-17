# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# What the .NET proofs inject (dotnet/tests/image). It is not released and the chart does not name it: the proofs decide
# what the kit's .NET image becomes. It is the OpenTelemetry Operator's own .NET image, rebuilt from the upstream
# release by SHA-256, with the kit's startup hook beside it:
#   /autoinstrumentation/                     copied by the Operator's init container (`cp -r /autoinstrumentation/. /otel-auto-instrumentation-dotnet`)
#     linux/, linux-musl/                     the upstream profiler for this image's architecture, glibc and musl: the
#                                             Operator's CORECLR_PROFILER_PATH names these directories, and its runtime
#                                             annotation (otel-dotnet-auto-runtime: linux-x64 | linux-musl-x64) only
#                                             chooses between them; linux-x64 and linux-musl-x64 (or -arm64) are links,
#                                             as in the Operator's image
#     net/                                    the upstream managed agent and its startup hook (DOTNET_STARTUP_HOOKS)
#     impact/Impact.StartupHook.dll           the kit's startup hook (dotnet/hook, net8.0), named first in
#                                             DOTNET_STARTUP_HOOKS by the Instrumentation's env
# The zip of each architecture is downloaded in a stage of its own, so a build for one architecture fetches only its
# own; nothing runs for the target platform, so an arm64 image builds on an amd64 machine without emulation.
ARG TARGETARCH

# The upstream agent, https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/tag/v1.18.0
FROM --platform=$BUILDPLATFORM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec AS agent-amd64
ADD --checksum=sha256:b8116ac9011b537e0a6edc607c815814ac93734ec28c4954f5a34bd5ad91599c https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/download/v1.18.0/opentelemetry-dotnet-instrumentation-linux-glibc-x64.zip /download/glibc.zip
ADD --checksum=sha256:50eeb870def171e6c4fd9562acd1d32cd8ca6ecc386120ce5966a017090dce07 https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/download/v1.18.0/opentelemetry-dotnet-instrumentation-linux-musl-x64.zip /download/musl.zip
ENV RID=x64

FROM --platform=$BUILDPLATFORM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec AS agent-arm64
ADD --checksum=sha256:0bfe3e6301f4ef5ab475984ce37de01c6372b472d01c7d5a303145ce13c3802b https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/download/v1.18.0/opentelemetry-dotnet-instrumentation-linux-glibc-arm64.zip /download/glibc.zip
ADD --checksum=sha256:9c9c8c02d2a42a415f95ea1636a4a4e6c416de568f1a66788f938624de7822d7 https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/download/v1.18.0/opentelemetry-dotnet-instrumentation-linux-musl-arm64.zip /download/musl.zip
ENV RID=arm64

FROM agent-${TARGETARCH} AS agent
WORKDIR /autoinstrumentation
RUN unzip -q /download/glibc.zip \
 && unzip -q /download/musl.zip "linux-musl-${RID}/*" -d . \
 && mv "linux-${RID}" linux && mv "linux-musl-${RID}" linux-musl \
 && ln -s linux "linux-${RID}" && ln -s linux-musl "linux-musl-${RID}" \
 && chmod -R go+rX . \
 && rm -rf /download

# The kit's startup hook, built from source for net8.0 (the oldest .NET the upstream agent supports, so that it loads on
# every .NET the Operator injects) against packages pinned by SHA-256 (restored from this folder only), with the .NET
# versions the kit is proved on (DOTNET_VERSIONS) in its metadata. Its self-test (hook-test) runs here, on the SDK's
# .NET 8, as java/build.sh runs the launcher's.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0-noble@sha256:50ccf8db55d70df7338dd91c303764ab9c7ffc7253d8f32cf6961950966975c8 AS hook
ARG PROVED=10.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
ADD --checksum=sha256:0b17da01eaae3aa899e1a3d8764cb606bef47a69382edd622ca49a4c8aeb53d4 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai/9.5.0/microsoft.extensions.ai.9.5.0.nupkg /feed/
ADD --checksum=sha256:28241238202c152ead4811d162b8a33247d4ac1eb660d27fd1430e6ed1b2ae01 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai.abstractions/9.5.0/microsoft.extensions.ai.abstractions.9.5.0.nupkg /feed/
ADD --checksum=sha256:c46a4aaf2c10bd4d569bf5a895823180760415f824346789f8619ce434f7a612 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.caching.abstractions/8.0.0/microsoft.extensions.caching.abstractions.8.0.0.nupkg /feed/
ADD --checksum=sha256:3bd8348d64be8df1a84f7caa2b06582462fe8c64887926f09afa262830b78535 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.dependencyinjection/8.0.1/microsoft.extensions.dependencyinjection.8.0.1.nupkg /feed/
ADD --checksum=sha256:51f2df1100245f10da54f0bb7e813f277155117777d4fbbab902214e27372606 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.dependencyinjection.abstractions/8.0.2/microsoft.extensions.dependencyinjection.abstractions.8.0.2.nupkg /feed/
ADD --checksum=sha256:e4c498d5a13051b4577a148f1d8c3470167215c507e2392069b75dc61322bb74 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.logging.abstractions/8.0.3/microsoft.extensions.logging.abstractions.8.0.3.nupkg /feed/
ADD --checksum=sha256:154f2a8f70d1f1b0dd73573e59e199c7f3c265eaaa9ddc1e64cf5ea5ca578d2a https://api.nuget.org/v3-flatcontainer/microsoft.extensions.primitives/8.0.0/microsoft.extensions.primitives.8.0.0.nupkg /feed/
ADD --checksum=sha256:ce6c078dc26029c6e49307a91f4dfc421727b16309707cacf8f11b1460b4260a https://api.nuget.org/v3-flatcontainer/system.diagnostics.diagnosticsource/8.0.1/system.diagnostics.diagnosticsource.8.0.1.nupkg /feed/
ADD --checksum=sha256:c8ac68e78c39a1d593ea73ebb9456c697e77a0f45efa02b31af2e79f1b703faf https://api.nuget.org/v3-flatcontainer/system.text.json/8.0.5/system.text.json.8.0.5.nupkg /feed/
ADD --checksum=sha256:7394d8a0b3570cbae82c83e795fc8c1e4ee767bd1001c91cfdced5d7df580a18 https://api.nuget.org/v3-flatcontainer/system.threading.channels/8.0.0/system.threading.channels.8.0.0.nupkg /feed/
WORKDIR /src
COPY hook/ ./hook/
COPY hook-test/ ./hook-test/
RUN dotnet build hook/Impact.StartupHook.csproj -c Release -o /out "-p:ProvedDotnet=${PROVED}" --source /feed \
 && dotnet build hook-test/HookSelfTest.csproj -c Release -o /selftest --source /feed \
 && dotnet /selftest/HookSelfTest.dll \
 && mkdir -p /payload/impact && cp /out/Impact.StartupHook.dll /payload/impact/ && chmod -R a+rX,go-w /payload

# busybox provides the `cp` the Operator's init container runs; non-root, as the Java image.
FROM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
LABEL org.opencontainers.image.title="impact.ai .NET proofs payload" \
      org.opencontainers.image.description="The OpenTelemetry .NET automatic instrumentation 1.18.0 in the Operator's layout, and the kit's startup hook; not released"
COPY --from=agent /autoinstrumentation /autoinstrumentation
# The hook reaches the image as a directory whose modes the build stage set: COPY --chmod onto a directory that COPY
# creates can give that directory the file's mode (0644, which no one can enter), as GitHub's Ubuntu 26.04 runners do.
COPY --from=hook /payload/impact /autoinstrumentation/impact
USER 65532:65532
