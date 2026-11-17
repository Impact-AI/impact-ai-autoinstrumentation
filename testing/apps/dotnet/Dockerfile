# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Fixture workloads for the .NET proofs (dotnet/tests/image): one image per .NET and C library (DOTNET=10.0,
# LIBC=glibc|musl; the .NET version is a parameter, and each version names its base images below), each with
#   /app/Chat.dll    an ASP.NET Core support assistant that calls a model through Microsoft.Extensions.AI and Microsoft
#                    Agent Framework, with no tracing of its own (chat/Program.cs)
#   /lite/Lite.dll   the same assistant with Microsoft.Extensions.AI's abstractions and the OpenAI provider's package
#                    only, without Microsoft.Extensions.AI (lite/Program.cs)
#   /own-hook/Own.StartupHook.dll   a startup hook of the workload's own, as an APM agent that attaches by one has
#   /opt/datadog     Datadog's .NET tracer 3.57.0 for this architecture, as its own install puts it: an APM vendor's
#                    profiler, which a workload turns on with CORECLR_PROFILER (both C libraries are in the archive)
# and, with --target older, a service on a .NET the kit is not proved on (.NET 8, glibc; older/Program.cs).
# Base images are pinned by digest, and every download by SHA-256. NuGet packages are restored from a folder of pinned
# packages only, with nothing from the network.
ARG DOTNET=10.0
ARG LIBC=glibc
ARG TARGETARCH
FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble@sha256:1a48394b6f29d3a32c79d9edecd4abda0ecbe8776d567dbfc4590ed6979c740c AS runtime-10.0-glibc
FROM mcr.microsoft.com/dotnet/aspnet:10.0-alpine@sha256:486297ae006e95ee2ee3112a2c03e3d26e41fbbc64b7a29e28e7a18cc36b12bb AS runtime-10.0-musl

FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0-noble@sha256:212fe4cb74627d5adf9817a4088906ae3edff1bd853bb27565d4b46731efe61e AS build
ARG DOTNET
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
ADD --checksum=sha256:19bc9a44b781372bc9af1e3e4a481ec02964d67093b5d13bb0daa74a393c5413 https://api.nuget.org/v3-flatcontainer/google.protobuf/3.30.2/google.protobuf.3.30.2.nupkg /feed/
ADD --checksum=sha256:491b1cb1bd8563f5c2a43414dcf0a1e30a293aac12342cc74a700f5b8645488e https://api.nuget.org/v3-flatcontainer/microsoft.agents.ai/1.28.0/microsoft.agents.ai.1.28.0.nupkg /feed/
ADD --checksum=sha256:c6c0c1c7a207d71b9c8117e101d33aae72a3cf6d853ff01acd2a55611129c573 https://api.nuget.org/v3-flatcontainer/microsoft.agents.ai.abstractions/1.28.0/microsoft.agents.ai.abstractions.1.28.0.nupkg /feed/
ADD --checksum=sha256:8510cbc25ef42cdde1cec9317bb9e9fd6c8c2ce65fb3b9a2af6a73ab79aa3d28 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai/10.11.0/microsoft.extensions.ai.10.11.0.nupkg /feed/
ADD --checksum=sha256:87e1367f89d8a4957ff281df09703c860a93786830930fb2ba1f5d0af3ad2245 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai.abstractions/10.11.0/microsoft.extensions.ai.abstractions.10.11.0.nupkg /feed/
ADD --checksum=sha256:f60e07a45b97d67eb0389d7d68dc62723287e0c9f8da03a8ea17e3de47e1950e https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai.evaluation/10.11.0/microsoft.extensions.ai.evaluation.10.11.0.nupkg /feed/
ADD --checksum=sha256:e56e5831edefe91451fdf58f98d2977fb7afd231f532f9903b906905fffc9122 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.ai.openai/10.11.0/microsoft.extensions.ai.openai.10.11.0.nupkg /feed/
ADD --checksum=sha256:bf66ebe438451a3495562c8fb853a939d9e875e65b040637b66ce16b2313e7b1 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.caching.abstractions/10.0.13/microsoft.extensions.caching.abstractions.10.0.13.nupkg /feed/
ADD --checksum=sha256:e50192eccaab5c2ba6df50ebeba31252b6afcca62c274f74546a4f631224fab8 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.compliance.abstractions/10.10.0/microsoft.extensions.compliance.abstractions.10.10.0.nupkg /feed/
ADD --checksum=sha256:632deba4e226d5510f7584f219e1bb4bc6cd4fe001692262620b155957991ad5 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.configuration.abstractions/10.0.10/microsoft.extensions.configuration.abstractions.10.0.10.nupkg /feed/
ADD --checksum=sha256:676f9bb47a23af349f85b6f45f71d7a634db60a71b5c9f370d037215a8f3ac8c https://api.nuget.org/v3-flatcontainer/microsoft.extensions.dependencyinjection.abstractions/10.0.13/microsoft.extensions.dependencyinjection.abstractions.10.0.13.nupkg /feed/
ADD --checksum=sha256:be157a2858410586e06a919a9f24a21e1c9c9c7495a8fa88ca783c3b1017890f https://api.nuget.org/v3-flatcontainer/microsoft.extensions.diagnostics.abstractions/10.0.10/microsoft.extensions.diagnostics.abstractions.10.0.10.nupkg /feed/
ADD --checksum=sha256:bb79d3099a6aaa7e872159b43b6e199a6cb23656e65239aaf000de28cbb82725 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.fileproviders.abstractions/10.0.10/microsoft.extensions.fileproviders.abstractions.10.0.10.nupkg /feed/
ADD --checksum=sha256:8630d8878798bea4530828dafa3a4ec868b528004c95eaf37102b2ab95956d4a https://api.nuget.org/v3-flatcontainer/microsoft.extensions.filesystemglobbing/10.0.12/microsoft.extensions.filesystemglobbing.10.0.12.nupkg /feed/
ADD --checksum=sha256:59589a29dd1c6c7697cc561de82ba85effced1179b9f9616c406e66fa1581820 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.hosting.abstractions/10.0.10/microsoft.extensions.hosting.abstractions.10.0.10.nupkg /feed/
ADD --checksum=sha256:8c09f4328a801716b8997707fb567013db193935b4ef9e69897c83dc9c56c8b1 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.logging.abstractions/10.0.13/microsoft.extensions.logging.abstractions.10.0.13.nupkg /feed/
ADD --checksum=sha256:3d48df372c90ca674aa32250ca3478f6e270fc7987cd276723512a16da4f3c53 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.objectpool/10.0.12/microsoft.extensions.objectpool.10.0.12.nupkg /feed/
ADD --checksum=sha256:4b890f1ecc865a90ad99d8fe4bf2149d93aa600037461859c3be965aae90b308 https://api.nuget.org/v3-flatcontainer/microsoft.extensions.options/10.0.10/microsoft.extensions.options.10.0.10.nupkg /feed/
ADD --checksum=sha256:7a5f1dcbcc6e7b94cb3e7c18d22acef5823949fe94b4076ad20ff057322c6e6f https://api.nuget.org/v3-flatcontainer/microsoft.extensions.primitives/10.0.13/microsoft.extensions.primitives.10.0.13.nupkg /feed/
ADD --checksum=sha256:2d9aa1553a7dac59e9d5ff55b3be07068cdb1d3e0e05236c0e6b1f98f572072a https://api.nuget.org/v3-flatcontainer/microsoft.extensions.vectordata.abstractions/10.10.0/microsoft.extensions.vectordata.abstractions.10.10.0.nupkg /feed/
ADD --checksum=sha256:56f1ef70552fac61c4b67f728e32c2709d969a73e90d7320e5928291392845a2 https://api.nuget.org/v3-flatcontainer/microsoft.ml.tokenizers/2.0.0/microsoft.ml.tokenizers.2.0.0.nupkg /feed/
ADD --checksum=sha256:49c84ea677ef68b2bd9bdeb236c56f80e7bece489b86c4caafd496829f09ab8a https://api.nuget.org/v3-flatcontainer/openai/2.15.0/openai.2.15.0.nupkg /feed/
ADD --checksum=sha256:431e52106cbad62f4d18897f7ea093fdd1969bdac6946581a4167f7e42587744 https://api.nuget.org/v3-flatcontainer/system.clientmodel/1.16.0/system.clientmodel.1.16.0.nupkg /feed/
ADD --checksum=sha256:289a3b8d38e196a359acda1ea94b3c0aa1210f4fba14b06f7655a5fb4d12e79a https://api.nuget.org/v3-flatcontainer/system.memory.data/10.0.10/system.memory.data.10.0.10.nupkg /feed/
ADD --checksum=sha256:0e141778b912727a940871a169e2f6e568fa514e3b87038ca4a75462d3332573 https://api.nuget.org/v3-flatcontainer/system.numerics.tensors/10.0.13/system.numerics.tensors.10.0.13.nupkg /feed/
WORKDIR /src
COPY chat/ ./chat/
COPY lite/ ./lite/
COPY own-hook/ ./own-hook/
RUN dotnet publish chat/Chat.csproj -c Release -o /out -p:DotnetVersion=${DOTNET} --source /feed \
 && dotnet publish lite/Lite.csproj -c Release -o /out-lite -p:DotnetVersion=${DOTNET} --source /feed \
 && dotnet build own-hook/Own.StartupHook.csproj -c Release -o /out-own-hook -p:DotnetVersion=${DOTNET} --source /feed

