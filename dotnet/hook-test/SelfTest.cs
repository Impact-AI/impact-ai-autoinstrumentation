// SPDX-License-Identifier: Apache-2.0

// The startup hook's decisions (dotnet/hook/StartupHook.cs), on a machine of the test's own: which .NET it starts on,
// what counts as another tracer, when the upstream profiler cannot load, that every failure stops the upstream agent
// too, and which chat client registrations it wraps. Exit code 1 when a check fails.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;

internal static class SelfTest
{
    private const string Kit = "http://gw.kit.svc:4318";
    private static readonly string[] Proved = { "10.0" };
    private static int failures;

    private static int Main()
    {
        Versions();
        OtherTracers();
        Profiler();
        Failures();
        ChatLibrary();
        Registrations();
        Console.WriteLine(failures == 0 ? "hook self-test: passed" : $"hook self-test: {failures} failed");
        return failures == 0 ? 0 : 1;
    }

    private static void Versions()
    {
        var on10 = Started(new Fake());
        Equal("a proved .NET starts", "started", on10.Lines.Single());
        Equal("the kit's endpoint", Kit, on10.Get("OTEL_EXPORTER_OTLP_ENDPOINT"));
        Check("listens for the host", on10.Listened);
        Equal("nothing excluded", null, on10.Get(StartupHook.Exclude));

        var on8 = Started(new Fake { Version = new Version(8, 0, 21) });
        Equal("another .NET", "not started: .NET 8.0, where the kit is proved on 10.0", on8.Lines.Single());
        Equal("the upstream agent stopped", "dotnet,Chat", on8.Get(StartupHook.Exclude));
        Check("no endpoint", on8.Get("OTEL_EXPORTER_OTLP_ENDPOINT") == null);
        Check("no listener", !on8.Listened);

        // With the upstream profiler attached, the agent's loader must run: its profiler redirected the application's
        // references to the agent's assemblies. Its signals go off instead.
        var attached = Started(new Fake
        {
            Version = new Version(8, 0, 21),
            ["CORECLR_ENABLE_PROFILING"] = "1",
            ["CORECLR_PROFILER"] = StartupHook.UpstreamProfilerId,
            ["CORECLR_PROFILER_PATH"] = "/otel-auto-instrumentation-dotnet/linux/OpenTelemetry.AutoInstrumentation.Native.so",
        });
        Equal(
            "another .NET, the upstream profiler attached",
            "not started: .NET 8.0, where the kit is proved on 10.0; the upstream agent's signals are off, because its profiler is attached",
            attached.Lines.Single());
        Equal("not excluded", null, attached.Get(StartupHook.Exclude));
        Check("every signal off", StartupHook.Signals.All(s => attached.Get(s) == "false"));
        Check("no endpoint there", attached.Get("OTEL_EXPORTER_OTLP_ENDPOINT") == null);

        Equal("the versions the build names", "10.0|9.0", string.Join("|", StartupHook.Proved()));
        Equal("none proved", ".NET 10.0, where the kit is proved on none", StartupHook.OutsideProved(new Version(10, 0), Array.Empty<string>()));
    }

