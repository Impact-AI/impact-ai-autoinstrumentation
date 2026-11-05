# SPDX-License-Identifier: Apache-2.0

"""Environment defaults of the package (C7). The chart's Instrumentation resource sets the same values."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

# Kubernetes probes and metric scrapes hit these every few seconds. Patterns are regular expressions searched in
# the full request URL; the variable is a comma-separated list, so a pattern must not contain a comma.
PROBE_PATTERNS = (
    r"/health(z|check)?(/|\?|$)",
    r"/live(z|ness)?(/|\?|$)",
    r"/read(y|yz|iness)(/|\?|$)",
    r"/ping(/|\?|$)",
    r"/_health(/|\?|$)",
    r"/-/(healthy|ready)(/|\?|$)",
    r"/metrics(/|\?|$)",
)

DEFAULTS = {
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY",
    "OTEL_PYTHON_EXCLUDED_URLS": ",".join(PROBE_PATTERNS),
}

CONTENT_MODE = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
# Modes that keep message content off the span.
_NO_CONTENT_ON_SPAN = {"no_content", "false", "event_only"}


def disabled_instrumentations(environ: Mapping[str, str]) -> set[str]:
    """The instrumentations the workload switched off (OTEL_PYTHON_DISABLED_INSTRUMENTATIONS), `*` for all of them."""
    names = (environ.get("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS") or "").split(",")
    return {name.strip() for name in names if name.strip()}


def apply(environ: MutableMapping[str, str]) -> dict[str, str]:
    """Set the defaults the workload did not set; returns what was set.

    OpenLLMetry's Bedrock package has its own content switch (TRACELOOP_TRACE_CONTENT, on by default). A content-free
    OpenTelemetry GenAI mode turns it off too, so one setting governs all three providers.
    """
    changed = {}
    for name, value in DEFAULTS.items():
        if name not in environ:
            environ[name] = value
            changed[name] = value
    mode = environ.get(CONTENT_MODE, "").strip().lower()
    if mode in _NO_CONTENT_ON_SPAN and "TRACELOOP_TRACE_CONTENT" not in environ:
        environ["TRACELOOP_TRACE_CONTENT"] = "false"
        changed["TRACELOOP_TRACE_CONTENT"] = "false"
    return changed
