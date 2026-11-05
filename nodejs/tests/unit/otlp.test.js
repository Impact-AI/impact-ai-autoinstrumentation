// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('http');
const zlib = require('zlib');
const otlp = require('../../src/impact/otlp');
const { Sink, hangingServer, blackHole, runNode } = require('./helpers');

test('values are encoded as OTLP JSON has them, 64-bit integers as decimal strings', () => {
  assert.deepEqual(otlp.anyValue('x'), { stringValue: 'x' });
  assert.deepEqual(otlp.anyValue(true), { boolValue: true });
  assert.deepEqual(otlp.anyValue(7), { intValue: '7' });
  assert.deepEqual(otlp.anyValue(-3), { intValue: '-3' });
  assert.deepEqual(otlp.anyValue(1.5), { doubleValue: 1.5 });
  assert.deepEqual(otlp.anyValue(12345678901234567890n), { intValue: '12345678901234567890' });
  assert.deepEqual(otlp.anyValue(NaN), { stringValue: 'NaN' });
  assert.deepEqual(otlp.anyValue(Infinity), { stringValue: 'Infinity' });
  assert.deepEqual(otlp.anyValue(['a', 1]), { arrayValue: { values: [{ stringValue: 'a' }, { intValue: '1' }] } });
  assert.deepEqual(otlp.anyValue(new Uint8Array([1, 2])), { bytesValue: 'AQI=' });
  assert.deepEqual(otlp.anyValue({ a: 1 }), { kvlistValue: { values: [{ key: 'a', value: { intValue: '1' } }] } });
});

test('key values skip undefined and null', () => {
  assert.deepEqual(otlp.keyValues({ a: 'x', b: undefined, c: null, d: 0 }), [
    { key: 'a', value: { stringValue: 'x' } },
    { key: 'd', value: { intValue: '0' } },
  ]);
  assert.deepEqual(otlp.keyValues(undefined), []);
});

test('the resource is what OTEL_RESOURCE_ATTRIBUTES says, percent-decoded, plus the distro', () => {
  const attributes = otlp.resourceAttributes({
    OTEL_RESOURCE_ATTRIBUTES: 'k8s.namespace.name=shop,k8s.deployment.name=support-assistant,note=a%20b,broken,=x',
  });
  assert.equal(attributes['k8s.namespace.name'], 'shop');
  assert.equal(attributes.note, 'a b');
  assert.equal(attributes['telemetry.sdk.language'], 'nodejs');
  assert.equal(attributes['telemetry.distro.name'], 'impact-instrumentation');
  assert.ok(!('broken' in attributes) && !('' in attributes));
});

test('the service is named after the workload when nothing names it', () => {
  assert.equal(otlp.resourceAttributes({ OTEL_RESOURCE_ATTRIBUTES: 'k8s.deployment.name=chat,k8s.pod.name=chat-x' })['service.name'], 'chat');
  assert.equal(otlp.resourceAttributes({ OTEL_RESOURCE_ATTRIBUTES: 'k8s.statefulset.name=db' })['service.name'], 'db');
  assert.equal(otlp.resourceAttributes({ OTEL_RESOURCE_ATTRIBUTES: 'k8s.cronjob.name=nightly' })['service.name'], 'nightly');
  assert.equal(otlp.resourceAttributes({})['service.name'], 'unknown_service:node');
});

test('OTEL_SERVICE_NAME and a service.name attribute win over the workload', () => {
  assert.equal(otlp.resourceAttributes({ OTEL_SERVICE_NAME: 'mine', OTEL_RESOURCE_ATTRIBUTES: 'k8s.deployment.name=chat' })['service.name'], 'mine');
  assert.equal(otlp.resourceAttributes({ OTEL_RESOURCE_ATTRIBUTES: 'service.name=theirs,k8s.deployment.name=chat' })['service.name'], 'theirs');
  assert.equal(otlp.defaultServiceName({ OTEL_SERVICE_NAME: 'mine' }), null);
});