    private static void OtherTracers()
    {
        var datadog = Started(new Fake
        {
            ["CORECLR_ENABLE_PROFILING"] = "1",
            ["CORECLR_PROFILER"] = "{846F5F1C-F9AE-4B07-969E-05C26BC060D8}",
            ["CORECLR_PROFILER_PATH"] = "/opt/datadog/linux-x64/Datadog.Trace.ClrProfiler.Native.so",
        });
        Equal("another profiler", "stood down: Datadog.Trace.ClrProfiler.Native.so (CORECLR_PROFILER)", datadog.Lines.Single());
        Equal("stopped", "dotnet,Chat", datadog.Get(StartupHook.Exclude));

        var settings = Started(new Fake
        {
            ["OTEL_TRACES_EXPORTER"] = "zipkin",
            ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.theirs:4318",
            ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://collector.theirs:4318/v1/traces",
            ["OTEL_EXPORTER_OTLP_HEADERS"] = "x-api-key=secret-value",
            ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = "x-trace-key=secret-value",
            ["OTEL_EXPORTER_ZIPKIN_ENDPOINT"] = "http://zipkin.theirs:9411",
            ["OTEL_EXPORTER_JAEGER_AGENT_HOST"] = "jaeger.theirs",
            ["OTEL_EXPORTER_JAEGER_PROTOCOL"] = " ",
        });
        Equal(
            "every exporter setting, by name",
            "stood down: OTEL_TRACES_EXPORTER (set by the workload), OTEL_EXPORTER_OTLP_ENDPOINT (set by the workload), "
                + "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (set by the workload), OTEL_EXPORTER_OTLP_HEADERS (set by the workload), "
                + "OTEL_EXPORTER_OTLP_TRACES_HEADERS (set by the workload), OTEL_EXPORTER_JAEGER_AGENT_HOST (set by the workload), "
                + "OTEL_EXPORTER_ZIPKIN_ENDPOINT (set by the workload)",
            settings.Lines.Single());
        Check("never a value", !settings.Lines.Single().Contains("secret") && !settings.Lines.Single().Contains("theirs"));

        foreach (var header in new[] { "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS" })
        {
            Equal(header, $"stood down: {header} (set by the workload)", Started(new Fake { [header] = "api-key=secret" }).Lines.Single());
        }
        Equal("OTLP is no signal", "started", Started(new Fake { ["OTEL_TRACES_EXPORTER"] = " OTLP " }).Lines.Single());
        Equal("a blank setting is none", "started", Started(new Fake { ["OTEL_EXPORTER_OTLP_HEADERS"] = "  " }).Lines.Single());
        // A process the workload starts inherits the endpoint this hook set in its parent: it is the kit's, not the
        // workload's.
        var child = Started(new Fake { ["OTEL_EXPORTER_OTLP_ENDPOINT"] = Kit });
        Equal("the kit's endpoint, inherited", "started", child.Lines.Single());
        var outside = new Fake { ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318" };
        outside.Env.Remove(StartupHook.KitEndpoint);
        Equal("outside the kit an endpoint is the kit's own", null, StartupHook.OtherTracer(outside));
        Equal(
            "outside the kit, another trace exporter is still the workload's",
            "OTEL_TRACES_EXPORTER (set by the workload)",
            StartupHook.OtherTracer(new Fake { ["OTEL_TRACES_EXPORTER"] = "console" }.Without(StartupHook.KitEndpoint)));
    }

    private static void Profiler()
    {
        Fake Upstream(string directory, string rid, bool present = true)
        {
            var path = $"/otel-auto-instrumentation-dotnet/{directory}/OpenTelemetry.AutoInstrumentation.Native.so";
            var fake = new Fake
            {
                ["CORECLR_ENABLE_PROFILING"] = "1",
                ["CORECLR_PROFILER"] = StartupHook.UpstreamProfilerId,
                ["CORECLR_PROFILER_PATH"] = path,
            };
            fake.Rid = rid;
            if (present)
            {
                fake.Files.Add(path);
            }
            return fake;
        }

        Equal("glibc, linux", "started", Started(Upstream("linux", "linux-x64")).Lines.Single());
        Equal("musl, linux-musl", "started", Started(Upstream("linux-musl", "linux-musl-arm64")).Lines.Single());
        Equal("the Operator's older links", "started", Started(Upstream("linux-musl-x64", "linux-musl-x64")).Lines.Single());
        var musl = Started(Upstream("linux", "linux-musl-x64"));
        Equal(
            "musl under the glibc directory",
            "not started: the runtime annotation names glibc (linux), the workload runs musl (linux-musl-x64)",
            musl.Lines.Single());
        Equal("stopped", "dotnet,Chat", musl.Get(StartupHook.Exclude));
        Check("no chat clients wrapped where nothing traces", !musl.Listened);
        Equal(
            "glibc under the musl directory",
            "not started: the runtime annotation names musl (linux-musl), the workload runs glibc (linux-x64)",
            Started(Upstream("linux-musl", "linux-x64")).Lines.Single());
        Equal(
            "no profiler at the path",
            "not started: the upstream profiler is not at CORECLR_PROFILER_PATH",
            Started(Upstream("linux", "linux-x64", present: false)).Lines.Single());
    }

    private static void Failures()
    {
        // Any failure stops the upstream agent too: its hook runs after this one.
        var failing = Started(new Fake { ListenFails = true });
        Equal("a failure", "not started: InvalidOperationException", failing.Lines.Single());
        Equal("the upstream agent stopped on a failure", "dotnet,Chat", failing.Get(StartupHook.Exclude));

        var unreadable = Started(new Fake { Version = new Version(9, 0), NamesFail = true });
        Equal("by the application's name when the main module cannot be read", "Chat", unreadable.Get(StartupHook.Exclude));

        var nameless = Started(new Fake { Version = new Version(9, 0), ProcessNamesList = { null, " " } });
        Equal(
            "no name to stop it by",
            "not started: .NET 9.0, where the kit is proved on 10.0; the upstream agent could not be stopped: the process has no name",
            nameless.Lines.Single());
        Check("no empty exclusion", nameless.Get(StartupHook.Exclude) == null);

        var existing = Started(new Fake { Version = new Version(9, 0), [StartupHook.Exclude] = "Worker.exe" });
        Equal("the workload's own exclusions kept", "Worker.exe,dotnet,Chat", existing.Get(StartupHook.Exclude));
    }

    private static void ChatLibrary()
    {
        Equal("missing", "Microsoft.Extensions.AI not found",
            StartupHook.ChatLibraryMissing(_ => throw new FileNotFoundException("Microsoft.Extensions.AI")));
        Equal("older", $"Microsoft.Extensions.AI {typeof(object).Assembly.GetName().Version} is older than 9.5",
            StartupHook.ChatLibraryMissing(_ => typeof(object).Assembly));
        Equal("present", null, StartupHook.ChatLibraryMissing(name => Assembly.Load(name)));
    }

    private static void Registrations()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient>(new Stub());
        services.AddSingleton<IChatClient>(_ => new Stub());
        services.AddSingleton<IChatClient, Stub>();
        services.AddKeyedSingleton<IChatClient>("instance", new Stub());
        services.AddKeyedSingleton<IChatClient>("factory", (_, _) => new Stub());
        services.AddKeyedSingleton<IChatClient, Routed>("support");
        var (wrapped, keyedTypes) = StartupHook.WrapChatClients(services);
        Equal("wrapped", "5", wrapped.ToString());
        Equal("a type under a key left as it is", "1", keyedTypes.ToString());
        using var provider = services.BuildServiceProvider();
        var all = provider.GetServices<IChatClient>().ToList();
        Equal("every unkeyed registration", "3", all.Count.ToString());
        Check("each wrapped", all.All(c => c.GetService(typeof(OpenTelemetryChatClient)) != null));
        Check("keyed instance wrapped", provider.GetRequiredKeyedService<IChatClient>("instance").GetService(typeof(OpenTelemetryChatClient)) != null);
        Check("keyed factory wrapped", provider.GetRequiredKeyedService<IChatClient>("factory").GetService(typeof(OpenTelemetryChatClient)) != null);
        try
        {
            var routed = provider.GetRequiredKeyedService<IChatClient>("support");
            Check("a type under a key gets its key, as without the kit", routed is Routed { Key: "support" });
        }
        catch (Exception e)
        {
            Check($"a type under a key resolves ({e.GetType().Name})", false);
        }
        var answer = all[0].GetResponseAsync("Where is my order?").GetAwaiter().GetResult();
        Equal("a wrapped client answers", "Hello from the stub.", answer.Text);
    }

