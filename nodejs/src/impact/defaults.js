// SPDX-License-Identifier: Apache-2.0
'use strict';

// Environment defaults of the package. The chart's Instrumentation resource sets none of them, so a workload that
// already has a tracer keeps its environment as it was: they are applied only when the package starts.

// Kubernetes probes and metric scrapes hit these every few seconds. Patterns are regular expressions searched in the
// request URL (path and query).
const PROBE_PATTERNS = [
  /\/health(z|check)?(\/|\?|$)/,
  /\/live(z|ness)?(\/|\?|$)/,
  /\/read(y|yz|iness)(\/|\?|$)/,
  /\/ping(\/|\?|$)/,
  /\/_health(\/|\?|$)/,
  /\/-\/(healthy|ready)(\/|\?|$)/,
  /\/metrics(\/|\?|$)/,
];

const DEFAULTS = {
  // The SDKs of the kit's tracer pipeline have no logs or metrics pipeline to feed, and ingest takes no logs.
  OTEL_LOGS_EXPORTER: 'none',
  OTEL_METRICS_EXPORTER: 'none',
  // Anthropic's own SDK spans (0.134 and later) carry the prompt and the answer on the span, as the other routes do.
  ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE: 'content',
};

const CONTENT_MODE = 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT';
// Modes that keep message content off the span (the same switch the Python package honours).
const NO_CONTENT_ON_SPAN = ['no_content', 'false', 'event_only'];

/** The instrumentations the workload switched off (OTEL_NODE_DISABLED_INSTRUMENTATIONS), `all` for every one of them. */
function disabledInstrumentations(env) {
  const names = String(env.OTEL_NODE_DISABLED_INSTRUMENTATIONS || '').split(',');
  return names.map((name) => name.trim().toLowerCase()).filter(Boolean);
}

/** True when the workload asked for no message content on spans. */
function contentOff(env) {
  const mode = String(env[CONTENT_MODE] || '').trim().toLowerCase();
  return NO_CONTENT_ON_SPAN.indexOf(mode) >= 0;
}

/**
 * Set the defaults the workload did not set; returns what was set.
 *
 * A content-free mode of the OpenTelemetry GenAI switch turns Anthropic's content off as well, so that one setting
 * governs both providers (OpenInference's content is switched off in the same case, see runtime.js).
 */
function apply(env) {
  const changed = {};
  for (const name of Object.keys(DEFAULTS)) {
    if (env[name] === undefined) {
      env[name] = DEFAULTS[name];
      changed[name] = DEFAULTS[name];
    }
  }
  if (contentOff(env) && changed.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE === 'content') {
    env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE = 'metadata_only';
    changed.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE = 'metadata_only';
  }
  return changed;
}

function probe(url) {
  const text = String(url || '');
  for (const pattern of PROBE_PATTERNS) {
    if (pattern.test(text)) return true;
  }
  return false;
}

module.exports = { PROBE_PATTERNS, DEFAULTS, CONTENT_MODE, disabledInstrumentations, contentOff, apply, probe };
