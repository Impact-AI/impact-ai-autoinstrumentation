// SPDX-License-Identifier: Apache-2.0
'use strict';

// OTLP/HTTP JSON helpers on Node's own modules: value encoding, the resource from the environment, the endpoint to
// send to, and the POST. Shared by the span exporter and the setup report, so it loads nothing but built-in modules.

const http = require('http');
const https = require('https');
const zlib = require('zlib');
const { VERSION } = require('./version');

const KIT_ENDPOINT = 'IMPACT_OTLP_ENDPOINT';
// What one attempt to reach the in-cluster gateway may take, from the moment the request is made. The gateway answers in
// milliseconds when it works; when it does not (a NetworkPolicy dropping the packets, a wedged pod) every second is one a
// process holds on to its spans, and one its exit waits for.
const KIT_TIMEOUT_MS = 2000;
// A timer that fires more than STALL_MS late says that the process did not run its event loop (it was busy with its own
// work, as an application is while it loads its modules), not that the gateway did not answer: what has come in is waiting to
// be read. The deadline then gets one more STALL_GRACE_MS for that, and no more.
const STALL_MS = 100;
const STALL_GRACE_MS = 300;
const DEFAULT_TIMEOUT_MS = 10000;
const USER_AGENT = 'impact-instrumentation/' + VERSION + ' node/' + process.versions.node;
const MAX_SAFE = Number.MAX_SAFE_INTEGER;

const WORKLOAD_NAME_KEYS = [
  'k8s.deployment.name',
  'k8s.statefulset.name',
  'k8s.daemonset.name',
  'k8s.cronjob.name',
  'k8s.job.name',
  'k8s.pod.name',
];

/** An OTLP JSON AnyValue. 64-bit integers are decimal strings, as the OTLP JSON mapping requires. */
function anyValue(value) {
  if (typeof value === 'boolean') return { boolValue: value };
  if (typeof value === 'string') return { stringValue: value };
  if (typeof value === 'bigint') return { intValue: value.toString() };
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return { stringValue: String(value) };
    if (Number.isInteger(value)) return Math.abs(value) <= MAX_SAFE ? { intValue: String(value) } : { doubleValue: value };
    return { doubleValue: value };
  }
  if (Array.isArray(value)) return { arrayValue: { values: value.map(anyValue) } };
  if (value && typeof value === 'object') {
    if (value instanceof Uint8Array) return { bytesValue: Buffer.from(value).toString('base64') };
    return { kvlistValue: { values: keyValues(value) } };
  }
  return { stringValue: String(value) };
}

function keyValues(attributes) {
  const out = [];
  const source = attributes || {};
  for (const key of Object.keys(source)) {
    if (source[key] === undefined || source[key] === null) continue;
    out.push({ key: String(key), value: anyValue(source[key]) });
  }
  return out;
}

function percentDecode(text) {
  try {
    return decodeURIComponent(text);
  } catch (error) {
    return text;
  }
}

function environmentAttributes(env) {
  const attributes = {};
  const pairs = String(env.OTEL_RESOURCE_ATTRIBUTES || '').split(',');
  for (const pair of pairs) {
    const at = pair.indexOf('=');
    if (at < 0) continue;
    const key = pair.slice(0, at).trim();
    if (key) attributes[key] = percentDecode(pair.slice(at + 1).trim());
  }
  return attributes;
}

/**
 * The name of the workload, when nothing names the service; null when OTEL_SERVICE_NAME or service.name is set.
 *
 * The kit's Instrumentation leaves OTEL_SERVICE_NAME out (another tracer in the workload would obey it), so the service
 * is named after the workload the Operator described in OTEL_RESOURCE_ATTRIBUTES.
 */
function defaultServiceName(env) {
  const attributes = environmentAttributes(env);
  if (env.OTEL_SERVICE_NAME || attributes['service.name']) return null;
  for (const key of WORKLOAD_NAME_KEYS) {
    if (attributes[key]) return attributes[key];
  }
  return null;
}

/** The workload's resource as the OpenTelemetry SDK builds it from OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME. */
function resourceAttributes(env) {
  const attributes = environmentAttributes(env);
  if (env.OTEL_SERVICE_NAME) attributes['service.name'] = env.OTEL_SERVICE_NAME;
  if (!attributes['service.name']) {
    attributes['service.name'] = defaultServiceName(env) || 'unknown_service:node';
  }
  attributes['telemetry.sdk.language'] = 'nodejs';
  attributes['telemetry.distro.name'] = 'impact-instrumentation';
  attributes['telemetry.distro.version'] = VERSION;
  return attributes;
}

/** True inside the cluster kit, where the package sends to the gateway named by IMPACT_OTLP_ENDPOINT. */
function inKit(env) {
  return Boolean(env[KIT_ENDPOINT]);
}

function tracesEndpoint(env) {
  const kit = env[KIT_ENDPOINT];
  if (kit) return kit.replace(/\/+$/, '') + '/v1/traces';
  if (env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) return env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
  const general = env.OTEL_EXPORTER_OTLP_ENDPOINT;
  return (general ? general.replace(/\/+$/, '') : 'http://localhost:4318') + '/v1/traces';
}

