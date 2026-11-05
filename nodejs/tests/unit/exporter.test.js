// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { TracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { JsonTraceSerializer } = require('@opentelemetry/otlp-transformer');
const exporterModule = require('../../src/impact/exporter');
const { Sink, until, hangingServer } = require('./helpers');

const { ImpactSpanExporter, encodeSpans, nanos } = exporterModule;

/** Spans from the real SDK: every kind, every attribute type, events, links, a parent, errors. */
function sdkSpans() {
  const memory = new InMemorySpanExporter();
  const provider = new TracerProvider({
    resource: resourceFromAttributes({ 'service.name': 'support-assistant', 'k8s.namespace.name': 'shop' }),
    spanProcessors: [new SimpleSpanProcessor({ exporter: memory })],
  });
  const tracer = provider.getTracer('test-scope', '1.2.3');
  const other = provider.getTracer('other-scope');
  const root = tracer.startSpan('root', { kind: api.SpanKind.SERVER, attributes: { s: 'text', i: 42, d: 1.5, b: true, big: 2 ** 40, strings: ['a', 'b'], numbers: [1, 2], empty: '' } });
  const context = api.trace.setSpan(api.context.active(), root);
  const child = tracer.startSpan('child', { kind: api.SpanKind.CLIENT, links: [{ context: root.spanContext(), attributes: { why: 'because' } }] }, context);
  child.addEvent('retry', { attempt: 2 });
  child.addEvent('plain');
  child.setStatus({ code: api.SpanStatusCode.ERROR, message: 'it broke' });
  child.end();
  const llm = other.startSpan('llm', { kind: 0 /* SpanKind: internal */, attributes: { 'gen_ai.input.messages': '[{"role":"user"}]' } }, context);
  llm.setStatus({ code: api.SpanStatusCode.OK });
  llm.end();
  root.end();
  return memory.getFinishedSpans();
}

// The official encoder writes defaults too; ours, like the Python package's, leaves them out. Compare what is set.
function plain(value) {
  if (Array.isArray(value)) return value.map(plain);
  if (value && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      // The OTLP JSON mapping writes 64-bit integers as decimal strings; the JavaScript encoder writes numbers. Both parse.
      const inner = key === 'intValue' ? String(value[key]) : plain(value[key]);
      if (inner === '' || inner === 0 || inner === false || (Array.isArray(inner) && inner.length === 0) || (inner && typeof inner === 'object' && !Array.isArray(inner) && Object.keys(inner).length === 0)) {
        if (!['stringValue', 'boolValue', 'intValue', 'doubleValue'].includes(key)) continue;
      }
      out[key] = inner;
    }
    return out;
  }
  return value;
}

test('the encoding agrees with the official OpenTelemetry JSON encoder on everything that is set', () => {
  const spans = sdkSpans();
  const ours = JSON.parse(JSON.stringify(encodeSpans(spans)));
  const official = JSON.parse(Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString('utf8'));
  const names = (payload) => payload.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans.map((span) => span.name))).sort();
  assert.deepEqual(names(ours), ['child', 'llm', 'root']);
  assert.deepEqual(plain(ours.resourceSpans[0].resource), plain(official.resourceSpans[0].resource));
  const byName = (payload) => Object.fromEntries(payload.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans.map((span) => [span.name, plain(span)]))));
  const left = byName(ours);
  const right = byName(official);
  for (const name of Object.keys(right)) {
    // The flags field is the OTLP 1.x one (the W3C flags and whether the parent is remote); older encoders leave it out.
    delete right[name].flags;
    assert.deepEqual({ ...left[name], flags: undefined }, { ...right[name], flags: undefined }, name);
  }
  const scopes = (payload) => payload.resourceSpans[0].scopeSpans.map((s) => plain(s.scope)).sort((a, b) => (a.name < b.name ? -1 : 1));
  assert.deepEqual(scopes(ours), scopes(official));
});

test('a span knows its parent, kind and status as OTLP has them', () => {
  const spans = encodeSpans(sdkSpans()).resourceSpans[0].scopeSpans.flatMap((s) => s.spans);
  const root = spans.find((s) => s.name === 'root');
  const child = spans.find((s) => s.name === 'child');
  assert.equal(root.kind, 2);
  assert.equal(root.parentSpanId, undefined);
  assert.equal(child.kind, 3);
  assert.equal(child.parentSpanId, root.spanId);
  assert.deepEqual(child.status, { message: 'it broke', code: 2 });
  assert.equal(spans.find((s) => s.name === 'llm').kind, 1);
  assert.match(root.startTimeUnixNano, /^\d{19}$/);
  assert.ok(BigInt(root.endTimeUnixNano) >= BigInt(root.startTimeUnixNano));
});

