// SPDX-License-Identifier: Apache-2.0

// The kit's .NET startup hook, as the proofs run it (dotnet/tests/image). It is not released yet.
//
// The OpenTelemetry Operator injects .NET by environment: CORECLR_PROFILER* for the upstream agent's profiler, and
// DOTNET_STARTUP_HOOKS for its startup hook, appended (with ':') to a value the workload or the Instrumentation already
// set. The Instrumentation's own env comes first, so a hook it names runs before the upstream agent's (only when the
// workload sets no DOTNET_STARTUP_HOOKS of its own: the Operator adds the Instrumentation's env where the workload has
// none). This one does two things there:
//
//   1. It decides whether the upstream agent starts. It stops it, through the agent's own switch
//      OTEL_DOTNET_AUTO_EXCLUDE_PROCESSES (set in this process only, before the agent's hook reads it), when the
//      workload already has a tracer (another profiler in CORECLR_PROFILER, or OpenTelemetry exporter settings of its
//      own), when the upstream profiler cannot load (the runtime annotation names another C library than the
//      workload's), and when anything here fails. On a .NET the kit is not proved on it does the same, except where the
//      upstream profiler is attached: there it turns the agent's signals off (SignalsOff says why). Otherwise it points
//      the agent at the kit's endpoint (IMPACT_OTLP_ENDPOINT).
//   2. It adds UseOpenTelemetry() to every IChatClient the application registers in its services, which is the call
//      Microsoft.Extensions.AI asks for in code. It listens for the host being built ("HostBuilding" on the
//      "Microsoft.Extensions.Hosting" diagnostic listener, the event the framework's own test host uses) and adds a
//      ConfigureServices step that wraps each registration. A client that is already wrapped is left as it is; so is
//      every client when the application does not have Microsoft.Extensions.AI 9.5 or newer, and a client registered
//      by type under a key (its constructor may take the key, which only the container can give).
//
// It is built for the oldest .NET the upstream agent supports (net8.0), so that it loads on every .NET the Operator
// injects, and it reads the .NET versions the kit is proved on from its own assembly (ProvedDotnet, set by the build).
// It never throws: an exception from a startup hook stops the application. Each step catches and writes one line to
// standard error, names only, never values.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

internal static class StartupHook
{
    // The upstream agent's profiler (CORECLR_PROFILER), as the Operator sets it.
    internal const string UpstreamProfilerId = "{918728DD-259F-4A6A-AC2B-B85E1B658318}";
    internal const string KitEndpoint = "IMPACT_OTLP_ENDPOINT";
    internal const string Exclude = "OTEL_DOTNET_AUTO_EXCLUDE_PROCESSES";
    private const string ChatClientType = "Microsoft.Extensions.AI.IChatClient";
    private const int MaxName = 100;

    // The Microsoft.Extensions.AI this hook is built against (Impact.StartupHook.csproj): UseOpenTelemetry() as it calls it.
    internal static readonly Version OldestChatLibrary = new Version(9, 5);

    // Settings a workload sets for an OpenTelemetry pipeline of its own, as the kit's Java agent counts them. The kit's
    // endpoint arrives as IMPACT_OTLP_ENDPOINT, so inside the kit any of these was set by the workload; a trace exporter
    // other than OTLP is the workload's anywhere.
    private static readonly string[] OwnSettings =
    {
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    };

    internal static readonly string[] Signals =
    {
        "OTEL_DOTNET_AUTO_TRACES_ENABLED",
        "OTEL_DOTNET_AUTO_METRICS_ENABLED",
        "OTEL_DOTNET_AUTO_LOGS_ENABLED",
    };

    private static readonly string[] OwnPrefixes = { "OTEL_EXPORTER_ZIPKIN_", "OTEL_EXPORTER_JAEGER_" };

    public static void Initialize()
    {
        Start(new Machine(), Proved());
    }