    private static Fake Started(Fake fake)
    {
        StartupHook.Start(fake, Proved);
        return fake;
    }

    private static void Check(string name, bool ok)
    {
        if (!ok)
        {
            failures++;
            Console.WriteLine($"FAIL {name}");
        }
    }

    private static void Equal(string name, string? expected, string? actual)
    {
        if (expected != actual)
        {
            failures++;
            Console.WriteLine($"FAIL {name}\n  expected: {expected}\n  actual:   {actual}");
        }
    }

    private sealed class Fake : StartupHook.Machine
    {
        internal readonly Dictionary<string, string> Env = new() { [StartupHook.KitEndpoint] = Kit };
        internal readonly HashSet<string> Files = new();
        internal readonly List<string> Lines = new();
        internal readonly List<string?> ProcessNamesList = new();
        internal Version Version = new(10, 0, 1);
        internal string Rid = "linux-x64";
        internal bool Listened;
        internal bool ListenFails;
        internal bool NamesFail;

        internal string this[string name]
        {
            set => Env[name] = value;
        }

        internal Fake Without(string name)
        {
            Env.Remove(name);
            return this;
        }

        internal override string? Get(string name) => Env.TryGetValue(name, out var value) ? value : null;

        internal override void Set(string name, string value) => Env[name] = value;

        internal override IEnumerable<string> Names() => Env.Keys.ToList();

        internal override Version Runtime => Version;

        internal override string? RuntimeIdentifier => Rid;

        internal override bool FileExists(string path) => Files.Contains(path);

        internal override IEnumerable<string?> ProcessNames()
        {
            if (ProcessNamesList.Count > 0)
            {
                return ProcessNamesList;
            }
            return NamesFail ? new string?[] { null, "Chat" } : new string?[] { "dotnet", "Chat" };
        }

        internal override void Listen()
        {
            if (ListenFails)
            {
                throw new InvalidOperationException("the listener");
            }
            Listened = true;
        }

        internal override void Write(string line) => Lines.Add(line);
    }

    private class Stub : IChatClient
    {
        public Task<ChatResponse> GetResponseAsync(
            IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
            Task.FromResult(new ChatResponse(new ChatMessage(ChatRole.Assistant, "Hello from the stub.")));

        public IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(
            IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public object? GetService(Type serviceType, object? serviceKey = null) =>
            serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;

        public void Dispose()
        {
        }
    }

    private sealed class Routed : DelegatingChatClient
    {
        public Routed([ServiceKey] string key)
            : base(new Stub())
        {
            Key = key;
        }

        public string Key { get; }
    }
}