test('spans of one resource and scope are grouped, those of another scope are not', () => {
  const resourceSpans = encodeSpans(sdkSpans()).resourceSpans;
  assert.equal(resourceSpans.length, 1);
  assert.deepEqual(resourceSpans[0].scopeSpans.map((s) => [s.scope.name, s.spans.length]).sort(), [['other-scope', 1], ['test-scope', 2]]);
});

test('hrtime values become nanoseconds', () => {
  assert.equal(nanos([1700000000, 123456789]), '1700000000123456789');
  assert.equal(nanos([1700000000, 5]), '1700000000000000005');
  assert.equal(nanos([0, 42]), '42');
  assert.equal(nanos(undefined), '0');
});

function exporterWith(posts, extra) {
  const delays = [];
  const warnings = [];
  const instance = new ImpactSpanExporter({
    env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' },
    post: async () => {
      const next = posts.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    sleep: async (ms) => delays.push(ms),
    warn: (message) => warnings.push(message),
    ...extra,
  });
  return { instance, delays, warnings };
}

function exportAll(instance, spans) {
  return new Promise((resolve) => instance.export(spans || sdkSpans(), resolve));
}

test('inside the kit the batch is compressed at the fastest level, outside it at the usual one', async () => {
  const levels = [];
  const post = async (url, payload, options) => {
    levels.push(options.gzipLevel);
    return { status: 200, retryAfter: null };
  };
  await exportAll(new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' }, post }));
  await exportAll(new ImpactSpanExporter({ env: { OTEL_EXPORTER_OTLP_ENDPOINT: 'https://ingest.example.test' }, post }));
  assert.deepEqual(levels, [1, 5]);
});

test('a success is reported once, with no wait', async () => {
  const { instance, delays } = exporterWith([{ status: 200, retryAfter: null }]);
  assert.deepEqual(await exportAll(instance), { code: 0 });
  assert.deepEqual(delays, []);
});

test('a refusal that may pass is retried once after half a second, then reported once', async () => {
  // With the two seconds an attempt may take, a batch for a gateway that does not answer costs at most 2 + 0.5 + 2 seconds.
  const { instance, delays, warnings } = exporterWith([{ status: 503, retryAfter: null }, { status: 503, retryAfter: null }, { status: 503, retryAfter: null }]);
  assert.deepEqual(await exportAll(instance), { code: 1 });
  assert.deepEqual(delays, [500]);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /could not export 3 spans to http:\/\/gateway:4318\/v1\/traces: HTTP 503/);
});

test('Retry-After is honoured, up to five seconds', async () => {
  const waits = exporterWith([{ status: 429, retryAfter: 2 }, { status: 200, retryAfter: null }]);
  assert.deepEqual(await exportAll(waits.instance), { code: 0 });
  assert.deepEqual(waits.delays, [2000]);
  const capped = exporterWith([{ status: 503, retryAfter: 60 }, { status: 200, retryAfter: null }]);
  assert.deepEqual(await exportAll(capped.instance), { code: 0 });
  assert.deepEqual(capped.delays, [5000]);
});

test('inside the kit an attempt is given two seconds, from the moment it is made', async () => {
  const deadlines = [];
  const post = async (url, payload, options) => {
    deadlines.push(options.timeoutMs);
    throw new Error('timed out');
  };
  await exportAll(new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' }, post, sleep: async () => {}, warn: () => {} }));
  await exportAll(new ImpactSpanExporter({ env: { OTEL_EXPORTER_OTLP_ENDPOINT: 'https://ingest.example.test', OTEL_EXPORTER_OTLP_TIMEOUT: '3000' }, post, sleep: async () => {}, warn: () => {} }));
  assert.deepEqual(deadlines, [2000, 2000, 3000, 3000]);
});

test('once the process is exiting a failed attempt is final: no wait, no second try', async () => {
  const { instance, delays } = exporterWith([new Error('timed out'), { status: 200, retryAfter: null }]);
  instance.beginShutdown();
  assert.deepEqual(await exportAll(instance), { code: 1 });
  assert.deepEqual(delays, []);
});

test('an attempt that is waiting to try again stops waiting when the exit begins, and does not try again', async () => {
  const posts = [];
  const instance = new ImpactSpanExporter({
    env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' },
    post: async () => {
      posts.push(Date.now());
      throw new Error('timed out');
    },
    warn: () => {},
  });
  setTimeout(() => instance.beginShutdown(), 100);
  const started = Date.now();
  assert.deepEqual(await exportAll(instance), { code: 1 });
  assert.equal(posts.length, 1);
  assert.ok(Date.now() - started < 400, 'the wait of half a second was sat out');
});

test('a healthy gateway is still given every batch while the process exits', async () => {
  const sink = await new Sink().listen();
  const instance = new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: sink.url } });
  try {
    instance.beginShutdown();
    assert.deepEqual(await exportAll(instance), { code: 0 });
    assert.deepEqual(await exportAll(instance), { code: 0 });
    assert.equal(sink.requests.length, 2);
  } finally {
    await instance.shutdown();
    await sink.close();
  }
});

