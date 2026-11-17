<!-- SPDX-License-Identifier: Apache-2.0 -->

# .NET: the proofs before the image

Nothing in this directory is released, and the chart does not name it. The container tests in `tests/image`
(`make test-dotnet`, part of `make ci`) reproduce the OpenTelemetry Operator's .NET injection (v0.162.0) with the upstream
OpenTelemetry .NET automatic instrumentation 1.18.0 and the kit's startup hook (`hook/`), on ASP.NET Core workloads
(`testing/apps/dotnet`) on .NET 10, glibc and musl, and on .NET 8. The hook's self-test (`hook-test/`) runs when its
image is built. What they establish, and what the kit's .NET image is built on:

- **`UseOpenTelemetry()` without code.** The upstream agent records no Microsoft.Extensions.AI call: the library makes its
  GenAI spans only on a client wrapped with `UseOpenTelemetry()`. A startup hook named first in `DOTNET_STARTUP_HOOKS`
  (by the Instrumentation's env; the Operator appends its own) wraps every `IChatClient` the application registers in its
  services, while the host is built ("HostBuilding" on the `Microsoft.Extensions.Hosting` diagnostic listener). The model
  call then arrives inside the request's trace with provider, model, tokens and, as
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` says, content. An Agent Framework agent over such a client has its
  model calls traced, not its own `invoke_agent` span; a client the code builds outside the services is not reached.
  The hook wraps nothing in an application without Microsoft.Extensions.AI 9.5 or newer (a client registered with the
  abstractions and a provider's package alone), and leaves a client registered by type under a key, whose constructor
  may take the key; it says so in either case, and the application answers as without the kit.
- **Stand-down beside another tracer.** The Operator keeps a workload's own `CORECLR_PROFILER*`, so an APM vendor's
  profiler stays, and the upstream profiler never loads; but it appends the upstream startup hook, and that hook starts
  the agent anyway (Datadog's tracer and the upstream agent then both trace each request). The kit's hook runs first and
  stands the agent down through the agent's own `OTEL_DOTNET_AUTO_EXCLUDE_PROCESSES`, set in the process only; Datadog's
  tracer works as before. The same holds for OpenTelemetry exporter settings of the workload's own (the endpoints,
  headers, Zipkin and Jaeger settings the kit's Java agent counts), where the upstream profiler is loaded and stays idle,
  and for any failure of the hook itself. **Not yet for a workload that sets `DOTNET_STARTUP_HOOKS` itself** (an APM
  agent that attaches by startup hook does): the Operator adds the Instrumentation's env only where the workload has
  none, so the kit's hook is not named there, while the upstream hook is still appended and starts beside the workload's
  tracer. `test_a_workload_with_startup_hooks_of_its_own_gets_no_stand_down` records this; the image must put the kit's
  hook where the Operator always names it (the upstream hook's path) before the stand-down holds for every workload.
- **Platforms.** The runtime annotation (`otel-dotnet-auto-runtime: linux-x64 | linux-musl-x64`) only chooses
  `linux/` or `linux-musl/` in `CORECLR_PROFILER_PATH`; neither carries the architecture. A glibc and a musl workload are
  traced on amd64; a workload whose C library the annotation does not match starts and serves without the kit, and the
  hook says why. The arm64 payload holds arm64 profilers at the same paths; on an arm64 machine the same tests prove
  arm64.
- **.NET versions.** The hook is built for net8.0, the oldest .NET the upstream agent supports, because a startup hook
  built for a newer .NET cannot load on an older runtime and stops the application. It carries the versions the kit is
  proved on (`DOTNET_VERSIONS`, default 10.0); on any other it keeps the upstream agent from recording anything, and a
  .NET 8 service starts and serves. It turns the agent's signals off there rather than excluding it: the upstream
  profiler, attached as the process starts, redirects the application's `System.Diagnostics.DiagnosticSource` 8.0 to the
  agent's 10.0, which only the agent's loader provides, and a .NET 8 service with the loader excluded does not start.
  **The same holds for the stand-down by exclusion** wherever the upstream profiler is attached (exporter settings of
  the workload's own, a failure of the hook): it is proved on .NET 10 only, and a decision to support .NET 8 or 9 needs
  another stand-down there.
