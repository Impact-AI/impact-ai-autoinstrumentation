// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const report = require('../../src/impact/setup-report');
const { Sink, tmpdir, attributesOf, until, hangingServer, blackHole, runNode } = require('./helpers');

const ENV = { OTEL_RESOURCE_ATTRIBUTES: 'k8s.namespace.name=shop,k8s.deployment.name=support-assistant' };

function setupSpan(payload) {
  const resourceSpans = payload.resourceSpans[0];
  return { resource: attributesOf(resourceSpans.resource.attributes), span: resourceSpans.scopeSpans[0].spans[0], scope: resourceSpans.scopeSpans[0].scope };
}

test('a report is one impact.setup span with the state, the detail, the versions and both lists', () => {
  const payload = report.build('active', '', ENV, {
    'impact.setup.instrumentations': 'http,openai',
    'impact.setup.frameworks': ['openai 7.50.0'],
    'impact.setup.untraced': [],
  });
  const { resource, span, scope } = setupSpan(payload);
  assert.equal(span.name, 'impact.setup');
  assert.equal(span.kind, 1);
  assert.match(span.traceId, /^[0-9a-f]{32}$/);
  assert.match(span.spanId, /^[0-9a-f]{16}$/);
  assert.equal(span.startTimeUnixNano, span.endTimeUnixNano);
  assert.match(span.startTimeUnixNano, /^\d{19}$/);
  assert.equal(scope.name, 'impact.setup');
  const attributes = attributesOf(span.attributes);
  assert.equal(attributes['impact.setup.state'], 'active');
  assert.equal(attributes['impact.setup.detail'], '');
  assert.equal(attributes['impact.setup.node.version'], process.versions.node);
  assert.ok(attributes['impact.setup.package.version']);
  assert.equal(attributes['impact.setup.instrumentations'], 'http,openai');
  assert.equal(resource['k8s.namespace.name'], 'shop');
  assert.equal(resource['service.name'], 'support-assistant');
});

test('the lists are OTLP arrays of strings, an empty list is still there, never one joined string', () => {
  const { span } = setupSpan(report.build('active', '', ENV, { 'impact.setup.frameworks': ['a 1', 'b 2'], 'impact.setup.untraced': [] }));
  const byKey = Object.fromEntries(span.attributes.map((item) => [item.key, item.value]));
  assert.deepEqual(byKey['impact.setup.frameworks'], { arrayValue: { values: [{ stringValue: 'a 1' }, { stringValue: 'b 2' }] } });
  assert.deepEqual(byKey['impact.setup.untraced'], { arrayValue: { values: [] } });
});

test('the detail is cut at 200 characters', () => {
  const { span } = setupSpan(report.build('failed', 'x'.repeat(500), ENV));
  assert.equal(attributesOf(span.attributes)['impact.setup.detail'].length, 200);
});

test('a state the impact.ai service does not know is refused', () => {
  assert.throws(() => report.build('maybe', '', ENV), /unknown setup state/);
  assert.deepEqual(report.STATES, ['active', 'pre-existing', 'failed']);
});

test('the first process of a container to report claims it, the others do not', () => {
  const state = tmpdir();
  const env = { IMPACT_STATE_DIR: state };
  assert.equal(report.claim('active', 'a', env, '/payload'), true);
  assert.equal(report.claim('active', 'a', env, '/payload'), false);
  assert.equal(report.claim('active', 'b', env, '/payload'), true, 'other content is another report');
  assert.equal(report.claim('failed', 'a', env, '/payload'), true, 'another state is another report');
  assert.equal(fs.readdirSync(state).length, 3);
  assert.ok(fs.readdirSync(state).every((name) => /^\.impact-setup-[0-9a-f]{16}$/.test(name)));
});

test('when no directory is writable the report is sent anyway', () => {
  assert.equal(report.claim('active', 'a', { IMPACT_STATE_DIR: '/proc/nothing' }, '/proc/nothing-either'), true);
  assert.equal(report.claim('active', 'a', { IMPACT_STATE_DIR: '/proc/nothing' }, '/proc/nothing-either'), true);
});

