# SPDX-License-Identifier: Apache-2.0

"""The `impact.setup` span: one report per process start naming the state the package is in (C8).

States: `active` (the package started), `pre-existing` (it stood down because the workload has a tracer of its own;
the detail names it) and `failed` (it could not start; the detail says why). The report is built and sent with the
standard library only, directly to the gateway, so that it works in all three states and never touches the
application's own OpenTelemetry setup.

Every Python process of the container starts the package (an exec probe is one), so a report is claimed once per
container with a marker file, and the HTTP machinery is imported only by the process that actually sends.

Besides the state, every report carries two string arrays (see frameworks.py; the monorepo drops what does not fit):

* `impact.setup.frameworks`: the AI libraries and agent frameworks among the installed distributions, each `name` or
  `name version` (one space, both at most 64 characters), at most 20, agent frameworks before provider SDKs;
* `impact.setup.untraced`: those of them the image cannot trace (yet), in the same form.

They are on every report, stood down or not, and `[]` when there is nothing to list: OTLP `arrayValue` of
`stringValue`, never one joined string and never a missing attribute.
"""

from __future__ import annotations

import atexit
import os
import time
import zlib
from collections.abc import Mapping

from impact_instrumentation._version import __version__

SPAN_NAME = "impact.setup"
STATES = ("active", "pre-existing", "failed")
MAX_DETAIL = 200
# What a process that ends waits for the report: a healthy gateway takes milliseconds, and one that does not answer is
# not worth a longer wait (a Job, a CronJob or a rollout would pay it).
EXIT_GRACE_SECONDS = 0.5
RETRY_SLEEP_SECONDS = 0.2
STATE_DIR_ENV = "IMPACT_STATE_DIR"
MARKER_PREFIX = ".impact-setup-"
_ATTEMPTS = 2


def claim(state: str, detail: str, *, environ: Mapping[str, str], root: str) -> bool:
    """True for the first process of the container to report this state and detail; False for the others.

    The marker lives in IMPACT_STATE_DIR (default /tmp, per container), else in the payload directory. If neither is
    writable the report is sent anyway: a duplicate is harmless, a missing report is not.
    """
    data = f"{state}\0{detail}\0{root}".encode()
    key = f"{zlib.crc32(data):08x}{zlib.adler32(data):08x}"  # builtin modules: hashlib would cost ~10 ms per process
    for directory in (environ.get(STATE_DIR_ENV) or "/tmp", root):  # noqa: S108 - per-container scratch space
        try:
            os.close(os.open(os.path.join(directory, MARKER_PREFIX + key), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            return False
        except OSError:
            continue
        return True
    return True


def build(
    state: str,
    detail: str,
    *,
    environ: Mapping[str, str],
    extra: Mapping[str, str | int | float | bool | list[str]] | None = None,
    now_ns: int | None = None,
) -> dict:
    if state not in STATES:
        raise ValueError(f"unknown setup state {state!r}; expected one of {STATES}")
    import platform

    from impact_instrumentation import otlp

    now = str(time.time_ns() if now_ns is None else now_ns)
    attributes = {
        "impact.setup.state": state,
        "impact.setup.detail": detail[:MAX_DETAIL],
        "impact.setup.package.version": __version__,
        "impact.setup.python.version": platform.python_version(),
        **(extra or {}),
    }
    span = {
        "traceId": os.urandom(16).hex(),
        "spanId": os.urandom(8).hex(),
        "name": SPAN_NAME,
        "kind": 1,
        "startTimeUnixNano": now,
        "endTimeUnixNano": now,
        "attributes": otlp.key_values(attributes),
        "status": {},
        "flags": 256,
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": otlp.key_values(otlp.resource_attributes(environ))},
                "scopeSpans": [{"scope": {"name": SPAN_NAME, "version": __version__}, "spans": [span]}],
            }
        ]
    }


def _state_of(payload: dict) -> str | None:
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    for item in span["attributes"]:
        if item["key"] == "impact.setup.state":
            return item["value"].get("stringValue")
    return None


def _run(url: str, payload: dict, headers: Mapping[str, str], timeout: float) -> None:
    from impact_instrumentation import otlp

    for attempt in range(_ATTEMPTS):
        try:
            if otlp.post(url, payload, headers=headers, timeout=timeout).ok:
                return
        except Exception:  # noqa: BLE001, S110 - reporting is best effort and must never reach the application
            pass
        if attempt + 1 < _ATTEMPTS:
            time.sleep(RETRY_SLEEP_SECONDS)


def _join(thread) -> None:
    thread.join(EXIT_GRACE_SECONDS)


def send_in_background(payload: dict, *, environ: Mapping[str, str]):
    """Send the report without delaying the process; short-lived jobs get a brief grace period at exit.

    A stood-down workload reports to the gateway only: its OTEL_EXPORTER_OTLP_* settings belong to the other tracer.
    """
    import threading

    from impact_instrumentation import otlp

    if _state_of(payload) == "pre-existing" and not otlp.in_kit(environ):
        return None
    thread = threading.Thread(
        target=_run,
        args=(otlp.traces_endpoint(environ), payload, otlp.exporter_headers(environ), otlp.timeout_seconds(environ)),
        name="impact-setup-report",
        daemon=True,
    )
    thread.start()
    atexit.register(_join, thread)
    return thread


def _text(value) -> str:
    return ",".join(value) if isinstance(value, list | tuple) else str(value)


def report(
    state: str,
    detail: str,
    *,
    environ: Mapping[str, str],
    root: str,
    extra: Mapping[str, str | list[str]] | None = None,
) -> None:
    """Claim and send a report, once per container for identical content."""
    content = f"{detail}\0" + "\0".join(f"{k}={_text(v)}" for k, v in sorted((extra or {}).items()))
    if claim(state, content, environ=environ, root=root):
        send_in_background(build(state, detail, environ=environ, extra=extra), environ=environ)
