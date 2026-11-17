// SPDX-License-Identifier: Apache-2.0
'use strict';

// The `impact.setup` span: one report per process start naming the state the package is in, with the attributes and the
// limits of the Python package's report.
//
// States: `active` (the package started), `pre-existing` (it stood down because the workload has a tracer of its own; the
// detail names it) and `failed` (it could not start; the detail says why). The report is built and sent with Node's own
// modules only, directly to the gateway, so that it works in all three states and never touches the application's own
// OpenTelemetry setup.
//
// Every Node process of the container starts the package (a worker, a child process, a probe is one), so a report is
// claimed once per container with a marker file, and the process that does not win builds nothing.
//
// Besides the state, every report carries two string arrays: `impact.setup.frameworks`, the AI libraries and agent
// frameworks among the installed packages, and `impact.setup.untraced`, those of them the image cannot trace. They are on
// every report, stood down or not, and `[]` when there is nothing to list: OTLP `arrayValue` of `stringValue`, never one
// joined string and never a missing attribute.
//
// When the package found a tool it knows beside it (detect.toolOf; coexistence.js), the report also names it:
// `impact.setup.tool` (its id, such as `langsmith`), `impact.setup.tool.version`, `impact.setup.exporter` (the kind of
// exporter its provider uses, such as `otlp-http`, when the names and settings show it) and `impact.setup.alongside`, a
// boolean: true when the package started beside the tool, false when it stood down. Names, a version and a flag; never a
// setting's value. A report that names no tool has none of the four.

const fs = require('fs');
const path = require('path');
const otlp = require('./otlp');
const { VERSION } = require('./version');

const SPAN_NAME = 'impact.setup';
const STATES = ['active', 'pre-existing', 'failed'];
const MAX_DETAIL = 200;
// How long the report may hold a process open: it is the one thing the package holds a short-lived job for, with its request.
// A healthy gateway takes milliseconds, and one that does not answer is not worth a longer wait (a Job, a CronJob or a
// rollout would pay it): `node -e 0` waited 3.2 s for a gateway that hangs, 5.1 s for one whose packets are dropped. The
// deadline covers the connect, too: a connect that nothing answers holds the process however the request is made.
const GRACE_MS = 500;
const RETRY_AFTER_MS = 1000;
const STATE_DIR_ENV = 'IMPACT_STATE_DIR';
const MARKER_PREFIX = '.impact-setup-';

let crcTable = null;

function crc32(text) {
  if (!crcTable) {
    crcTable = [];
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable.push(c >>> 0);
    }
  }
  const bytes = Buffer.from(text, 'utf8');
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) crc = crcTable[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function adler32(text) {
  const bytes = Buffer.from(text, 'utf8');
  let a = 1;
  let b = 0;
  for (let i = 0; i < bytes.length; i += 1) {
    a = (a + bytes[i]) % 65521;
    b = (b + a) % 65521;
  }
  return ((b << 16) | a) >>> 0;
}

function hex8(number) {
  return ('00000000' + number.toString(16)).slice(-8);
}

/**
 * True for the first process of the container to report this state and detail; false for the others.
 *
 * The marker lives in IMPACT_STATE_DIR (default /tmp, per container), else in the payload directory. If neither is
 * writable the report is sent anyway: a duplicate is harmless, a missing report is not.
 */
function claim(state, detail, env, root) {
  const data = state + '\0' + detail + '\0' + root;
  const key = hex8(crc32(data)) + hex8(adler32(data));
  const directories = [env[STATE_DIR_ENV] || '/tmp', root];
  for (const directory of directories) {
    try {
      fs.closeSync(fs.openSync(path.join(directory, MARKER_PREFIX + key), 'wx', 0o644));
    } catch (error) {
      if (error && error.code === 'EEXIST') return false;
      continue;
    }
    return true;
  }
  return true;
}

function nowNanos() {
  return String(Date.now()) + '000000';
}

function build(state, detail, env, extra) {
  if (STATES.indexOf(state) < 0) throw new Error('unknown setup state ' + state + '; expected one of ' + STATES.join(', '));
  const crypto = require('crypto');
  const now = nowNanos();
  const attributes = Object.assign(
    {
      'impact.setup.state': state,
      'impact.setup.detail': String(detail || '').slice(0, MAX_DETAIL),
      'impact.setup.package.version': VERSION,
      'impact.setup.node.version': process.versions.node,
    },
    extra || {}
  );
  const span = {
    traceId: crypto.randomBytes(16).toString('hex'),
    spanId: crypto.randomBytes(8).toString('hex'),
    name: SPAN_NAME,
    kind: 1,
    startTimeUnixNano: now,
    endTimeUnixNano: now,
    attributes: otlp.keyValues(attributes),
    status: {},
    flags: 256,
  };
  return {
    resourceSpans: [
      {
        resource: { attributes: otlp.keyValues(otlp.resourceAttributes(env)) },
        scopeSpans: [{ scope: { name: SPAN_NAME, version: VERSION }, spans: [span] }],
      },
    ],
  };
}

/**
 * Send the report without delaying the process for more than a short grace period: the request is the only thing that
 * keeps a short-lived job alive, and it gives up after GRACE_MS, wherever it is. A second attempt follows a second later if
 * the process is still running (the timer does not keep it alive).
 *
 * A stood-down workload reports to the gateway only: its OTEL_EXPORTER_OTLP_* settings belong to the other tracer.
 */
function send(payload, state, env) {
  if (state === 'pre-existing' && !otlp.inKit(env)) return;
  const url = otlp.tracesEndpoint(env);
  const headers = otlp.exporterHeaders(env);
  const timeout = Math.min(otlp.timeoutMs(env), GRACE_MS);
  const attempt = (remaining) => {
    otlp
      .post(url, payload, { headers, timeoutMs: timeout })
      .then((response) => {
        if (response.status >= 200 && response.status < 300) return;
        throw new Error('HTTP ' + response.status);
      })
      .catch(() => {
        if (remaining <= 0) return;
        const timer = setTimeout(() => attempt(remaining - 1), RETRY_AFTER_MS);
        if (timer.unref) timer.unref();
      });
  };
  attempt(1);
}

/** Claim and send a report, once per container for identical content. Never throws. */
function report(state, detail, env, root, extra) {
  try {
    const parts = Object.keys(extra || {})
      .sort()
      .map((key) => key + '=' + [].concat(extra[key]).join(','));
    if (claim(state, String(detail || '') + '\0' + parts.join('\0'), env, root)) {
      send(build(state, detail, env, extra), state, env);
    }
  } catch (error) {
    // reporting is best effort and must never reach the application
  }
}

module.exports = { SPAN_NAME, STATES, MAX_DETAIL, claim, build, send, report };
