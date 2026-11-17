# SPDX-License-Identifier: Apache-2.0

"""Find out whether the workload already has a tracer of its own (stand down).

This runs from sitecustomize, before any application code. It therefore reads directory listings, the command line
and environment variable names only: it never imports a candidate package and uses the standard library alone.
"""

from __future__ import annotations

import os
from collections import namedtuple
from collections.abc import Iterable, Mapping, Sequence

MAX_DETAIL = 200

# Distribution names (PEP 503 normalized) of tracers that start themselves in the application's own code or
# configuration. Finding one in the application's environment means the application owns its tracing.
TRACER_DISTRIBUTIONS = frozenset(
    {
        "ddtrace",
        "traceloop-sdk",
        "langfuse",
        "openlit",
        "logfire",
        "agentops",
        "arize-phoenix-otel",
        "newrelic",
        "elastic-apm",
        "splunk-opentelemetry",
        "aws-opentelemetry-distro",
        "azure-monitor-opentelemetry",
        "honeycomb-opentelemetry",
        "openinference-instrumentation",
        "elastic-opentelemetry",
        "arize-otel",
        # Impact AI's own SDK: an application that uses it sends its traces itself, so the package does not trace
        # the same calls a second time.
        "impact",
    }
)

# OpenLLMetry's per-provider packages (the family Traceloop starts). Contrib packages such as
# opentelemetry-instrumentation-openai-v2 or the -genai- ones this image ships have different names.
OPENLLMETRY_PROVIDERS = frozenset(
    {
        "openai",
        "anthropic",
        "bedrock",
        "cohere",
        "mistralai",
        "ollama",
        "groq",
        "together",
        "replicate",
        "watsonx",
        "sagemaker",
        "vertexai",
        "google-generativeai",
        "langchain",
        "llamaindex",
        "haystack",
        "crewai",
        "transformers",
        "alephalpha",
        "openai-agents",
        "mcp",
    }
)
OPENLLMETRY_PREFIX = "opentelemetry-instrumentation-"
OPENINFERENCE_PREFIX = "openinference-instrumentation-"

# Top-level packages that mark a tracer even when no distribution metadata is present (Datadog's injector puts
# ddtrace on PYTHONPATH from /datadog-lib).
TRACER_MODULES = {
    "ddtrace": "ddtrace",
    "traceloop": "traceloop-sdk",
    "langfuse": "langfuse",
    "openlit": "openlit",
}

LAUNCHERS = {
    "opentelemetry-instrument": "opentelemetry-instrument",
    "ddtrace-run": "ddtrace-run",
    "newrelic-admin": "newrelic-admin",
    "splunk-py-trace": "splunk-py-trace",
}
LAUNCHER_MODULES = {
    "opentelemetry.instrumentation.auto_instrumentation": "opentelemetry-instrument",
    "ddtrace.commands.ddtrace_run": "ddtrace-run",
}

# The exporter settings a workload sets when it runs its own OpenTelemetry pipeline. OTEL_EXPORTER_OTLP_PROTOCOL and
# OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp are set by the Operator itself and are not signals.
EXPORTER_SETTINGS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
)
EXPORTER_PREFIXES = ("OTEL_EXPORTER_ZIPKIN_", "OTEL_EXPORTER_JAEGER_")

KIT_ENDPOINT = "IMPACT_OTLP_ENDPOINT"

# LangSmith: its default tracing uses callbacks and wrappers and leaves the OpenTelemetry provider alone, so the package
# starts beside it. Its OpenTelemetry mode makes the SDK own the process's provider and export OTLP itself, and then the
# workload owns its tracing. The SDK reads each setting under both prefixes, LANGSMITH_ first; TRACING_MODE (langsmith,
# otel or hybrid) decides when it is set, else OTEL_ONLY or OTEL_ENABLED switch the mode on ("true" or "1"). The values
# are read here and never leave the process: a finding names the setting.
LANGSMITH_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")
LANGSMITH_OTEL_MODES = frozenset({"otel", "hybrid"})
LANGSMITH_OTEL_SWITCHES = ("OTEL_ONLY", "OTEL_ENABLED")

_INFO_SUFFIXES = (".dist-info", ".egg-info")