test('the payload directory is the second place for the marker', () => {
  const root = tmpdir();
  assert.equal(report.claim('active', 'z', { IMPACT_STATE_DIR: '/proc/nothing' }, root), true);
  assert.equal(report.claim('active', 'z', { IMPACT_STATE_DIR: '/proc/nothing' }, root), false);
});

test('report sends the span to the gateway once for identical content', async () => {
  const sink = await new Sink().listen();
  try {
    const env = Object.assign({ IMPACT_OTLP_ENDPOINT: sink.url, IMPACT_STATE_DIR: tmpdir() }, ENV);
    report.report('active', '', env, '/payload', { 'impact.setup.frameworks': [], 'impact.setup.untraced': [] });
    report.report('active', '', env, '/payload', { 'impact.setup.frameworks': [], 'impact.setup.untraced': [] });
    await until(() => sink.requests.length >= 1);
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(sink.requests.length, 1);
    assert.equal(sink.requests[0].path, '/v1/traces');
    assert.equal(sink.spans()[0].name, 'impact.setup');
  } finally {
    await sink.close();
  }
});

test('a stood-down workload reports to the gateway only, never to the endpoint of its own tracer', async () => {
  const theirs = await new Sink().listen();
  try {
    const env = { OTEL_EXPORTER_OTLP_ENDPOINT: theirs.url, IMPACT_STATE_DIR: tmpdir() };
    report.report('pre-existing', 'dd-trace 5.2.0', env, '/payload');
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.equal(theirs.requests.length, 0);
  } finally {
    await theirs.close();
  }
});

test('a report that the gateway refuses is tried a second time', async () => {
  const sink = await new Sink([503, 200]).listen();
  try {
    report.report('failed', 'boom', { IMPACT_OTLP_ENDPOINT: sink.url, IMPACT_STATE_DIR: tmpdir() }, '/payload');
    await until(() => sink.requests.length >= 2, 4000);
    assert.equal(sink.requests.length, 2);
  } finally {
    await sink.close();
  }
});

test('reporting never throws, with an endpoint that is nonsense or content that is hostile', () => {
  const env = { IMPACT_OTLP_ENDPOINT: 'ht!tp://[', IMPACT_STATE_DIR: tmpdir() };
  assert.doesNotThrow(() => report.report('active', {}, env, '/payload', { 'impact.setup.frameworks': 5 }));
  assert.doesNotThrow(() => report.report('nonsense', 'x', env, '/payload'));
  assert.doesNotThrow(() => report.report('failed', undefined, undefined, undefined));
});

// The report is the one thing that holds a short-lived process open for the gateway: `node -e 0`, the first process of a
// container, waited 3.2 s for a gateway that hangs and 5.1 s for one whose packets are dropped. Every attempt is given half a
// second, the connect included.
test('both attempts of a report that gets no answer are given up on after half a second', { timeout: 15000 }, async () => {
  const hanging = await hangingServer();
  try {
    const env = { IMPACT_OTLP_ENDPOINT: hanging.url, IMPACT_STATE_DIR: tmpdir() };
    report.report('active', '', env, '/payload', { 'impact.setup.frameworks': [], 'impact.setup.untraced': [] });
    for (const attempt of ['first', 'second']) {
      await until(() => hanging.connections() === 1, 4000);
      const started = Date.now();
      await until(() => hanging.connections() === 0, 4000);
      const took = Date.now() - started;
      assert.ok(took >= 300 && took < 1200, 'the ' + attempt + ' attempt was given up on after ' + took + ' ms');
    }
  } finally {
    await hanging.close();
  }
});

const LINUX = process.platform === 'linux' ? false : 'a black hole needs the full accept queue of Linux';

async function lifetimeWith(gateway) {
  const script = [
    'const report = require(' + JSON.stringify(require.resolve('../../src/impact/setup-report')) + ');',
    "report.report('active', '', { IMPACT_OTLP_ENDPOINT: process.argv[1], IMPACT_STATE_DIR: process.argv[2] }, '/payload', {});",
    "process.stdout.write('WORK_DONE\\n');",
  ].join('\n');
  return runNode(['-e', script, gateway, tmpdir()], { cap: 20000 });
}

