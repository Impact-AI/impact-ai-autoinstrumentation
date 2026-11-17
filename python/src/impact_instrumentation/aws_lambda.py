# SPDX-License-Identifier: Apache-2.0

"""The package on AWS Lambda: start it for a function, wrap the function's handler, and send what it holds at the end of
each invocation without holding the invocation open.

The layer's wrapper (the script AWS_LAMBDA_EXEC_WRAPPER names) puts the layer's sitecustomize first on PYTHONPATH, and
that calls start() before Lambda's runtime has loaded anything of the function. From then on:

* Start is the package's own (bootstrap.start), with the directories Lambda's runtime is about to put on the path (the
  function's code and the layers' /opt/python) searched as well, so that a tracer or an AI library of the function or of
  another layer is found. A function whose handler or settings show another tracer's Lambda wrapper stands down: the
  function has one wrapper, and it belongs to that tracer.
* The handler is wrapped when the runtime imports its module, so the runtime finds the same handler under the same name
  and reports the function's own errors exactly as it would without the layer.
* Lambda freezes the environment between invocations, and a thread that sends while the environment is frozen sends
  nothing. So the setup report waits for the first invocation (which also names the function's account and ARN), and at
  the end of every invocation a thread of the package sends the spans and reports it holds. The handler waits for it at
  most IMPACT_LAMBDA_FLUSH_MS (FLUSH_MS by default, and never into the last MARGIN_MS of the invocation's time), then
  returns: what is not sent by then goes out with the next invocation of the same environment. Once a wait has run out,
  invocations stop waiting at all until a send succeeds again, so an ingest that hangs or cannot be reached costs one
  invocation the bound, not every invocation.

Whatever happens here, the function runs and returns as it would without the layer. Standard library only at import.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping

from impact_instrumentation import detect

FUNCTION_NAME = "AWS_LAMBDA_FUNCTION_NAME"
TASK_ROOT = "LAMBDA_TASK_ROOT"
HANDLER = "_HANDLER"
FLUSH_ENV = "IMPACT_LAMBDA_FLUSH_MS"
# What an invocation waits, at most, for the package to send what it holds. A healthy ingest takes tens of milliseconds;
# one that is slow or out of reach costs one invocation this much, and those after it no wait until a send succeeds.
FLUSH_MS = 200
# The time an invocation keeps for itself: the wait ends this long before the function's own timeout.
MARGIN_MS = 50
# How many invocations a setup report that could not be sent is tried with.
REPORT_ATTEMPTS = 3

# Another tracer's Lambda wrapper, by the handler it puts in _HANDLER or the setting that names the function's own
# handler for it. Read here, never sent: a finding names the tracer.
FOREIGN_HANDLERS = {
    "datadog_lambda.handler.handler": "datadog-lambda",
    "newrelic_lambda_wrapper.handler": "newrelic-lambda",
    "lumigo_tracer._handler": "lumigo",
    "otel_wrapper.lambda_handler": "opentelemetry-lambda",
}
FOREIGN_SETTINGS = {
    "DD_LAMBDA_HANDLER": "datadog-lambda",
    "NEW_RELIC_LAMBDA_HANDLER": "newrelic-lambda",
    "LUMIGO_ORIGINAL_HANDLER": "lumigo",
    "ORIG_HANDLER": "opentelemetry-lambda",
}


def on_lambda(environ: Mapping[str, str]) -> bool:
    return bool(environ.get(FUNCTION_NAME) and environ.get(TASK_ROOT))


def runtime_paths(environ: Mapping[str, str], *, pythonpath_set: bool, version=None) -> list[str]:
    """The directories Lambda's runtime puts first on the path before it imports the handler, in its order: the
    function's code, and when the function sets no PYTHONPATH, the layers' /opt/python/lib/pythonX.Y/site-packages and
    /opt/python."""
    major, minor = (version or sys.version_info)[:2]
    paths = [environ[TASK_ROOT]] if environ.get(TASK_ROOT) else []
    if not pythonpath_set:
        paths += [f"/opt/python/lib/python{major}.{minor}/site-packages", "/opt/python"]
    return paths


def foreign(environ: Mapping[str, str]) -> list[detect.Finding]:
    """Another tracer's Lambda wrapper: its handler in _HANDLER, or the setting it keeps the function's handler in."""
    found = {}
    tracer = FOREIGN_HANDLERS.get((environ.get(HANDLER) or "").strip())
    if tracer:
        found[tracer] = detect.Finding("module", tracer)
    for name, tracer in FOREIGN_SETTINGS.items():
        if environ.get(name) and tracer not in found:
            found[tracer] = detect.Finding("environment", name)
    return sorted(found.values(), key=lambda finding: finding.order)


def split_handler(handler: str) -> tuple[str, str] | None:
    """('package.module', 'function') from the handler setting, as Lambda's runtime reads it ('dir/module.function')."""
    module, sep, function = (handler or "").strip().rpartition(".")
    if not sep or not module or not function:
        return None
    return module.replace("/", "."), function


