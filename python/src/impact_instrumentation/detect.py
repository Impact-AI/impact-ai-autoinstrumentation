# SPDX-License-Identifier: Apache-2.0

"""Find out whether the workload already has a tracer of its own (AE-5: stand down).

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

_INFO_SUFFIXES = (".dist-info", ".egg-info")


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
    return sorted(findings, key=lambda f: f.order)