# The tool a finding shows, by the source ids impact.ai's service knows, and the distribution whose version is the
# tool's. A finding of no tool here (an OpenLLMetry or OpenInference instrumentation on its own, which several tools
# start) names none: impact.ai reads the workload as owned by another tracer.
TOOLS = {
    "ddtrace": "datadog",
    "ddtrace-run": "datadog",
    "traceloop-sdk": "traceloop",
    "langfuse": "langfuse",
    "openlit": "openlit",
    "logfire": "logfire",
    "arize-phoenix-otel": "phoenix",
    "arize-otel": "arize-ax",
    "newrelic": "newrelic",
    "newrelic-admin": "newrelic",
    "splunk-opentelemetry": "splunk-otel",
    "splunk-py-trace": "splunk-otel",
    "honeycomb-opentelemetry": "honeycomb",
    "elastic-opentelemetry": "elastic-edot",
    "opentelemetry-instrument": "opentelemetry",
}
TOOL_DISTRIBUTIONS = {
    "datadog": "ddtrace",
    "traceloop": "traceloop-sdk",
    "langfuse": "langfuse",
    "openlit": "openlit",
    "logfire": "logfire",
    "phoenix": "arize-phoenix-otel",
    "arize-ax": "arize-otel",
    "newrelic": "newrelic",
    "splunk-otel": "splunk-opentelemetry",
    "honeycomb": "honeycomb-opentelemetry",
    "elastic-edot": "elastic-opentelemetry",
    "opentelemetry": "opentelemetry-sdk",
    "langsmith": "langsmith",
}
# The exporter kind of an OpenTelemetry SDK's traces, from OTEL_TRACES_EXPORTER and the OTLP protocol it reads. Only
# these words leave the process, never a setting's value.
TRACES_EXPORTERS = frozenset({"zipkin", "console"})
OTLP_PROTOCOLS = {"grpc": "otlp-grpc", "http/protobuf": "otlp-http", "http/json": "otlp-http"}
LANGSMITH_EXPORTER = "otlp-http"  # LangSmith's OpenTelemetry mode exports OTLP over HTTP, whatever else is set
MAX_TOOL_TEXT = 64


class Finding(namedtuple("Finding", ["kind", "name", "version"], defaults=[None])):
    """One thing that shows the workload owns its tracing.

    kind: launcher | distribution | module | environment. (A namedtuple, not a dataclass: this module runs in every
    Python process of the container and dataclasses would import inspect.)
    """

    __slots__ = ()

    def describe(self) -> str:
        if self.kind == "launcher":
            return f"{self.name} (launcher)"
        if self.kind == "environment":
            return f"{self.name} (set by the workload)"
        return f"{self.name} {self.version}" if self.version else self.name

    @property
    def order(self) -> tuple[int, str]:
        """Launchers, then the tracers, then the per-provider packages a tracer brings, then the environment.

        The description is cut at MAX_DETAIL and a tracer such as Traceloop installs a dozen packages that sort before
        its own name, so the tracer has to come first or the cut would remove it.
        """
        rank = {"launcher": 0, "distribution": 1, "module": 1, "environment": 3}[self.kind]
        if self.kind == "distribution" and _is_provider_package(self.name):
            rank = 2
        return (rank, self.name)


def normalize(name: str) -> str:
    """PEP 503: runs of '-', '_' and '.' become one '-', lower case. (No `re`: it is imported in every process.)"""
    out = name.lower().replace("_", "-").replace(".", "-")
    while "--" in out:
        out = out.replace("--", "-")
    return out


def _parse_info_name(entry_name: str) -> tuple[str, str] | None:
    """('name', 'version') from 'name-1.2.3.dist-info' or 'name-1.2.3-py3.11.egg-info'."""
    for suffix in _INFO_SUFFIXES:
        if entry_name.endswith(suffix):
            stem = entry_name[: -len(suffix)]
            break
    else:
        return None
    # The version starts at the first '-' that is followed by a digit.
    for index in range(1, len(stem) - 1):
        if stem[index] == "-" and stem[index + 1].isdigit():
            name, rest = stem[:index], stem[index + 1 :]
            version = rest.split("-", 1)[0]  # drop '-py3.11' of egg-info names
            return name, version
    return None


def describe(findings: Iterable[Finding]) -> str:
    text = ", ".join(f.describe() for f in sorted(findings, key=lambda f: f.order))
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 3] + "..."