/** Standard OTEL_EXPORTER_OTLP_HEADERS, honoured outside the kit only (inside it they are the workload's own). */
function exporterHeaders(env) {
  if (inKit(env)) return {};
  const raw = env.OTEL_EXPORTER_OTLP_TRACES_HEADERS || env.OTEL_EXPORTER_OTLP_HEADERS || '';
  const headers = {};
  for (const pair of raw.split(',')) {
    const at = pair.indexOf('=');
    const key = at < 0 ? '' : pair.slice(0, at).trim();
    if (key) headers[key] = percentDecode(pair.slice(at + 1).trim());
  }
  return headers;
}

function timeoutMs(env) {
  if (inKit(env)) return KIT_TIMEOUT_MS;
  const raw = env.OTEL_EXPORTER_OTLP_TRACES_TIMEOUT || env.OTEL_EXPORTER_OTLP_TIMEOUT;
  const parsed = raw ? Number(raw) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? Math.max(parsed, 100) : DEFAULT_TIMEOUT_MS;
}

function abortError(signal) {
  const error = new Error('aborted');
  error.name = 'AbortError';
  if (signal && signal.reason !== undefined) error.cause = signal.reason;
  return error;
}

function unref(timer) {
  if (timer && typeof timer.unref === 'function') timer.unref();
}

/**
 * POST the payload as gzip JSON. Resolves {status, retryAfter} (retryAfter in seconds, or null); rejects when the endpoint
 * cannot be reached or does not answer in time.
 *
 * options: headers; gzipLevel; agent and ca for the connection; timeoutMs, the deadline of the whole request; signal, an
 * AbortSignal that ends it; unref, so that the request does not keep the process alive.
 *
 * The deadline starts when the request is made and ends it wherever it is: compressing, waiting in the agent's queue,
 * connecting (a connect that nothing answers, as with dropped packets, is left to the kernel's retries, minutes, by
 * request.setTimeout, which only starts once the socket is connected), sending, or waiting for the answer.
 *
 * Node's http and https modules are used directly on purpose: a proxy set for the application must not redirect traffic
 * to the in-cluster gateway.
 */
function post(url, payload, options) {
  const settings = options || {};
  return new Promise((resolve, reject) => {
    let parts;
    try {
      parts = new URL(url);
    } catch (error) {
      reject(new Error('unsupported OTLP endpoint: ' + url));
      return;
    }
    if ((parts.protocol !== 'http:' && parts.protocol !== 'https:') || !parts.hostname) {
      reject(new Error('unsupported OTLP endpoint: ' + url));
      return;
    }
    const signal = settings.signal;
    if (signal && signal.aborted) {
      reject(abortError(signal));
      return;
    }

    let request = null;
    let finished = false;
    let deadline;
    const onAbort = () => finish(reject, abortError(signal));
    // The one place a request ends: the answer, an error, the deadline or the abort, whichever comes first.
    const finish = (settle, value) => {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      if (signal) signal.removeEventListener('abort', onAbort);
      if (request && settle === reject) request.destroy();
      settle(value);
    };
    const timeoutMs = settings.timeoutMs || KIT_TIMEOUT_MS;
    const started = Date.now();
    let extended = false;
    const expire = () => {
      if (!extended && Date.now() - started > timeoutMs + STALL_MS) {
        extended = true;
        deadline = setTimeout(expire, STALL_GRACE_MS);
        unref(deadline);
        return;
      }
      finish(reject, new Error('timed out'));
    };
    deadline = setTimeout(expire, timeoutMs);
    unref(deadline); // the request is what keeps a process alive, or is not meant to; the deadline only has to fire
    if (signal) signal.addEventListener('abort', onAbort, { once: true });

    zlib.gzip(Buffer.from(JSON.stringify(payload), 'utf8'), { level: settings.gzipLevel || 5 }, (zipError, body) => {
      if (finished) return;
      if (zipError) {
        finish(reject, zipError);
        return;
      }
      const headers = Object.assign(
        {
          'Content-Type': 'application/json',
          'Content-Encoding': 'gzip',
          'Content-Length': body.length,
          'User-Agent': USER_AGENT,
        },
        settings.headers || {}
      );
      const transport = parts.protocol === 'https:' ? https : http;
      request = transport.request(
        {
          protocol: parts.protocol,
          hostname: parts.hostname.replace(/^\[|\]$/g, ''),
          port: parts.port || undefined,
          path: (parts.pathname || '/') + (parts.search || ''),
          method: 'POST',
          headers,
          agent: settings.agent,
          ca: settings.ca,
        },
        (response) => {
          response.resume();
          response.on('end', () => {
            const raw = response.headers['retry-after'];
            const seconds = raw === undefined ? NaN : Number(raw);
            finish(resolve, { status: response.statusCode, retryAfter: Number.isFinite(seconds) ? seconds : null });
          });
          response.on('error', (error) => finish(reject, error));
        }
      );
      if (settings.unref) {
        request.on('socket', (socket) => {
          if (socket && typeof socket.unref === 'function') socket.unref();
        });
      }
      request.on('error', (error) => finish(reject, error));
      request.end(body);
    });
  });
}

module.exports = {
  KIT_ENDPOINT,
  USER_AGENT,
  anyValue,
  keyValues,
  environmentAttributes,
  defaultServiceName,
  resourceAttributes,
  inKit,
  tracesEndpoint,
  exporterHeaders,
  timeoutMs,
  post,
};
