// SPDX-License-Identifier: Apache-2.0
'use strict';

// Span exporter: OTLP/HTTP JSON to the gateway (or to ingest), on Node's own http and zlib. It is the exporter of a
// private tracer provider, so the application's own OpenTelemetry state is never touched, and it keeps the OpenTelemetry
// exporter packages (and the protocol buffer library they bring) out of the image.

const http = require('http');
const https = require('https');
const fs = require('fs');
const otlp = require('./otlp');
const { writeWarning } = require('./warn');

// One more try after a failed attempt: with the two seconds an attempt may take inside the kit, a batch for a gateway that
// does not answer costs at most 2 + 0.5 + 2 seconds.
const RETRIES = 1;
const PERMANENT = [400, 401, 403, 404, 405, 413, 415];
const WARN_EVERY_MS = 60000;
const MAX_RETRY_AFTER_S = 5;
const HAS_IS_REMOTE = 0x100;
const IS_REMOTE = 0x200;
const SUCCESS = 0;
const FAILED = 1;

function nanos(hrTime) {
  if (!hrTime) return '0';
  const seconds = hrTime[0];
  const rest = ('000000000' + hrTime[1]).slice(-9);
  return seconds === 0 ? String(Number(rest)) : String(seconds) + rest;
}

function put(target, key, value) {
  if (value === undefined || value === null || value === '' || value === 0) return;
  if (Array.isArray(value) && value.length === 0) return;
  target[key] = value;
}

function traceState(state) {
  return state && typeof state.serialize === 'function' ? state.serialize() : '';
}

function flags(context, remote) {
  return ((context && context.traceFlags) || 0) & 0xff | HAS_IS_REMOTE | (remote ? IS_REMOTE : 0);
}

function encodeEvent(event) {
  const out = { timeUnixNano: nanos(event.time), name: event.name };
  put(out, 'attributes', otlp.keyValues(event.attributes));
  put(out, 'droppedAttributesCount', event.droppedAttributesCount);
  return out;
}

function encodeLink(link) {
  const context = link.context;
  const out = { traceId: context.traceId, spanId: context.spanId };
  put(out, 'traceState', traceState(context.traceState));
  put(out, 'attributes', otlp.keyValues(link.attributes));
  put(out, 'droppedAttributesCount', link.droppedAttributesCount);
  out.flags = flags(context, false);
  return out;
}

function encodeSpan(span) {
  const context = span.spanContext();
  const parent = span.parentSpanContext || null;
  const out = { traceId: context.traceId, spanId: context.spanId };
  put(out, 'traceState', traceState(context.traceState));
  const parentId = parent ? parent.spanId : span.parentSpanId;
  if (parentId) out.parentSpanId = parentId;
  out.name = span.name;
  out.kind = span.kind + 1; // SDK INTERNAL=0..CONSUMER=4; OTLP INTERNAL=1..CONSUMER=5
  out.startTimeUnixNano = nanos(span.startTime);
  out.endTimeUnixNano = nanos(span.endTime);
  put(out, 'attributes', otlp.keyValues(span.attributes));
  put(out, 'droppedAttributesCount', span.droppedAttributesCount);
  put(out, 'events', (span.events || []).map(encodeEvent));
  put(out, 'droppedEventsCount', span.droppedEventsCount);
  put(out, 'links', (span.links || []).map(encodeLink));
  put(out, 'droppedLinksCount', span.droppedLinksCount);
  const status = {};
  put(status, 'message', span.status && span.status.message);
  put(status, 'code', span.status && span.status.code);
  out.status = status;
  out.flags = flags(parent || context, Boolean(parent && parent.isRemote));
  return out;
}

/** An ExportTraceServiceRequest in OTLP JSON, grouped by resource and instrumentation scope. */
function encodeSpans(spans) {
  const groups = [];
  const resources = new Map();
  for (const span of spans) {
    let entry = resources.get(span.resource);
    if (!entry) {
      entry = { resource: span.resource, scopes: new Map() };
      resources.set(span.resource, entry);
      groups.push(entry);
    }
    const scope = span.instrumentationScope || span.instrumentationLibrary || null;
    const key = scope ? scope.name + '\0' + (scope.version || '') + '\0' + (scope.schemaUrl || '') : '';
    let scoped = entry.scopes.get(key);
    if (!scoped) {
      scoped = { scope, spans: [] };
      entry.scopes.set(key, scoped);
    }
    scoped.spans.push(encodeSpan(span));
  }
  return {
    resourceSpans: groups.map((group) => {
      const scopeSpans = [];
      for (const scoped of group.scopes.values()) {
        const item = { scope: {}, spans: scoped.spans };
        if (scoped.scope) {
          put(item.scope, 'name', scoped.scope.name);
          put(item.scope, 'version', scoped.scope.version);
          put(item, 'schemaUrl', scoped.scope.schemaUrl);
        }
        scopeSpans.push(item);
      }
      const resource = { attributes: otlp.keyValues(group.resource.attributes) };
      const entry = { resource, scopeSpans };
      put(entry, 'schemaUrl', group.resource.schemaUrl);
      return entry;
    }),
  };
}