def function_arn(invoked_arn: str) -> tuple[str, str] | None:
    """(account, the function's ARN without an alias or version) from the ARN an invocation was made with."""
    parts = (invoked_arn or "").split(":")
    if len(parts) < 7 or parts[0] != "arn" or parts[2] != "lambda" or parts[5] != "function":
        return None
    account = parts[4]
    if not (account.isdigit() and len(account) == 12):
        return None
    return account, ":".join(parts[:7])


# --- what the package holds, and its sender -------------------------------------------------------------------------


class Outbox:
    """Setup reports that wait for an invocation to be sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[list] = []  # [payload, environ, attempts]

    def hold(self, payload: dict, *, environ: Mapping[str, str]):
        """setup_report.send_in_background on Lambda: keep the report for the end of the next invocation."""
        from impact_instrumentation import otlp, setup_report

        if setup_report._state_of(payload) == "pre-existing" and not otlp.in_kit(environ):
            return None  # its OTEL_EXPORTER_OTLP_* settings belong to the other tracer, as everywhere else
        with self._lock:
            self._items.append([payload, environ, 0])
        return None

    def __len__(self) -> int:
        return len(self._items)

    def send(self, post: Callable | None = None) -> bool:
        """Send what is held; a report that fails is tried again with the next invocations, then dropped. True when
        every report held was sent (or none was held)."""
        from impact_instrumentation import otlp

        post = post or otlp.post
        with self._lock:
            items, self._items = self._items, []
        kept = []
        all_sent = True
        for item in items:
            payload, environ, attempts = item
            try:
                ok = post(
                    otlp.traces_endpoint(environ),
                    payload,
                    headers=otlp.exporter_headers(environ),
                    timeout=otlp.timeout_seconds(environ),
                    ca_file=None if otlp.in_kit(environ) else environ.get("OTEL_EXPORTER_OTLP_CERTIFICATE"),
                ).ok
            except Exception:  # noqa: BLE001 - ingest out of reach is the normal case this survives
                ok = False
            all_sent = all_sent and ok
            if not ok and attempts + 1 < REPORT_ATTEMPTS:
                kept.append([payload, environ, attempts + 1])
        if kept:
            with self._lock:
                self._items[0:0] = kept
        return all_sent


def _flush_spans(timeout_millis: int) -> bool:
    from impact_instrumentation import runtime

    return runtime.force_flush(timeout_millis)


class Flusher:
    """Sends what the package holds from a thread of its own, so that an invocation can stop waiting for it.

    Each flush is a request with a number; the thread answers the newest request it has read, and a wait ends when the
    thread has answered the request it made (never an earlier one). When a wait runs out, the flusher backs off:
    invocations ask for a send and return at once, without waiting, until a send succeeds again (an ingest that hangs
    would otherwise cost every invocation the whole bound). What they hold goes out when the thread gets to it.
    """

    def __init__(self, outbox: Outbox, flush_spans: Callable[[int], object] = _flush_spans) -> None:
        self.outbox = outbox
        self._flush_spans = flush_spans
        self._pid = os.getpid()
        self._reset()

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._asked = 0  # the number of the newest request
        self._answered = 0  # the number of the newest request the thread has answered
        self._backing_off = False
        self._thread: threading.Thread | None = None

    @property
    def backing_off(self) -> bool:
        return self._backing_off

    def _ensure_thread(self) -> None:
        if self._pid != os.getpid():  # a forked child: the parent's thread and lock are not its own
            self._pid = os.getpid()
            self._reset()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="impact-lambda-flush", daemon=True)
            self._thread.start()

    def _send(self) -> bool:
        sent = True
        try:
            sent = self.outbox.send() is not False
        except Exception:  # noqa: BLE001 - the sender must outlive any failure
            sent = False
        try:
            sent = self._flush_spans(10_000) is not False and sent
        except Exception:  # noqa: BLE001
            sent = False
        return sent

    def _run(self) -> None:
        while True:
            with self._changed:
                while self._answered >= self._asked:
                    self._changed.wait()
                request = self._asked
            sent = self._send()
            with self._changed:
                self._answered = request
                if sent:
                    self._backing_off = False
                self._changed.notify_all()

    def flush(self, wait_seconds: float) -> bool:
        """Ask for a send and wait at most `wait_seconds` for the thread to answer it. True when it answered in that
        time (sent, or failed at once: a refused key costs no more than its answer).

        While backing off (an earlier wait ran out and no send has succeeded since), it does not wait at all."""
        self._ensure_thread()
        with self._changed:
            self._asked += 1
            request = self._asked
            self._changed.notify_all()
            if self._backing_off or wait_seconds <= 0:
                return False
            if self._changed.wait_for(lambda: self._answered >= request, wait_seconds):
                return True
            self._backing_off = True
            return False


def flush_budget(environ: Mapping[str, str], remaining_ms: float | None) -> float:
    """Seconds the end of an invocation waits for the package: IMPACT_LAMBDA_FLUSH_MS (else FLUSH_MS), never into the
    last MARGIN_MS of the invocation's time."""
    try:
        budget = float(environ.get(FLUSH_ENV) or FLUSH_MS)
    except ValueError:
        budget = FLUSH_MS
    budget = max(budget, 0.0)
    if remaining_ms is not None:
        budget = min(budget, max(remaining_ms - MARGIN_MS, 0.0))
    return budget / 1000.0


