# SPDX-License-Identifier: Apache-2.0

"""Arm the instrumentation at process start; apply it when the application imports the library.

Every Python process of an injected container starts this package, including exec probes and helper processes. Eager
instrumentation imports OpenAI, Anthropic, boto3 and the web frameworks (seconds, in every process, whether or not it
uses them) and would stall a process with a one-second probe timeout. So nothing heavy happens at start: for each
instrumentation of the payload whose library is installed (found by listing directories, see detect.scan) a
post-import hook is registered, and the instrumentor is applied when, and only if, the application imports that library.
The supported version range is checked then, with `packaging`, in the one process that needs it.

The tracer provider is created on the first hook and is private: no global tracer, meter or logger provider is set,
and only the instrumentors this payload ships are applied (never the application's own).
"""

from __future__ import annotations

import importlib
import logging
from collections import namedtuple
from collections.abc import Callable, Mapping

from impact_instrumentation import defaults, hooks
from impact_instrumentation._version import __version__
from impact_instrumentation.detect import normalize

log = logging.getLogger("impact_instrumentation")

# name: the entry point name (openai, fastapi, ...); module, cls: where the instrumentor lives; libraries: the
# top-level modules whose import applies it; required / any_of: ((distribution, specifier, installed version), ...).
Spec = namedtuple("Spec", ["name", "module", "cls", "libraries", "required", "any_of"])


# The module an application imports where it differs from the name of its distribution.
MODULES = {"openai-agents": "agents"}

# Libraries that also arm an instrumentation, beside those of its own `instruments` extra. The LangChain
# instrumentation listens to langchain-core's callbacks, and a LangGraph application often has langchain-core (and
# langchain-openai) but not langchain. The floor is the langchain-core that langchain 0.3.22, the instrumentation's own
# floor, requires.
ALTERNATIVES = {"langchain": (("langchain-core", "<2,>=0.3.49"),)}


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


def unsupported(spec: Spec) -> str:
    """Why the installed libraries are outside the range this instrumentation supports; empty when they are inside."""
    from packaging.specifiers import SpecifierSet

    def inside(item) -> bool:
        _, specifier, version = item
        return not specifier or SpecifierSet(specifier).contains(version, prereleases=True)

    bad = [item for item in spec.required if not inside(item)]
    if spec.any_of and not any(inside(item) for item in spec.any_of):
        bad += list(spec.any_of)
    return ", ".join(f"{lib} {version} installed, supported {specifier}" for lib, specifier, version in bad)


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
        self.broken = False
        self.applied: list[str] = []
        self.skipped: dict[str, str] = {}

    def _provider(self):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from impact_instrumentation import otlp
        from impact_instrumentation.agent_names import AgentNames
        from impact_instrumentation.exporter import ImpactSpanExporter
        from impact_instrumentation.processor import BoundedBatchSpanProcessor

        if self.provider is None:
            attributes = {"telemetry.distro.name": "impact-instrumentation", "telemetry.distro.version": __version__}
            service = otlp.default_service_name(self.environ)
            if service:
                attributes["service.name"] = service
            provider = TracerProvider(resource=Resource.create(attributes))
            provider.add_span_processor(AgentNames())
            exporter = ImpactSpanExporter(environ=self.environ)
            provider.add_span_processor(BoundedBatchSpanProcessor(exporter, environ=self.environ))
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
                try:
                    getattr(importlib.import_module(spec.module), spec.cls)().instrument(
                        tracer_provider=provider, skip_dep_check=True
                    )
                except Exception as exc:  # noqa: BLE001 - one broken instrumentor must not stop the others
                    log.warning("could not instrument %s: %s: %s", spec.name, type(exc).__name__, exc)
                    return
                self.applied.append(spec.name)
        except Exception:  # noqa: BLE001, S110 - last line of defence around the application's import
            pass


def arm(
    specs: list[Spec],
    *,
    environ: Mapping[str, str],
    on_failure: Callable[[BaseException], None],
    on_skipped: Callable[[list[str], dict[str, str]], None],
) -> Hooks:
    """Register the post-import hooks."""
    handler = Hooks(environ, on_failure, on_skipped)
    finder = hooks.install()
    for spec in specs:
        for library in spec.libraries:
            finder.watch(library, lambda module, spec=spec: handler.apply(spec, module))
    return handler