class ImpactSpanExporter {
  constructor(options) {
    const settings = options || {};
    const env = settings.env || process.env;
    this.url = otlp.tracesEndpoint(env);
    this.headers = otlp.exporterHeaders(env);
    this.timeoutMs = otlp.timeoutMs(env);
    // Inside the cluster the bytes are cheap and the CPU of the workload is not: the fastest level of gzip.
    this.gzipLevel = otlp.inKit(env) ? 1 : 5;
    this.ca = undefined;
    const certificate = otlp.inKit(env) ? undefined : env.OTEL_EXPORTER_OTLP_CERTIFICATE;
    if (certificate) {
      try {
        this.ca = fs.readFileSync(certificate);
      } catch (error) {
        this.ca = undefined;
      }
    }
    this.post = settings.post || otlp.post;
    this.sleep = settings.sleep || ((ms) => this.pause(ms));
    this.warn = settings.warn || writeWarning;
    // A keep-alive agent per scheme: the gateway is one address and a batch every few seconds should not reconnect. One
    // request at a time (the processor sends one batch at a time): a second one would wait in the agent's queue, and the
    // gateway that makes it wait is the one that does not answer. Idle sockets of an agent do not keep the process alive.
    const transport = this.url.indexOf('https:') === 0 ? https : http;
    this.agent = new transport.Agent({ keepAlive: true, maxSockets: 1 });
    this.stopped = false;
    this.exiting = false;
    this.waits = new Set(); // the pauses between attempts, ended when the process starts to exit
    this.running = new Set(); // the AbortController of every batch being sent
    this.lastWarning = -Infinity;
  }

  /** The process is exiting. What is being sent now is the last attempt: one that fails is not tried again. */
  beginShutdown() {
    this.exiting = true;
    for (const wake of Array.from(this.waits)) wake();
  }

  /** The process goes on after all: attempts are retried again. */
  endShutdown() {
    this.exiting = false;
  }

  /** End the attempts that are running. The exporter goes on working. */
  abort() {
    for (const controller of Array.from(this.running)) controller.abort();
  }

  // The wait before trying again. A timer that does not hold the process, and that ends at once when the process starts to
  // exit.
  pause(ms) {
    return new Promise((resolve) => {
      const wake = () => {
        clearTimeout(timer);
        this.waits.delete(wake);
        resolve();
      };
      const timer = setTimeout(wake, ms);
      if (timer.unref) timer.unref();
      this.waits.add(wake);
    });
  }

  export(spans, resultCallback) {
    if (this.stopped) {
      resultCallback({ code: FAILED });
      return;
    }
    let payload;
    try {
      payload = encodeSpans(spans);
    } catch (error) {
      this.warnRarely('could not encode ' + spans.length + ' spans: ' + error.message);
      resultCallback({ code: FAILED });
      return;
    }
    this.send(payload, spans.length).then(
      (ok) => resultCallback({ code: ok ? SUCCESS : FAILED }),
      () => resultCallback({ code: FAILED })
    );
  }

  async send(payload, count) {
    const controller = new AbortController();
    this.running.add(controller);
    let reason = 'no attempt';
    try {
      for (let attempt = 0; attempt <= RETRIES; attempt += 1) {
        // a process on its way out does not try again
        if (attempt > 0 && (this.exiting || this.stopped || controller.signal.aborted)) break;
        let delay = 500 * Math.pow(2, attempt);
        try {
          const response = await this.post(this.url, payload, {
            headers: this.headers,
            timeoutMs: this.timeoutMs,
            gzipLevel: this.gzipLevel,
            agent: this.agent,
            ca: this.ca,
            signal: controller.signal,
            // Once connected, a request in flight does not hold the process: the exit flush does, for as long as it waits.
            // (A connect that is still in progress does, until its deadline: Node cannot let go of it.)
            unref: true,
          });
          if (response.status >= 200 && response.status < 300) return true;
          reason = 'HTTP ' + response.status;
          if (PERMANENT.indexOf(response.status) >= 0) break;
          if (response.retryAfter !== null) delay = Math.min(response.retryAfter, MAX_RETRY_AFTER_S) * 1000;
        } catch (error) {
          reason = (error && error.name ? error.name : 'Error') + ': ' + (error && error.message);
        }
        if (attempt < RETRIES && !this.exiting && !this.stopped && !controller.signal.aborted) await this.sleep(delay);
      }
    } finally {
      this.running.delete(controller);
    }
    this.warnRarely('could not export ' + count + ' spans to ' + this.url + ': ' + reason);
    return false;
  }

  warnRarely(message) {
    const now = Date.now();
    if (now - this.lastWarning >= WARN_EVERY_MS) {
      this.lastWarning = now;
      try {
        this.warn(message);
      } catch (error) {
        // a closed stderr is not a reason to stop
      }
    }
  }

  forceFlush() {
    return Promise.resolve();
  }

  shutdown() {
    this.stopped = true;
    this.beginShutdown();
    this.abort();
    if (this.agent) this.agent.destroy();
    return Promise.resolve();
  }
}

module.exports = { ImpactSpanExporter, encodeSpans, nanos, SUCCESS, FAILED };
