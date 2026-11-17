# SPDX-License-Identifier: Apache-2.0

"""Arm the instrumentation at process start; apply it when the application imports the library.

Every Python process of an injected container starts this package, including exec probes and helper processes. Eager
instrumentation imports OpenAI, Anthropic, boto3 and the web frameworks (seconds, in every process, whether or not it
uses them) and would stall a process with a one-second probe timeout. So nothing heavy happens at start: for each
instrumentation of the payload whose library is installed (found by listing directories, see detect.scan) a
post-import hook is registered, and the instrumentor is applied when, and only if, the application imports that library.
The supported version range is checked then, with `packaging`, in the one process that needs it.

The tracer provider is created on the first hook and is private: no global tracer, meter or logger provider is set,
and only the instrumentors this payload ships are applied (never the application's own). A GenAI instrumentation gets a
logger provider that sends nothing, and what any instrumentor sets in the process's environment while it instruments is
put back (RESTORED_ENVIRONMENT).
"""

from __future__ import annotations

import importlib
import logging
import os
from collections import namedtuple
from collections.abc import Callable, Mapping

from impact_instrumentation import defaults, hooks
from impact_instrumentation._version import __version__
from impact_instrumentation.detect import normalize

log = logging.getLogger("impact_instrumentation")

# name: the entry point name (openai, fastapi, ...); module, cls: where the instrumentor lives; libraries: the
# top-level modules whose import applies it; required / any_of: ((distribution, specifier, installed version), ...).
Spec = namedtuple("Spec", ["name", "module", "cls", "libraries", "required", "any_of"])


# The module an application imports where it differs from the name of its distribution. google-genai is the package
# google.genai inside the namespace package `google`, which other distributions share: its hook waits for google.genai.
MODULES = {"openai-agents": "agents", "google-genai": "google.genai"}

# Libraries that also arm an instrumentation, beside those of its own `instruments` extra. The LangChain
# instrumentation listens to langchain-core's callbacks, and a LangGraph application often has langchain-core (and
# langchain-openai) but not langchain. The floor is the langchain-core that langchain 0.3.22, the instrumentation's own
# floor, requires.
ALTERNATIVES = {"langchain": (("langchain-core", "<2,>=0.3.49"),)}

# Variables of the process's environment that an instrumentor sets while it instruments, and that the package puts back
# as they were: the environment holds only what the application and the package chose. Google's GenAI instrumentation
# sets OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT=true, which turns on log events in every GenAI instrumentation of the
# process (OpenAI's and Anthropic's too), overrides an application that turned them off, and passes to its children.
RESTORED_ENVIRONMENT = ("OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT",)

# The instrumentations built on OpenTelemetry's GenAI utility, which take a logger provider for their log events. Only
# they are given one: a web framework's instrumentation passes on what it is given, and FastAPI's fails on a keyword it
# does not know when the application makes its app.
GENAI_UTILITY = ("opentelemetry.instrumentation.genai.", "opentelemetry.instrumentation.google_genai")


def registry():
    """The instrumentations the payload ships (generated at build time); none when run from a source tree."""
    try:
        from impact_instrumentation import _registry
    except ImportError:
        return ()
    return _registry.INSTRUMENTATIONS


def _library(distribution: str) -> str:
    name = normalize(distribution)
    return MODULES.get(name, name.replace("-", "_"))


def plan(*, scan, environ: Mapping[str, str]) -> list[Spec]:
    """The instrumentations to arm: those whose libraries are installed. Imports nothing of the application."""
    disabled = defaults.disabled_instrumentations(environ)
    armed = []
    for name, module, cls, required, any_of in registry():
        if "*" in disabled or name in disabled:
            continue
        if name in ALTERNATIVES:
            required, any_of = (), (*required, *any_of, *ALTERNATIVES[name])
        found_required = [(lib, spec, scan.dists.get(normalize(lib))) for lib, spec in required]
        found_any = [(lib, spec, scan.dists.get(normalize(lib))) for lib, spec in any_of]
        found_any = [item for item in found_any if item[2] is not None]
        if any(version is None for _, _, version in found_required) or (any_of and not found_any):
            continue  # a library that is not installed is the normal case (a Flask app has no Django)
        libraries = tuple(dict.fromkeys(_library(lib) for lib, _, _ in found_required + found_any))
        armed.append(Spec(name, module, cls, libraries, tuple(found_required), tuple(found_any)))
    return sorted(armed, key=lambda spec: spec.name)