def _is_provider_package(name: str) -> bool:
    """One of the per-provider packages of OpenLLMetry or OpenInference (not their umbrella packages)."""
    return name not in TRACER_DISTRIBUTIONS and name.startswith((OPENLLMETRY_PREFIX, OPENINFERENCE_PREFIX))


def _is_tracer_distribution(name: str) -> bool:
    if name in TRACER_DISTRIBUTIONS:
        return True
    if name.startswith(OPENLLMETRY_PREFIX) and name[len(OPENLLMETRY_PREFIX) :] in OPENLLMETRY_PROVIDERS:
        return True
    return name.startswith(OPENINFERENCE_PREFIX)


def _same_path(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


Scan = namedtuple("Scan", ["dists", "modules"])  # dists: {normalized name: version}; modules: tracer modules found


def _scan_one(path: str) -> tuple[dict[str, str | None], set[str]]:
    dists: dict[str, str | None] = {}
    modules: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                parsed = _parse_info_name(entry.name)
                if parsed:
                    dists.setdefault(normalize(parsed[0]), parsed[1])
                elif entry.name in TRACER_MODULES and entry.is_dir():
                    modules.add(entry.name)
    except OSError:
        pass
    return dists, modules


def scan(sys_path: Iterable[str], own_roots: Iterable[str]) -> Scan:
    """Every distribution installed in the application's own environment, by directory listing (nothing is imported).

    The first occurrence of a name wins, as it does for imports. Our own directories are left out.
    """
    own = [r for r in own_roots if r]
    dists: dict[str, str | None] = {}
    modules: set[str] = set()
    for path in sys_path:
        if not path or any(_same_path(path, root) for root in own):
            continue
        found_dists, found_modules = _scan_one(path)
        for name, version in found_dists.items():
            dists.setdefault(name, version)
        modules |= found_modules
    return Scan(dists, modules)


_scan = scan  # find_foreign has a parameter of the same name


def _launchers(argv: Sequence[str], orig_argv: Sequence[str] | None) -> list[Finding]:
    found: dict[str, Finding] = {}
    if argv:
        base = os.path.basename(argv[0])
        base = base[:-3] if base.endswith(".py") else base
        name = LAUNCHERS.get(base)
        if name:
            found[name] = Finding("launcher", name)
    if orig_argv:
        for i, arg in enumerate(orig_argv[:-1]):
            if arg == "-m" and orig_argv[i + 1] in LAUNCHER_MODULES:
                name = LAUNCHER_MODULES[orig_argv[i + 1]]
                found[name] = Finding("launcher", name)
    return list(found.values())


def _environment(environ: Mapping[str, str], app_has_otel_sdk: bool) -> list[Finding]:
    found = []
    traces_exporter = (environ.get("OTEL_TRACES_EXPORTER") or "").strip().lower()
    if traces_exporter and traces_exporter != "otlp":
        found.append(Finding("environment", "OTEL_TRACES_EXPORTER"))
    # Inside the cluster kit our own endpoint is IMPACT_OTLP_ENDPOINT, so any OTEL_EXPORTER_* setting is the
    # workload's. It only means a pipeline of its own when the workload also ships the OpenTelemetry SDK.
    if environ.get(KIT_ENDPOINT) and app_has_otel_sdk:
        names = [n for n in EXPORTER_SETTINGS if environ.get(n)]
        names += sorted(n for n in environ if n.startswith(EXPORTER_PREFIXES) and environ.get(n))
        found += [Finding("environment", n) for n in names]
    return found


def _langsmith_setting(environ: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """(variable, value) of a LangSmith setting as its SDK reads it: the first prefix whose value is not blank."""
    for prefix in LANGSMITH_PREFIXES:
        value = environ.get(prefix + name)
        if value is not None and value.strip():
            return prefix + name, value.strip()
    return None


def _langsmith_otel_mode(environ: Mapping[str, str]) -> list[Finding]:
    """LangSmith's OpenTelemetry mode, switched on in the environment: the setting that switched it on."""
    mode = _langsmith_setting(environ, "TRACING_MODE")
    if mode is not None:
        return [Finding("environment", mode[0])] if mode[1].lower() in LANGSMITH_OTEL_MODES else []
    for name in LANGSMITH_OTEL_SWITCHES:
        switch = _langsmith_setting(environ, name)
        if switch is not None and (switch[1].lower() == "true" or switch[1] == "1"):
            return [Finding("environment", switch[0])]
    return []


def _langsmith_tracing(environ: Mapping[str, str]) -> bool:
    """LangSmith's tracing is switched on in the environment, as its SDK reads it: TRACING_V2, else TRACING, each under
    LANGSMITH_ then LANGCHAIN_, equal to "true"."""
    for name in ("TRACING_V2", "TRACING"):
        for prefix in LANGSMITH_PREFIXES:
            value = environ.get(prefix + name)
            if value is not None:
                return value == "true"
    return False


class Tool(namedtuple("Tool", ["id", "version", "exporter"], defaults=[None, None])):
    """The tool beside the package, as the setup report names it: its id, its version, and the kind of exporter its
    provider uses when the names and settings show it. Never a setting's value."""

    __slots__ = ()


def finding_tool(finding: Finding) -> str | None:
    """The tool id a finding shows, or None."""
    if finding.kind == "environment":
        if finding.name.startswith(LANGSMITH_PREFIXES):
            return "langsmith"
        return "opentelemetry" if finding.name.startswith("OTEL_") else None
    return TOOLS.get(finding.name)  # a launcher, a distribution, or a module by its distribution's name


def _printable_word(text: str | None) -> str | None:
    """The text when it is a name or version impact.ai keeps (a letter or digit first, then letters, digits and ._+!-,
    at most 64 characters); else None. (No `re`: this runs in every process.)"""
    if not text or len(text) > MAX_TOOL_TEXT or not text[0].isascii() or not text[0].isalnum():
        return None
    return text if all(c.isascii() and (c.isalnum() or c in "._+!-") for c in text) else None


def _otel_exporter(environ: Mapping[str, str]) -> str | None:
    """The kind of exporter an OpenTelemetry SDK configured by the environment sends its traces with, or None."""
    names = [n.strip().lower() for n in (environ.get("OTEL_TRACES_EXPORTER") or "").split(",") if n.strip()]
    if names and names[0] in TRACES_EXPORTERS:
        return names[0]
    if names and names[0] != "otlp":
        return None
    if any(environ.get(n) for n in environ if n.startswith("OTEL_EXPORTER_ZIPKIN_")):
        return "zipkin"
    protocol = environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or ""
    return OTLP_PROTOCOLS.get(protocol.strip().lower())


def tool(findings: Sequence[Finding], dists: Mapping[str, str | None], environ: Mapping[str, str]) -> Tool | None:
    """The tool the setup report names: that of the first finding which shows one (a LangSmith setting only when the
    langsmith package is installed), else, when there is no finding at all, LangSmith when its default tracing is on (it
    leaves the OpenTelemetry provider alone, so it is no finding), else None. A tracer of no known tool is named only in
    the detail: LangSmith's tracing beside it does not make LangSmith its owner."""
    installed = "langsmith" in dists
    found = next((t for t in map(finding_tool, findings) if t and (t != "langsmith" or installed)), None)
    if found is None:
        if not findings and installed and _langsmith_tracing(environ):
            return Tool("langsmith", _printable_word(dists["langsmith"]))
        return None
    version = _printable_word(dists.get(TOOL_DISTRIBUTIONS.get(found, "")))
    exporter = None
    if found == "langsmith":
        exporter = LANGSMITH_EXPORTER
    elif found == "opentelemetry":
        exporter = _otel_exporter(environ)
    return Tool(found, version, exporter)


def find_foreign(
    *,
    environ: Mapping[str, str],
    sys_path: Iterable[str],
    argv: Sequence[str],
    own_roots: Iterable[str],
    orig_argv: Sequence[str] | None = None,
    scan: Scan | None = None,
) -> list[Finding]:
    """Everything that shows the workload runs a tracer of its own; empty when it is safe to start."""
    dists, modules = scan if scan is not None else _scan(sys_path, own_roots)

    findings = _launchers(argv, orig_argv)
    for name, version in dists.items():
        if _is_tracer_distribution(name):
            findings.append(Finding("distribution", name, version))
    seen = {f.name for f in findings}
    for module in sorted(modules):
        label = TRACER_MODULES[module]
        if label not in seen:
            findings.append(Finding("module", label))
    findings += _environment(environ, "opentelemetry-sdk" in dists)
    findings += _langsmith_otel_mode(environ)
    return sorted(findings, key=lambda f: f.order)