test('abort ends the attempts that are running, and the exporter goes on working', async () => {
  const hanging = await hangingServer();
  const sink = await new Sink().listen();
  const warnings = [];
  const instance = new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: hanging.url }, warn: (message) => warnings.push(message) });
  try {
    const running = exportAll(instance);
    await until(() => hanging.connections() === 1);
    const started = Date.now();
    instance.abort();
    assert.deepEqual(await running, { code: 1 });
    assert.ok(Date.now() - started < 500, 'the attempt was not ended');
    instance.url = sink.url + '/v1/traces';
    assert.deepEqual(await exportAll(instance), { code: 0 });
  } finally {
    await instance.shutdown();
    await sink.close();
    await hanging.close();
  }
});

test('after shutdown a retry that was waiting is not made', async () => {
  const posts = [];
  const instance = new ImpactSpanExporter({
    env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' },
    post: async () => {
      posts.push(1);
      throw new Error('connect ECONNREFUSED');
    },
    sleep: async () => instance.shutdown(),
    warn: () => {},
  });
  assert.deepEqual(await exportAll(instance), { code: 1 });
  assert.equal(posts.length, 1);
});

test('the exporter sends one request at a time: its agent has one socket', () => {
  // A second request would wait in the agent's queue, and the gateway that makes it wait is the one that does not answer.
  const instance = new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' } });
  assert.equal(instance.agent.maxSockets, 1);
  return instance.shutdown();
});

for (const status of [400, 401, 403, 404, 405, 413, 415]) {
  test('HTTP ' + status + ' is permanent: no retry', async () => {
    const { instance, delays } = exporterWith([{ status, retryAfter: null }]);
    assert.deepEqual(await exportAll(instance), { code: 1 });
    assert.deepEqual(delays, []);
  });
}

test('a connection that fails is retried, and what finally works is a success', async () => {
  const { instance, delays } = exporterWith([new Error('connect ECONNREFUSED'), { status: 202, retryAfter: null }]);
  assert.deepEqual(await exportAll(instance), { code: 0 });
  assert.deepEqual(delays, [500]);
});

test('a warning is given once a minute, not for every batch', async () => {
  const { instance, warnings } = exporterWith(Array.from({ length: 9 }, () => ({ status: 401, retryAfter: null })));
  for (let i = 0; i < 3; i += 1) await exportAll(instance);
  assert.equal(warnings.length, 1);
});

test('after shutdown nothing is sent', async () => {
  const { instance } = exporterWith([{ status: 200, retryAfter: null }]);
  await instance.shutdown();
  assert.deepEqual(await exportAll(instance), { code: 1 });
});

test('a span the encoder cannot read is reported as a failure and does not throw', async () => {
  const { instance, warnings } = exporterWith([]);
  assert.deepEqual(await exportAll(instance, [{ nonsense: true }]), { code: 1 });
  assert.equal(warnings.length, 1);
});

test('against a real receiver: gzip JSON to /v1/traces, with the resource of the workload', async () => {
  const sink = await new Sink().listen();
  const instance = new ImpactSpanExporter({ env: { IMPACT_OTLP_ENDPOINT: sink.url } });
  try {
    assert.deepEqual(await exportAll(instance), { code: 0 });
    await until(() => sink.requests.length === 1);
    assert.equal(sink.requests[0].path, '/v1/traces');
    assert.equal(sink.requests[0].headers['content-encoding'], 'gzip');
    assert.deepEqual(sink.spans().map((s) => s.name).sort(), ['child', 'llm', 'root']);
  } finally {
    await instance.shutdown();
    await sink.close();
  }
});