def _below(version) -> str:
    """The newest series under an exclusive upper bound, as the report says it: '3' under 4, '0.4' under 0.5, '0.x'
    under 1, '1.4.x' under 1.5; 'below 2.5.1' where there is no such series."""
    release = list(version.release)
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if len(release) == 1 and release[0] > 1:
        return str(release[0] - 1)
    if len(release) == 1 and release[0] == 1:
        return "0.x"
    if len(release) == 2 and release[0] == 0 and release[1] > 0:
        return f"0.{release[1] - 1}"
    if len(release) == 2 and release[1] > 0:
        return f"{release[0]}.{release[1] - 1}.x"
    return f"below {version}"


def spoken_specifier(specifier: str) -> str:
    """A version range as a sentence reads it, with no comparison operator and no comma: '1.26.0 to 3' (>=1.26.0,<4),
    '0.51.0 to 2', '2.0 and later', '0.92 to 0.x' (~=0.92), 'any version' (none)."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    low = high = None
    exact: list[str] = []
    others: list[str] = []
    for clause in SpecifierSet(specifier):
        version = clause.version.removesuffix(".*")
        if clause.operator in (">=", "~="):
            low = version
            if clause.operator == "~=":
                parts = [int(part) for part in version.split(".")]
                parts = [*parts[:-2], parts[-2] + 1] if len(parts) > 1 else parts
                high = _below(Version(".".join(map(str, parts))))
        elif clause.operator == ">":
            low = f"after {version}"
        elif clause.operator == "<":
            high = _below(Version(version))
        elif clause.operator == "<=":
            high = version
        elif clause.operator in ("==", "==="):
            exact.append(version)
        elif clause.operator == "!=":
            others.append(f"except {version}")
    if exact:
        said = " or ".join(exact)
    elif low and high:
        said = f"{low} to {high}"
    elif low:
        said = f"{low} and later"
    elif high:
        said = high if high.startswith("below") else f"up to {high}"
    else:
        said = "any version"
    return " ".join([said, *others])


def unsupported(spec: Spec) -> str:
    """What Impact AI traces and what the workload runs, when the installed libraries are outside the range this
    instrumentation supports (inside a series the qualification proves, a release can still be older than the
    instrumentation's floor); empty when they are inside. Worded as the qualification words its own ranges: no
    comparison operator and no comma, so that impact.ai reads it as a range reason ('Impact AI traces openai 1.26.0 to
    3; this workload runs openai 1.20.0')."""
    from packaging.specifiers import SpecifierSet

    def inside(item) -> bool:
        _, specifier, version = item
        return not specifier or SpecifierSet(specifier).contains(version, prereleases=True)

    required = [item for item in spec.required if not inside(item)]
    either = list(spec.any_of) if spec.any_of and not any(inside(item) for item in spec.any_of) else []
    if not required and not either:
        return ""
    traces = [f"{lib} {spoken_specifier(specifier)}" for lib, specifier, _ in required]
    if either:
        traces.append(" or ".join(f"{lib} {spoken_specifier(specifier)}" for lib, specifier, _ in either))
    runs = " and ".join(f"{lib} {version}" for lib, _, version in required + either)
    return f"Impact AI traces {' and '.join(traces)}; this workload runs {runs}"


class Hooks:
    """Applies armed instrumentors on first import of their library, behind one private tracer provider."""

    def __init__(
        self,
        environ: Mapping[str, str],
        on_failure: Callable[[BaseException], None],
        on_skipped: Callable[[list[str], dict[str, str]], None],
    ) -> None:
        import threading

        self.environ = environ
        self.on_failure = on_failure
        self.on_skipped = on_skipped
        self.lock = threading.RLock()
        self.provider = None
        self.processor = None
        self.broken = False
        self.applied: list[str] = []
        self.skipped: dict[str, str] = {}

    def _provider(self):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from impact_instrumentation import otlp
        from impact_instrumentation.agent_names import AgentNames
        from impact_instrumentation.business_context import BusinessContext, guard_callers
        from impact_instrumentation.exporter import ImpactSpanExporter
        from impact_instrumentation.processor import BoundedBatchSpanProcessor

        if self.provider is None:
            attributes = {"telemetry.distro.name": "impact-instrumentation", "telemetry.distro.version": __version__}
            # Where the process runs (a Lambda function's name and region), unless OTEL_RESOURCE_ATTRIBUTES says it.
            declared = otlp.resource_attributes(self.environ)
            for key, value in otlp.platform_attributes(self.environ).items():
                if declared.get(key) == value:
                    attributes[key] = value
            service = otlp.default_service_name(self.environ)
            if service:
                attributes["service.name"] = service
            provider = TracerProvider(resource=Resource.create(attributes))
            provider.add_span_processor(AgentNames())
            provider.add_span_processor(BusinessContext())
            exporter = ImpactSpanExporter(environ=self.environ)
            processor = BoundedBatchSpanProcessor(exporter, environ=self.environ)
            provider.add_span_processor(processor)
            self.processor = processor
            try:
                # Before the first instrumentor: a caller's user, session and use case stay out of the request's
                # baggage unless the workload trusts its callers (business_context.py).
                guard_callers(self.environ)
            except Exception as exc:  # noqa: BLE001 - the spans go on without it, as they would without the kit
                log.warning("caller baggage is not filtered: %s: %s", type(exc).__name__, exc)
            self.provider = provider
        return self.provider

    def apply(self, spec: Spec, _module) -> None:
        """The post-import hook. Whatever happens here, the application's import must go on."""
        try:
            with self.lock:
                if self.broken or spec.name in self.applied or spec.name in self.skipped:
                    return
                why = unsupported(spec)
                if why:
                    self.skipped[spec.name] = why
                    log.warning("%s is not instrumented: %s", spec.name, why)
                    self.on_skipped(sorted(self.applied), dict(self.skipped))
                    return
                try:
                    provider = self._provider()
                except Exception as exc:  # noqa: BLE001 - the SDK cannot start: stop for good and say why
                    self.broken = True
                    log.warning("the instrumentation cannot start: %s: %s", type(exc).__name__, exc)
                    self.on_failure(exc)
                    return
                saved = {name: os.environ.get(name) for name in RESTORED_ENVIRONMENT}
                try:
                    options = {"tracer_provider": provider, "skip_dep_check": True}
                    if spec.module.startswith(GENAI_UTILITY):
                        # The package sends no logs: a GenAI instrumentation's log events go nowhere, never to a logger
                        # provider the application set globally for its own.
                        options["logger_provider"] = _no_logs()
                    getattr(importlib.import_module(spec.module), spec.cls)().instrument(**options)
                except Exception as exc:  # noqa: BLE001 - one broken instrumentor must not stop the others
                    log.warning("could not instrument %s: %s: %s", spec.name, type(exc).__name__, exc)
                    return
                finally:
                    _restore(saved)
                self.applied.append(spec.name)
        except Exception:  # noqa: BLE001, S110 - last line of defence around the application's import
            pass


def _no_logs():
    from opentelemetry._logs import NoOpLoggerProvider

    return NoOpLoggerProvider()


def _restore(saved: Mapping[str, str | None]) -> None:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        elif os.environ.get(name) != value:
            os.environ[name] = value


# Every Hooks of the process, so that a host that freezes the process between requests (AWS Lambda) can send what the
# tracer provider holds before it returns.
_ARMED: list[Hooks] = []


def force_flush(timeout_millis: int = 30000) -> bool:
    """Send the spans the process holds, within the timeout; True when all were sent and the last export succeeded. A
    process whose instrumentation has not been applied yet holds none."""
    done = True
    for handler in list(_ARMED):
        provider = handler.provider
        if provider is not None:
            done = bool(provider.force_flush(timeout_millis)) and done
            processor = getattr(handler, "processor", None)
            if processor is not None and processor.last_export_failed:
                done = False
    return done


def arm(
    specs: list[Spec],
    *,
    environ: Mapping[str, str],
    on_failure: Callable[[BaseException], None],
    on_skipped: Callable[[list[str], dict[str, str]], None],
) -> Hooks:
    """Register the post-import hooks."""
    handler = Hooks(environ, on_failure, on_skipped)
    _ARMED.append(handler)
    finder = hooks.install()
    for spec in specs:
        for library in spec.libraries:
            finder.watch(library, lambda module, spec=spec: handler.apply(spec, module))
    return handler