FROM --platform=$BUILDPLATFORM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec AS datadog-amd64
ADD --checksum=sha256:f1f87e948fc36e16f13bd32affcedd1ca914634433ab19a5d1c40987cc12d40c https://github.com/DataDog/dd-trace-dotnet/releases/download/v3.57.0/datadog-dotnet-apm-3.57.0.tar.gz /download/datadog.tar.gz

FROM --platform=$BUILDPLATFORM docker.io/library/busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec AS datadog-arm64
ADD --checksum=sha256:9889a09095bf08194094ebdcfb673bb0206089158d323cd5f55e002c54dc4e4f https://github.com/DataDog/dd-trace-dotnet/releases/download/v3.57.0/datadog-dotnet-apm-3.57.0.arm64.tar.gz /download/datadog.tar.gz

FROM datadog-${TARGETARCH} AS datadog
RUN mkdir -p /opt/datadog && tar -xzf /download/datadog.tar.gz -C /opt/datadog && chmod -R go+rX /opt/datadog

# The service on .NET 8 (--target older): built by .NET 8's SDK, which has everything it needs, so nothing is restored.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0-noble@sha256:50ccf8db55d70df7338dd91c303764ab9c7ffc7253d8f32cf6961950966975c8 AS build-older
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
WORKDIR /src
COPY older/ ./
RUN mkdir /feed && dotnet publish Older.csproj -c Release -o /out -p:DotnetVersion=8.0 --source /feed

FROM mcr.microsoft.com/dotnet/aspnet:8.0-noble@sha256:188854d2ed80c2768395bddde1af897ebe562509fbbbae8a3c1b504e42c65eae AS older
COPY --from=build-older /out /app
WORKDIR /app
EXPOSE 8000
CMD ["dotnet", "/app/Older.dll"]

FROM runtime-${DOTNET}-${LIBC}
COPY --from=build /out /app
COPY --from=build /out-lite /lite
COPY --from=build /out-own-hook/Own.StartupHook.dll /own-hook/Own.StartupHook.dll
COPY --from=datadog /opt/datadog /opt/datadog
WORKDIR /app
EXPOSE 8000
CMD ["dotnet", "/app/Chat.dll"]