# --- the handler -----------------------------------------------------------------------------------------------------


class Function:
    """One function's environment: its settings, what the package holds, and the facts its first invocation names."""

    def __init__(self, environ: Mapping[str, str], flusher: Flusher) -> None:
        self.environ = environ
        self.flusher = flusher
        self.invoked = False
        self.last_flush_seconds = 0.0

    def first_invocation(self, context) -> None:
        from impact_instrumentation import otlp

        self.invoked = True
        found = function_arn(getattr(context, "invoked_function_arn", "") or "")
        if found:
            account, arn = found
            otlp.remember({"cloud.account.id": account, "cloud.resource_id": arn})

    def before(self, context) -> None:
        try:
            if not self.invoked:
                self.first_invocation(context)
        except Exception:  # noqa: BLE001, S110 - the invocation goes on
            pass

    def after(self, context) -> None:
        begin = time.monotonic()
        try:
            remaining = None
            remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
            if remaining_ms is not None:
                remaining = float(remaining_ms())
            self.flusher.flush(flush_budget(self.environ, remaining))
        except Exception:  # noqa: BLE001, S110 - the invocation returns whatever the flush does
            pass
        self.last_flush_seconds = time.monotonic() - begin

    def wrap(self, handler):
        if getattr(handler, "__impact_lambda__", False) or not callable(handler):
            return handler
        function = self

        def impact_lambda_handler(event, context):
            function.before(context)
            try:
                return handler(event, context)
            finally:
                function.after(context)

        for attribute in ("__module__", "__name__", "__qualname__", "__doc__"):
            try:
                setattr(impact_lambda_handler, attribute, getattr(handler, attribute))
            except (AttributeError, TypeError):
                pass
        impact_lambda_handler.__wrapped__ = handler
        impact_lambda_handler.__impact_lambda__ = True
        return impact_lambda_handler


class HandlerFinder:
    """Wraps the handler when Lambda's runtime imports the handler's module (a package's module included)."""

    def __init__(self, module: str, attribute: str, wrap: Callable) -> None:
        self.module = module
        self.attribute = attribute
        self._wrap = wrap
        self.fired = False

    def fire(self, _name: str, module) -> None:
        if self.fired:
            return
        self.fired = True
        try:
            handler = getattr(module, self.attribute, None)
            if handler is not None:
                setattr(module, self.attribute, self._wrap(handler))
        except Exception:  # noqa: BLE001, S110 - the runtime finds the function's own handler
            pass
        finally:
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.module or self.fired:
            return None
        from impact_instrumentation.hooks import _Loader

        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                spec.loader = _Loader(spec.loader, self, fullname)
            return spec
        return None


def watch_handler(handler: str, wrap: Callable) -> HandlerFinder | None:
    """Wrap the handler the setting names once its module is imported (at once, if it already is)."""
    found = split_handler(handler)
    if found is None:
        return None
    finder = HandlerFinder(found[0], found[1], wrap)
    module = sys.modules.get(found[0])
    if module is not None:
        finder.fire(found[0], module)
        return finder
    sys.meta_path.insert(0, finder)
    return finder


# --- start -----------------------------------------------------------------------------------------------------------

OUTBOX = Outbox()
FUNCTION: Function | None = None


def _hold_reports() -> None:
    """Setup reports go to the outbox from now on: a report sent at start would be frozen with the environment."""
    from impact_instrumentation import setup_report

    setup_report.send_in_background = OUTBOX.hold


def start(
    *,
    environ: MutableMapping[str, str] | None = None,
    sys_path: list[str] | None = None,
    root: str | None = None,
    pythonpath_set: bool = False,
):
    """Start the package for a Lambda function. Returns bootstrap's Outcome; never raises."""
    global FUNCTION
    from impact_instrumentation import bootstrap

    env = os.environ if environ is None else environ
    path = sys.path if sys_path is None else sys_path
    root = root or bootstrap.payload_root()
    try:
        _hold_reports()
        function = Function(env, Flusher(OUTBOX))
        findings = foreign(env)
        if findings:
            from impact_instrumentation import setup_report

            detail = detect.describe(findings)
            bootstrap._restore(bootstrap.own_paths(root), env, path)
            setup_report.report("pre-existing", detail, environ=env, root=root, extra=dict(bootstrap.NO_FRAMEWORKS))
            return bootstrap.Outcome("pre-existing", detail)
        added = [p for p in runtime_paths(env, pythonpath_set=pythonpath_set) if p not in path]
        path[0:0] = added
        try:
            outcome = bootstrap.start(environ=env, sys_path=path, root=root)
        finally:
            for directory in added:
                if directory in path:
                    path.remove(directory)
        if outcome.state in ("active", "failed"):
            FUNCTION = function
            watch_handler(env.get(HANDLER) or "", function.wrap)
        return outcome
    except Exception as exc:  # noqa: BLE001 - the function must start whatever happens here
        return bootstrap.Outcome("failed", f"{type(exc).__name__}: {exc}".splitlines()[0][:200])
