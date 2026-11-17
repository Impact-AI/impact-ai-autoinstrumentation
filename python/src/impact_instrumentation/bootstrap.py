# SPDX-License-Identifier: Apache-2.0

"""Process start (called from sitecustomize): stand down beside another tracer, or arm the instrumentation.

Whatever happens, the application starts: every failure is caught, reported in the `impact.setup` span and the
application's environment is left as it was found.
"""

from __future__ import annotations

import os
import sys
from collections import namedtuple
from collections.abc import MutableMapping

from impact_instrumentation import coexistence, defaults, detect, frameworks, setup_report

SHARED_DIR = "_impact_shared"
AUTO_INSTRUMENTATION = os.path.join("opentelemetry", "instrumentation", "auto_instrumentation")

# state: active | pre-existing | failed | disabled
Outcome = namedtuple("Outcome", ["state", "detail"], defaults=[""])


def payload_root() -> str:
    """/otel-auto-instrumentation-python: the directory holding impact_instrumentation and the OTel packages."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def own_paths(root: str) -> list[str]:
    return [root, os.path.join(root, AUTO_INSTRUMENTATION), os.path.join(root, SHARED_DIR)]


def _same(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _restore(own: list[str], environ: MutableMapping[str, str], sys_path: list[str]) -> None:
    """Take our directories out of sys.path and PYTHONPATH, so the application sees the environment it was given."""
    sys_path[:] = [p for p in sys_path if not p or not any(_same(p, o) for o in own)]
    pythonpath = environ.get("PYTHONPATH")
    if pythonpath is not None:
        kept = [p for p in pythonpath.split(os.pathsep) if p and not any(_same(p, o) for o in own)]
        if kept:
            environ["PYTHONPATH"] = os.pathsep.join(kept)
        else:
            del environ["PYTHONPATH"]


def _purge(root: str) -> None:
    """Forget the modules a failed start imported from the payload, so the application re-imports its own."""
    prefix = os.path.realpath(root) + os.sep
    for name, module in list(sys.modules.items()):
        if name == "impact_instrumentation" or name.startswith("impact_instrumentation."):
            continue
        file = getattr(module, "__file__", None)
        if file and os.path.realpath(file).startswith(prefix):
            del sys.modules[name]


# The two lists are on every report; "nothing found", or "could not look", is an empty list, never a missing attribute.
NO_FRAMEWORKS = {"impact.setup.frameworks": [], "impact.setup.untraced": []}


def _survey(dists: dict, environ: MutableMapping[str, str], skipped=()) -> dict:
    """The setup span's framework attributes; a problem in the catalogue must not cost the report.

    A library the image's qualification does not prove for this workload is untraced, in every report: the image would
    not trace it, whether the package starts or stands down."""
    try:
        from impact_instrumentation import qualification

        unproven, _ = qualification.screen(frameworks.shipped(), dists)
        return frameworks.survey(dists, environ=environ, skipped=set(skipped) | unproven).attributes()
    except Exception:  # noqa: BLE001
        return {key: [] for key in NO_FRAMEWORKS}


def _tool(findings: list, dists: dict, environ: MutableMapping[str, str]):
    """The tool beside the package; a problem naming it must not cost the stand-down or the report."""
    try:
        return detect.tool(findings, dists, environ)
    except Exception:  # noqa: BLE001
        return None


def _skipped(reasons: dict[str, str]) -> str:
    """impact.setup.skipped: `instrumentation: why`, one after the other, by name."""
    return "; ".join(f"{name}: {why}" for name, why in sorted(reasons.items()))


def _failed(
    exc: BaseException,
    environ: MutableMapping[str, str],
    sys_path: list[str],
    root: str,
    own: list[str],
    applied: dict[str, str],
    extra: dict | None = None,
) -> Outcome:
    detail = f"{type(exc).__name__}: {exc}".splitlines()[0][: setup_report.MAX_DETAIL]
    for name, value in applied.items():
        if environ.get(name) == value:
            del environ[name]
    _restore(own, environ, sys_path)
    _purge(root)
    try:
        setup_report.report("failed", detail, environ=environ, root=root, extra=extra)
    except Exception:  # noqa: BLE001, S110 - best effort
        pass
    return Outcome("failed", detail)


def start(
    *,
    environ: MutableMapping[str, str] | None = None,
    sys_path: list[str] | None = None,
    argv: list[str] | None = None,
    orig_argv: list[str] | None = None,
    root: str | None = None,
) -> Outcome:
    env = os.environ if environ is None else environ
    path = sys.path if sys_path is None else sys_path
    root = root or payload_root()
    own = own_paths(root)
    applied: dict[str, str] = {}
    found: dict = {key: [] for key in NO_FRAMEWORKS}  # the AI libraries of the environment, on every report
    try:
        if (env.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true":
            return Outcome("disabled")

        scan = detect.scan(path, own)
        found = _survey(scan.dists, env)
        findings = detect.find_foreign(
            environ=env,
            sys_path=path,
            argv=list(sys.argv if argv is None else argv),
            orig_argv=getattr(sys, "orig_argv", None) if orig_argv is None else orig_argv,
            own_roots=own,
            scan=scan,
        )
        # The tool beside the package, by name: the package starts next to it only where the coexistence list
        # proves that; any other tracer keeps the stand-down.
        tool = _tool(findings, scan.dists, env)
        if findings and not coexistence.alongside(findings, tool):
            detail = detect.describe(findings)
            _restore(own, env, path)
            extra = {**found, **coexistence.attributes(tool, False)}
            setup_report.report("pre-existing", detail, environ=env, root=root, extra=extra)
            return Outcome("pre-existing", detail)
        beside = coexistence.attributes(tool, True)

        applied = defaults.apply(env)
        shared = os.path.join(root, SHARED_DIR)
        if os.path.isdir(shared) and not any(_same(p, shared) for p in path if p):
            path.append(shared)  # last: the application's own copies of wrapt, packaging, ... win
        from impact_instrumentation import qualification, runtime

        specs = runtime.plan(scan=scan, environ=env)
        # An AI library is armed only where a passing cell of the image's qualification proves it for this workload;
        # the others start as they would without the kit, and the report says what Impact AI traces instead.
        unproven, reasons = qualification.screen([spec.name for spec in specs], scan.dists)
        if not qualification.arms_unproven(env):
            specs = [spec for spec in specs if spec.name not in unproven]

        def on_skipped(armed: list[str], skipped: dict[str, str]) -> None:
            # The version range is checked when the application imports the library; what is outside it is untraced.
            extra = {
                "impact.setup.instrumentations": ",".join(armed),
                **_survey(scan.dists, env, unproven | set(skipped)),
                **beside,
            }
            extra["impact.setup.skipped"] = _skipped({**reasons, **skipped})
            setup_report.report("active", "", environ=env, root=root, extra=extra)

        runtime.arm(
            specs,
            environ=env,
            on_failure=lambda exc: _failed(exc, env, path, root, own, {}, found),
            on_skipped=on_skipped,
        )
        names = ",".join(spec.name for spec in specs)
        extra = {"impact.setup.instrumentations": names, **found, **beside}
        if reasons:
            extra["impact.setup.skipped"] = _skipped(reasons)
        setup_report.report("active", "", environ=env, root=root, extra=extra)
        return Outcome("active", names)
    except Exception as exc:  # noqa: BLE001 - the application must start whatever happens here
        return _failed(exc, env, path, root, own, applied, found)