for (const [what, make, skip] of [
  ['hangs', hangingServer, false],
  ['drops its packets', blackHole, LINUX],
]) {
  test('a process with nothing to do waits half a second at most for a gateway that ' + what, { timeout: 30000, skip }, async () => {
    const gateway = await make();
    try {
      const result = await lifetimeWith(gateway.url);
      assert.equal(result.code, 0, result.stderr);
      assert.ok(!result.capped, 'the process was still running ' + result.afterWork + ' s after its work');
      assert.ok(result.afterWork < 1.5, 'the process needed ' + result.afterWork + ' s to end');
    } finally {
      await gateway.close();
    }
  });
}

// --- the tool beside the package ------------------------------------------------------------------------------------
// The service keeps impact.setup.tool, impact.setup.tool.version and impact.setup.exporter only as strings of these
// shapes, and impact.setup.alongside only as a boolean (a string "true" says nothing); it drops anything else.

const coexistence = require('../../src/impact/coexistence');
const detect = require('../../src/impact/detect');

const TOOL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const TOOL_VERSION = /^[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}$/;
const EXPORTER_KIND = /^[a-z][a-z0-9-]{0,31}$/;
// The tool ids impact.ai's service knows; a report names no other.
const KNOWN_TOOLS = new Set([
  'arize-ax', 'braintrust', 'datadog', 'dynatrace', 'elastic-edot', 'grafana-alloy', 'honeycomb', 'langfuse', 'langsmith',
  'litellm', 'logfire', 'newrelic', 'openlit', 'opentelemetry', 'phoenix', 'portkey', 'splunk-otel', 'traceloop', 'zipkin',
]);
const TOOL_KEYS = ['impact.setup.tool', 'impact.setup.tool.version', 'impact.setup.exporter', 'impact.setup.alongside'];

function toolWire(tool, started) {
  const payload = JSON.parse(JSON.stringify(report.build('pre-existing', 'x', ENV, coexistence.attributes(tool, started))));
  const out = {};
  for (const item of payload.resourceSpans[0].scopeSpans[0].spans[0].attributes) {
    if (TOOL_KEYS.includes(item.key)) out[item.key] = item.value;
  }
  return out;
}

test('the tool fields are strings of the shapes the service keeps, and alongside is a boolean', () => {
  const tool = { id: 'langsmith', version: '0.11.8', exporter: 'otlp-http' };
  assert.deepEqual(toolWire(tool, false), {
    'impact.setup.tool': { stringValue: 'langsmith' },
    'impact.setup.tool.version': { stringValue: '0.11.8' },
    'impact.setup.exporter': { stringValue: 'otlp-http' },
    'impact.setup.alongside': { boolValue: false },
  });
  assert.deepEqual(toolWire(tool, true)['impact.setup.alongside'], { boolValue: true });
  // A report that names no tool has none of the four.
  assert.deepEqual(toolWire(null, true), {});
});

test('every tool the package can name is one the service knows', () => {
  const named = new Set([...Object.values(detect.TOOLS), ...Object.keys(detect.TOOL_PACKAGES)]);
  for (const tool of named) {
    assert.ok(KNOWN_TOOLS.has(tool), tool);
    assert.match(tool, TOOL_ID);
  }
});

test('every exporter kind the package can name has the shape the service keeps', () => {
  for (const kind of [...detect.TRACES_EXPORTERS, ...Object.values(detect.OTLP_PROTOCOLS), detect.LANGSMITH_EXPORTER, 'zipkin']) {
    assert.match(kind, EXPORTER_KIND);
  }
});

for (const version of ['0.11.8', '1.0.0-rc.1', '2.0.0+build.1', '5.60.0']) {
  test('a version the package names has the shape the service keeps: ' + version, () => {
    const tool = detect.toolOf([{ kind: 'distribution', name: 'dd-trace', version }], { 'dd-trace': version }, {});
    assert.equal(tool.version, version);
    assert.match(tool.version, TOOL_VERSION);
  });
}