    // What the hook decides and does, on the process it runs in or on the self-test's (dotnet/hook-test).
    internal static void Start(Machine machine, string[] proved)
    {
        try
        {
            var outside = OutsideProved(machine.Runtime, proved);
            if (outside != null)
            {
                if (UpstreamProfiler(machine))
                {
                    SignalsOff(machine, $"not started: {outside}");
                }
                else
                {
                    StandDown(machine, $"not started: {outside}");
                }
                return;
            }
            var found = OtherTracer(machine);
            if (found != null)
            {
                StandDown(machine, $"stood down: {found}");
                return;
            }
            var unusable = ProfilerUnusable(machine);
            if (unusable != null)
            {
                StandDown(machine, $"not started: {unusable}");
                return;
            }
            var endpoint = machine.Get(KitEndpoint);
            if (!IsBlank(endpoint))
            {
                machine.Set("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint!.Trim());
            }
            machine.Listen();
            machine.Write("started");
        }
        catch (Exception e)
        {
            // The upstream agent's hook runs after this one and would start: a failure here stops it too.
            StandDown(machine, $"not started: {e.GetType().Name}");
        }
    }

    // The .NET versions the kit is proved on (major.minor, joined with +), from this assembly's ProvedDotnet metadata; none
    // if unreadable.
    internal static string[] Proved()
    {
        try
        {
            var value = typeof(StartupHook).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>()
                .FirstOrDefault(a => a.Key == "ProvedDotnet")?.Value ?? "";
            return value.Split(new[] { '+', ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries);
        }
        catch
        {
            return Array.Empty<string>();
        }
    }

    internal static string? OutsideProved(Version runtime, string[] proved)
    {
        var current = $"{runtime.Major}.{runtime.Minor}";
        if (proved.Contains(current))
        {
            return null;
        }
        return $".NET {current}, where the kit is proved on {(proved.Length == 0 ? "none" : string.Join(", ", proved))}";
    }

    // What the workload already runs, by name, or null.
    internal static string? OtherTracer(Machine machine)
    {
        var found = new List<string>();
        if ((machine.Get("CORECLR_ENABLE_PROFILING") ?? "").Trim() == "1")
        {
            var profiler = (machine.Get("CORECLR_PROFILER") ?? "").Trim();
            if (!string.Equals(profiler, UpstreamProfilerId, StringComparison.OrdinalIgnoreCase))
            {
                var path = ProfilerPath(machine) ?? "";
                var name = path.Length > 0 ? Path.GetFileName(path) : "a profiler";
                found.Add($"{Bounded(name)} (CORECLR_PROFILER)");
            }
        }
        var traces = (machine.Get("OTEL_TRACES_EXPORTER") ?? "").Trim().ToLowerInvariant();
        if (traces.Length > 0 && traces != "otlp")
        {
            found.Add("OTEL_TRACES_EXPORTER (set by the workload)");
        }
        var kit = machine.Get(KitEndpoint);
        if (!IsBlank(kit))
        {
            foreach (var variable in OwnSettings)
            {
                var value = machine.Get(variable);
                // The kit's own endpoint, which this hook sets and a process the workload starts inherits, is not the
                // workload's.
                if (IsBlank(value) || (variable == "OTEL_EXPORTER_OTLP_ENDPOINT" && value!.Trim() == kit!.Trim()))
                {
                    continue;
                }
                found.Add($"{variable} (set by the workload)");
            }
            var others = machine.Names()
                .Where(n => OwnPrefixes.Any(p => n.StartsWith(p, StringComparison.OrdinalIgnoreCase)))
                .Where(n => !IsBlank(machine.Get(n)))
                .OrderBy(n => n, StringComparer.Ordinal);
            foreach (var name in others)
            {
                found.Add($"{Bounded(name)} (set by the workload)");
            }
        }
        return found.Count > 0 ? string.Join(", ", found) : null;
    }

    // Whether the profiler the process starts with is the upstream agent's.
    internal static bool UpstreamProfiler(Machine machine) =>
        (machine.Get("CORECLR_ENABLE_PROFILING") ?? "").Trim() == "1"
        && string.Equals((machine.Get("CORECLR_PROFILER") ?? "").Trim(), UpstreamProfilerId, StringComparison.OrdinalIgnoreCase);

    // Why the upstream profiler the Operator named cannot load, or null. The runtime annotation only chooses the linux/
    // or linux-musl/ directory; a workload of the other C library starts without the profiler, and the agent with it.
    internal static string? ProfilerUnusable(Machine machine)
    {
        if (!UpstreamProfiler(machine))
        {
            return null;
        }
        var path = ProfilerPath(machine);
        if (path == null || !machine.FileExists(path))
        {
            return "the upstream profiler is not at CORECLR_PROFILER_PATH";
        }
        var directory = Path.GetFileName(Path.GetDirectoryName(path)) ?? "";
        var annotated = Libc(directory);
        var rid = machine.RuntimeIdentifier ?? "";
        var runs = Libc(rid);
        if (annotated != null && runs != null && annotated != runs)
        {
            return $"the runtime annotation names {annotated} ({Bounded(directory)}), the workload runs {runs} ({Bounded(rid)})";
        }
        return null;
    }

    private static string? Libc(string name) =>
        name.StartsWith("linux-musl", StringComparison.Ordinal) ? "musl"
        : name.StartsWith("linux", StringComparison.Ordinal) ? "glibc"
        : null;

    private static string? ProfilerPath(Machine machine)
    {
        var path = machine.Get("CORECLR_PROFILER_PATH_64");
        if (IsBlank(path))
        {
            path = machine.Get("CORECLR_PROFILER_PATH");
        }
        return IsBlank(path) ? null : path!.Trim();
    }

    // Stops the upstream agent in this process: its mandatory rule skips a process whose name is in
    // OTEL_DOTNET_AUTO_EXCLUDE_PROCESSES, by its main module's name or by the application's.
    internal static void StandDown(Machine machine, string line)
    {
        try
        {
            var names = machine.ProcessNames().Where(n => !IsBlank(n)).Select(n => n!.Trim()).Distinct().ToList();
            if (names.Count == 0)
            {
                machine.Write($"{line}; the upstream agent could not be stopped: the process has no name");
                return;
            }
            var excluded = machine.Get(Exclude);
            var all = IsBlank(excluded) ? names : new[] { excluded!.Trim() }.Concat(names);
            machine.Set(Exclude, string.Join(",", all));
            machine.Write(line);
        }
        catch (Exception e)
        {
            machine.Write($"{line}; the upstream agent could not be stopped: {e.GetType().Name}");
        }
    }

    // Turns the upstream agent's signals off where it cannot be stopped: on a .NET older than its own assemblies, its
    // profiler redirects the application's references to them (System.Diagnostics.DiagnosticSource 8.0 to 10.0) as the
    // process starts, and only the agent's loader, which the exclusion switch skips, can then provide them. Excluded
    // there, the application cannot start; with its signals off, the agent records and sends nothing.
    internal static void SignalsOff(Machine machine, string line)
    {
        try
        {
            foreach (var signal in Signals)
            {
                machine.Set(signal, "false");
            }
            machine.Write($"{line}; the upstream agent's signals are off, because its profiler is attached");
        }
        catch (Exception e)
        {
            machine.Write($"{line}; the upstream agent could not be switched off: {e.GetType().Name}");
        }
    }

    private static bool IsBlank(string? value) => string.IsNullOrWhiteSpace(value);

    private static string Bounded(string name) => name.Length > MaxName ? name.Substring(0, MaxName) : name;

    internal static void Write(string line)
    {
        try
        {
            Console.Error.WriteLine($"impact.ai: {line}");
        }
        catch
        {
            // nowhere to write
        }
    }

    // The process the hook runs in. The self-test (dotnet/hook-test) passes one of its own.
    internal class Machine
    {
        internal virtual string? Get(string name) => Environment.GetEnvironmentVariable(name);

        internal virtual void Set(string name, string value) => Environment.SetEnvironmentVariable(name, value);

        internal virtual IEnumerable<string> Names() => Environment.GetEnvironmentVariables().Keys.OfType<string>();

        internal virtual Version Runtime => Environment.Version;

        internal virtual string? RuntimeIdentifier => RuntimeInformation.RuntimeIdentifier;

        internal virtual bool FileExists(string path) => File.Exists(path);

        internal virtual IEnumerable<string?> ProcessNames()
        {
            var names = new List<string?>();
            try
            {
                names.Add(Process.GetCurrentProcess().MainModule?.ModuleName);
            }
            catch
            {
                // a process whose modules cannot be read: the application's name below
            }
            try
            {
                names.Add(AppDomain.CurrentDomain.FriendlyName);
            }
            catch
            {
                // none
            }
            return names;
        }

        internal virtual void Listen() => DiagnosticListener.AllListeners.Subscribe(new Listeners());

        internal virtual void Write(string line) => StartupHook.Write(line);
    }

    // Every DiagnosticListener of the process: the hosting one, when the application builds a host.
    private sealed class Listeners : IObserver<DiagnosticListener>
    {
        public void OnNext(DiagnosticListener listener)
        {
            if (listener.Name == "Microsoft.Extensions.Hosting")
            {
                listener.Subscribe(new HostEvents());
            }
        }

        public void OnError(Exception error)
        {
        }

        public void OnCompleted()
        {
        }
    }

    private sealed class HostEvents : IObserver<KeyValuePair<string, object?>>
    {
        public void OnNext(KeyValuePair<string, object?> value)
        {
            if (value.Key != "HostBuilding" || value.Value == null)
            {
                return;
            }
            try
            {
                AddConfigureServices(value.Value);
            }
            catch (Exception e)
            {
                Write($"chat clients not wrapped: {e.GetType().Name}");
            }
        }

        public void OnError(Exception error)
        {
        }

        public void OnCompleted()
        {
        }
    }

    // IHostBuilder.ConfigureServices(Action<HostBuilderContext, IServiceCollection>), by reflection: the hook does not
    // reference Microsoft.Extensions.Hosting, which a console application may not have.
    private static void AddConfigureServices(object builder)
    {
        var method = builder.GetType().GetInterfaces()
            .Where(i => i.FullName == "Microsoft.Extensions.Hosting.IHostBuilder")
            .Select(i => i.GetMethod("ConfigureServices"))
            .FirstOrDefault(m => m != null && m.GetParameters().Length == 1);
        if (method == null)
        {
            Write("chat clients not wrapped: the host has no ConfigureServices");
            return;
        }
        var action = method.GetParameters()[0].ParameterType;
        var context = action.GetGenericArguments()[0];
        var configure = typeof(StartupHook)
            .GetMethod(nameof(ConfigureServices), BindingFlags.NonPublic | BindingFlags.Static)!
            .MakeGenericMethod(context);
        method.Invoke(builder, new object[] { Delegate.CreateDelegate(action, configure) });
    }

    private static void ConfigureServices<TContext>(TContext context, IServiceCollection services)
    {
        try
        {
            // Only an application that registers a chat client has Microsoft.Extensions.AI's abstractions: look by name
            // first, so that their types are loaded only then.
            if (!services.Any(d => d.ServiceType.FullName == ChatClientType))
            {
                return;
            }
            var missing = ChatLibraryMissing(name => Assembly.Load(name));
            if (missing != null)
            {
                Write($"chat clients not wrapped: {missing}");
                return;
            }
            var (wrapped, keyedTypes) = WrapChatClients(services);
            Write(keyedTypes == 0
                ? $"chat clients wrapped: {wrapped}"
                : $"chat clients wrapped: {wrapped}; not wrapped: {keyedTypes} registered by type under a key");
        }
        catch (Exception e)
        {
            Write($"chat clients not wrapped: {e.GetType().Name}");
        }
    }

    // Why UseOpenTelemetry() cannot be called, or null. It is in Microsoft.Extensions.AI, which an application that
    // registers a client with the abstractions and a provider's package alone does not have; the hook ships no copy.
    internal static string? ChatLibraryMissing(Func<AssemblyName, Assembly> load)
    {
        Assembly library;
        try
        {
            library = load(new AssemblyName("Microsoft.Extensions.AI"));
        }
        catch (Exception)
        {
            return "Microsoft.Extensions.AI not found";
        }
        var version = library.GetName().Version;
        if (version == null || version < OldestChatLibrary)
        {
            return $"Microsoft.Extensions.AI {version} is older than {OldestChatLibrary}";
        }
        if (library.GetType("Microsoft.Extensions.AI.OpenTelemetryChatClient") == null
            || library.GetType("Microsoft.Extensions.AI.ChatClientBuilder") == null)
        {
            return $"Microsoft.Extensions.AI {version} has no OpenTelemetryChatClient";
        }
        return null;
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    internal static (int Wrapped, int KeyedTypes) WrapChatClients(IServiceCollection services)
    {
        var wrapped = 0;
        var keyedTypes = 0;
        for (var i = 0; i < services.Count; i++)
        {
            var d = services[i];
            if (d.ServiceType != typeof(IChatClient))
            {
                continue;
            }
            if (d.IsKeyedService && d.KeyedImplementationType != null)
            {
                keyedTypes++;
                continue;
            }
            services[i] = d.IsKeyedService ? Keyed(d) : Unkeyed(d);
            wrapped++;
        }
        return (wrapped, keyedTypes);
    }

    private static ServiceDescriptor Unkeyed(ServiceDescriptor d)
    {
        Func<IServiceProvider, object> inner;
        if (d.ImplementationFactory != null)
        {
            inner = d.ImplementationFactory;
        }
        else if (d.ImplementationInstance != null)
        {
            var instance = d.ImplementationInstance;
            inner = _ => instance;
        }
        else
        {
            var type = d.ImplementationType!;
            inner = sp => ActivatorUtilities.CreateInstance(sp, type);
        }
        return new ServiceDescriptor(typeof(IChatClient), sp => Guarded(inner(sp), sp), d.Lifetime);
    }

    private static ServiceDescriptor Keyed(ServiceDescriptor d)
    {
        Func<IServiceProvider, object?, object> inner;
        if (d.KeyedImplementationFactory != null)
        {
            inner = d.KeyedImplementationFactory;
        }
        else
        {
            var instance = d.KeyedImplementationInstance!;
            inner = (_, _) => instance;
        }
        return new ServiceDescriptor(
            typeof(IChatClient), d.ServiceKey, (sp, key) => Guarded(inner(sp, key), sp), d.Lifetime);
    }

    // The client the application built, wrapped; the client as it was when wrapping fails. Wrap is a method of its own,
    // never inlined, so that a Microsoft.Extensions.AI that cannot be loaded fails when Wrap is compiled, inside this try.
    private static object Guarded(object client, IServiceProvider services)
    {
        try
        {
            return Wrap((IChatClient)client, services);
        }
        catch (Exception e)
        {
            Write($"chat client not wrapped: {e.GetType().Name}");
            return client;
        }
    }

    // The client inside UseOpenTelemetry(): the spans of the GenAI conventions on the "Experimental.Microsoft.Extensions.AI"
    // source, content as OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT says.
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static IChatClient Wrap(IChatClient client, IServiceProvider services)
    {
        if (client.GetService(typeof(OpenTelemetryChatClient)) != null)
        {
            return client;
        }
        return new ChatClientBuilder(client)
            .UseOpenTelemetry(services.GetService<ILoggerFactory>())
            .Build(services);
    }
}