test('inside the kit spans go to the gateway, whatever else the workload set', () => {
  const env = { IMPACT_OTLP_ENDPOINT: 'http://gateway.impact-ai.svc:4318/', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://theirs:4318', OTEL_EXPORTER_OTLP_HEADERS: 'k=v', OTEL_EXPORTER_OTLP_TIMEOUT: '1' };
  assert.equal(otlp.tracesEndpoint(env), 'http://gateway.impact-ai.svc:4318/v1/traces');
  assert.deepEqual(otlp.exporterHeaders(env), {});
  // What one attempt to reach the in-cluster gateway may take, from the moment the request is made. The gateway answers in
  // milliseconds when it works; when it does not, every second is one a process holds on to its spans, and one its exit
  // waits for.
  assert.equal(otlp.timeoutMs(env), 2000);
});

test('outside the kit the standard OpenTelemetry settings apply', () => {
  assert.equal(otlp.tracesEndpoint({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://ingest.example.test/' }), 'https://ingest.example.test/v1/traces');
  assert.equal(otlp.tracesEndpoint({ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'https://x/custom', OTEL_EXPORTER_OTLP_ENDPOINT: 'https://y' }), 'https://x/custom');
  assert.equal(otlp.tracesEndpoint({}), 'http://localhost:4318/v1/traces');
  assert.deepEqual(otlp.exporterHeaders({ OTEL_EXPORTER_OTLP_HEADERS: 'authorization=Bearer%20abc, x-a=1' }), { authorization: 'Bearer abc', 'x-a': '1' });
  assert.equal(otlp.timeoutMs({ OTEL_EXPORTER_OTLP_TIMEOUT: '2500' }), 2500);
  assert.equal(otlp.timeoutMs({ OTEL_EXPORTER_OTLP_TIMEOUT: 'soon' }), 10000);
});

test('post sends gzip JSON with the headers it is given and reports the status and Retry-After', async () => {
  const sink = await new Sink([{ status: 503, retryAfter: 2 }, 200]).listen();
  try {
    const first = await otlp.post(sink.url + '/v1/traces', { a: 1 }, { headers: { authorization: 'Bearer k' } });
    assert.deepEqual(first, { status: 503, retryAfter: 2 });
    const second = await otlp.post(sink.url + '/v1/traces?x=1', { a: 2 });
    assert.deepEqual(second, { status: 200, retryAfter: null });
    const [one, two] = sink.requests;
    assert.equal(one.headers['content-encoding'], 'gzip');
    assert.equal(one.headers['content-type'], 'application/json');
    assert.equal(one.headers.authorization, 'Bearer k');
    assert.match(one.headers['user-agent'], /^impact-instrumentation\/\S+ node\/\d+/);
    assert.deepEqual(JSON.parse(zlib.gunzipSync(one.body)), { a: 1 });
    assert.equal(two.path, '/v1/traces?x=1');
  } finally {
    await sink.close();
  }
});

test('post rejects an endpoint it cannot use, one that is not there, and one that does not answer', async () => {
  await assert.rejects(otlp.post('ftp://x/y', {}), /unsupported OTLP endpoint/);
  await assert.rejects(otlp.post('not a url', {}), /unsupported OTLP endpoint/);
  await assert.rejects(otlp.post('http://127.0.0.1:9/v1/traces', {}, { timeoutMs: 500 }));
  const slow = await new Sink([], 1500).listen();
  try {
    await assert.rejects(otlp.post(slow.url, {}, { timeoutMs: 200 }), /timed out/);
  } finally {
    await slow.close();
  }
});

const LINUX = process.platform === 'linux' ? false : 'a black hole needs the full accept queue of Linux';

test('the deadline of a request covers the connect, which request.setTimeout does not', { timeout: 15000, skip: LINUX }, async () => {
  const hole = await blackHole();
  try {
    const started = Date.now();
    await assert.rejects(otlp.post(hole.url + '/v1/traces', {}, { timeoutMs: 300 }), /timed out/);
    const took = Date.now() - started;
    assert.ok(took >= 250 && took < 2000, 'the request needed ' + took + ' ms to give up on a connect with a deadline of 300');
  } finally {
    await hole.close();
  }
});

test('the deadline of a request covers a server that accepts and never answers', { timeout: 15000 }, async () => {
  const hanging = await hangingServer();
  try {
    const started = Date.now();
    await assert.rejects(otlp.post(hanging.url, {}, { timeoutMs: 300 }), /timed out/);
    assert.ok(Date.now() - started < 2000);
  } finally {
    await hanging.close();
  }
});

test('the deadline starts when the request is made: one that waits in the agent behind another is cut off with it', { timeout: 15000 }, async () => {
  const hanging = await hangingServer();
  const agent = new http.Agent({ keepAlive: true, maxSockets: 1 });
  try {
    const started = Date.now();
    const attempts = [1, 2, 3].map(() => otlp.post(hanging.url, {}, { timeoutMs: 500, agent }).then(() => 'answered', () => Date.now() - started));
    const ended = await Promise.all(attempts);
    // Counted from when it is made, all three are over at 500 ms; counted from when it gets a socket, the third at 1,500.
    for (const took of ended) assert.ok(typeof took === 'number' && took < 1100, 'a request in the queue of the agent ended after ' + took + ' ms');
  } finally {
    agent.destroy();
    await hanging.close();
  }
});

test('a request can be aborted, and one that is already aborted sends nothing', { timeout: 15000 }, async () => {
  const hanging = await hangingServer();
  try {
    const controller = new AbortController();
    const running = otlp.post(hanging.url, {}, { timeoutMs: 10000, signal: controller.signal });
    setTimeout(() => controller.abort(), 100);
    const started = Date.now();
    await assert.rejects(running, /abort/i);
    assert.ok(Date.now() - started < 2000);
    await assert.rejects(otlp.post(hanging.url, {}, { signal: controller.signal }), /abort/i);
  } finally {
    await hanging.close();
  }
});

test('a request made not to hold the process does not: a process whose only work is a hung request ends at once', { timeout: 30000 }, async () => {
  const hanging = await hangingServer();
  try {
    const script = "const otlp = require(" + JSON.stringify(require.resolve('../../src/impact/otlp')) + "); otlp.post(process.argv[1], {}, { timeoutMs: 20000, unref: true }).catch(() => {}); process.stdout.write('WORK_DONE\\n');";
    const result = await runNode(['-e', script, hanging.url], { cap: 15000 });
    assert.equal(result.code, 0, result.stderr);
    assert.ok(!result.capped && result.afterWork < 2, 'the process needed ' + result.afterWork + ' s to end');
  } finally {
    await hanging.close();
  }
});

test('a deadline reached because the process did not run its event loop is not held against the gateway', { timeout: 15000 }, async () => {
  // An application that loads its modules blocks its loop for a second or more, with the first request already made. When
  // the loop runs again the timer is due before the answer that has come in is read: the request is not given up on then.
  const sink = await new Sink().listen();
  try {
    const answer = otlp.post(sink.url + '/v1/traces', { a: 1 }, { timeoutMs: 300 });
    const until = Date.now() + 800;
    while (Date.now() < until); // busy: nothing is read, nothing is sent
    assert.deepEqual(await answer, { status: 200, retryAfter: null });
  } finally {
    await sink.close();
  }
});

test('a stalled loop buys a deadline one short extension, no more', { timeout: 15000 }, async () => {
  const hanging = await hangingServer();
  try {
    const started = Date.now();
    const answer = otlp.post(hanging.url, {}, { timeoutMs: 300 });
    const until = Date.now() + 800;
    while (Date.now() < until);
    await assert.rejects(answer, /timed out/);
    assert.ok(Date.now() - started < 2500, 'the request outlived its extension');
  } finally {
    await hanging.close();
  }
});
