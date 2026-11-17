// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const bootstrap = require('../../src/impact/bootstrap');
const { Sink, tmpdir, fakePackage, attributesOf, until } = require('./helpers');

const NODE22 = '22.23.4';

/** A runtime that records what it was asked and arms nothing. */
function fakeRuntime(result, fail) {
  const calls = [];
  return {
    calls,
    arm(input) {
      calls.push(input);
      if (fail) throw new Error(fail);
      return Object.assign({ armed: ['http'], skipped: {}, traced: new Set(), suppress: (fn) => fn() }, result);
    },
  };
}

async function started(files, overrides) {
  const sink = await new Sink().listen();
  const application = tmpdir();
  for (const [name, version] of Object.entries(files || {})) fakePackage(application, name, version);
  const env = Object.assign({ IMPACT_OTLP_ENDPOINT: sink.url, IMPACT_STATE_DIR: tmpdir(), OTEL_RESOURCE_ATTRIBUTES: 'k8s.namespace.name=shop,k8s.deployment.name=chat' }, (overrides && overrides.env) || {});
  const runtime = (overrides && overrides.runtime) || fakeRuntime();
  const root = tmpdir();
  const outcome = bootstrap.start(Object.assign({ env, root, cwd: application, argv: ['node', path.join(application, 'server.js')], execArgv: [], nodeVersion: NODE22, runtime, cache: {}, globalState: {} }, overrides && overrides.settings));
  const reports = async (count) => {
    await until(() => sink.requests.length >= count, 3000);
    return sink.spans().map((span) => attributesOf(span.attributes));
  };
  return { outcome, sink, env, runtime, root, application, reports };
}

test('OTEL_SDK_DISABLED=true: nothing is loaded, nothing is reported', async () => {
  const run = await started({}, { env: { OTEL_SDK_DISABLED: 'True' } });
  try {
    assert.deepEqual(run.outcome, { state: 'disabled', detail: '' });
    assert.equal(run.runtime.calls.length, 0);
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(run.sink.requests.length, 0);
  } finally {
    await run.sink.close();
  }
});

test('a package manager\'s own process starts nothing', async () => {
  const run = await started({}, { settings: { argv: ['node', '/usr/local/lib/node_modules/npm/bin/npm-cli.js', 'start'] } });
  try {
    assert.equal(run.outcome.state, 'skipped');
    assert.equal(run.runtime.calls.length, 0);
    assert.equal(run.env.OTEL_LOGS_EXPORTER, undefined);
  } finally {
    await run.sink.close();
  }
});

// Node 20 reached its end of life on 2026-04-30 and the image has no tree for it: a workload on it, or on any Node below
// 22.15, starts as it would without the kit, and the report says why.
for (const version of ['18.20.4', '20.20.2', '22.14.0']) {
  test('Node ' + version + ' is unsupported: the report says why, nothing is loaded and no setting is touched', async () => {
    const detail = 'Impact AI traces Node.js 22.15 and later; this workload runs Node.js ' + version;
    const run = await started({ openai: '5.0.0' }, { settings: { nodeVersion: version } });
    try {
      assert.deepEqual(run.outcome, { state: 'failed', detail });
      const [report] = await run.reports(1);
      assert.equal(report['impact.setup.state'], 'failed');
      assert.equal(report['impact.setup.detail'], detail);
      assert.deepEqual(report['impact.setup.frameworks'], []);
      assert.equal(report['impact.setup.node.tree'], undefined);
      assert.equal(run.runtime.calls.length, 0);
      assert.equal(run.env.OTEL_LOGS_EXPORTER, undefined);
    } finally {
      await run.sink.close();
    }
  });
}

test('a workload with a tracer of its own is left alone: pre-existing, named, environment untouched', async () => {
  const before = { OTEL_RESOURCE_ATTRIBUTES: 'k8s.namespace.name=shop,k8s.deployment.name=chat' };
  const run = await started({ 'dd-trace': '5.2.0', openai: '5.0.0', '@anthropic-ai/sdk': '0.140.0' }, { env: before });
  try {
    assert.deepEqual(run.outcome, { state: 'pre-existing', detail: 'dd-trace 5.2.0' });
    const [report] = await run.reports(1);
    assert.equal(report['impact.setup.state'], 'pre-existing');
    assert.equal(report['impact.setup.detail'], 'dd-trace 5.2.0');
    // it still says which AI libraries the workload has, and that the image could have traced them
    assert.deepEqual(report['impact.setup.frameworks'], ['anthropic-ai-sdk 0.140.0', 'openai 5.0.0']);
    assert.deepEqual(report['impact.setup.untraced'], []);
    assert.equal(run.runtime.calls.length, 0, 'the OpenTelemetry packages of the image were not even loaded');
    for (const name of ['OTEL_LOGS_EXPORTER', 'OTEL_METRICS_EXPORTER', 'ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE']) {
      assert.equal(run.env[name], undefined, name);
    }
  } finally {
    await run.sink.close();
  }
});

test('an OpenTelemetry pipeline of the workload\'s own (exporter settings and the SDK) stands it down too', async () => {
  const run = await started({ '@opentelemetry/sdk-node': '0.300.0' }, { env: { OTEL_EXPORTER_OTLP_ENDPOINT: 'http://theirs:4318' } });
  try {
    assert.equal(run.outcome.state, 'pre-existing');
    assert.match(run.outcome.detail, /^OTEL_EXPORTER_OTLP_ENDPOINT \(set by the workload\)$/);
  } finally {
    await run.sink.close();
  }
});

test('starting: the defaults are set, the runtime is asked for the tree of this Node, and the report says what is armed', async () => {
  const runtime = fakeRuntime({ armed: ['http', 'openai'], traced: new Set(['openai']) });
  const run = await started({ openai: '5.23.2', express: '5.0.0', '@langchain/core': '0.3.0' }, { runtime });
  try {
    assert.deepEqual(run.outcome, { state: 'active', detail: 'http,openai' });
    assert.equal(run.env.OTEL_LOGS_EXPORTER, 'none');
    assert.equal(run.env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE, 'content');
    const [call] = run.runtime.calls;
    assert.equal(call.tree.name, 'node22');
    assert.equal(call.nodeMajor, 22);
    assert.equal(call.moduleSystem, 'cjs');
    assert.equal(call.packages.express, '5.0.0');
    assert.equal(call.decisions.openai.action, 'trace');
    const [report] = await run.reports(1);
    assert.equal(report['impact.setup.state'], 'active');
    assert.equal(report['impact.setup.instrumentations'], 'http,openai');
    assert.equal(report['impact.setup.node.tree'], 'node22');
    assert.deepEqual(report['impact.setup.frameworks'], ['langchain-core 0.3.0', 'openai 5.23.2']);
    assert.deepEqual(report['impact.setup.untraced'], ['langchain-core 0.3.0']);
    assert.equal(report['impact.setup.skipped'], undefined);
  } finally {
    await run.sink.close();
  }
});

for (const version of ['22.15.0', '24.18.0', '26.10.0']) {
  test('Node ' + version + ' gets the node22 tree', async () => {
    const run = await started({}, { settings: { nodeVersion: version } });
    try {
      assert.equal(run.runtime.calls[0].tree.name, 'node22');
      assert.equal((await run.reports(1))[0]['impact.setup.node.tree'], 'node22');
    } finally {
      await run.sink.close();
    }
  });
}

test('a library the image cannot trace is listed as untraced, with the reason in skipped', async () => {
  const run = await started({ openai: '3.9.0', '@anthropic-ai/sdk': '0.100.0' });
  try {
    const [report] = await run.reports(1);
    assert.deepEqual(report['impact.setup.untraced'], ['anthropic-ai-sdk 0.100.0', 'openai 3.9.0']);
    assert.match(report['impact.setup.skipped'], /^anthropic-ai-sdk: @anthropic-ai\/sdk 0\.100\.0 installed, supported 0\.134\.0 or later; openai: openai 3\.9\.0 installed, supported 4 to 7$/);
  } finally {
    await run.sink.close();
  }
});

test('an ES module whose cell the matrix did not prove: the library is untraced and the reason says esm', async () => {
  const application = tmpdir();
  fs.writeFileSync(path.join(application, 'server.mjs'), '');
  const run = await started({}, { settings: { cwd: application, argv: ['node', path.join(application, 'server.mjs')] } });
  try {
    assert.equal(run.runtime.calls[0].moduleSystem, 'esm');
  } finally {
    await run.sink.close();
  }
  const sink = await new Sink().listen();
  try {
    fakePackage(application, 'openai', '6.0.0');
    const env = { IMPACT_OTLP_ENDPOINT: sink.url, IMPACT_STATE_DIR: tmpdir() };
    const outcome = bootstrap.start({ env, root: tmpdir(), cwd: application, argv: ['node', path.join(application, 'server.mjs')], execArgv: [], nodeVersion: NODE22, runtime: fakeRuntime(), cache: {}, globalState: {} });
    assert.equal(outcome.state, 'active');
    await until(() => sink.requests.length >= 1);
    const report = attributesOf(sink.spans()[0].attributes);
    assert.deepEqual(report['impact.setup.untraced'], ['openai 6.0.0']);
    assert.match(report['impact.setup.skipped'], /^openai: esm: not proven for Node 22 \(6\.0\.0\)$/);
  } finally {
    await sink.close();
  }
});

test('when the runtime cannot start: failed with the reason, the defaults taken back, the application\'s own values kept', async () => {
  const runtime = fakeRuntime({}, 'Cannot find module \'@opentelemetry/api\'\nRequire stack:\n- x');
  const run = await started({}, { runtime, env: { OTEL_METRICS_EXPORTER: 'prometheus' } });
  try {
    assert.equal(run.outcome.state, 'failed');
    assert.equal(run.outcome.detail, 'Error: Cannot find module \'@opentelemetry/api\'');
    const [report] = await run.reports(1);
    assert.equal(report['impact.setup.state'], 'failed');
    assert.equal(report['impact.setup.detail'], 'Error: Cannot find module \'@opentelemetry/api\'');
    assert.equal(run.env.OTEL_LOGS_EXPORTER, undefined, 'a default the package set is removed again');
    assert.equal(run.env.OTEL_METRICS_EXPORTER, 'prometheus', 'the workload\'s own value stays');
  } finally {
    await run.sink.close();
  }
});

test('start never throws, whatever it is given', () => {
  for (const settings of [undefined, {}, { env: null }, { argv: null }, { root: 5 }, { nodeVersion: 'banana' }, { runtime: { arm() { throw 5; } } }, { cwd: 12 }]) {
    assert.doesNotThrow(() => bootstrap.start(settings), JSON.stringify(settings));
  }
});

// --- the tool beside the package, and the coexistence list -------------------------------------------------------------

const TOOL_KEYS = ['impact.setup.tool', 'impact.setup.tool.version', 'impact.setup.exporter', 'impact.setup.alongside'];

function toolAttributes(report) {
  const out = {};
  for (const key of TOOL_KEYS) if (key in report) out[key] = report[key];
  return out;
}

/** A generic fixture list: these cells, on Node 22. */
function list(...cells) {
  return { schema: 'impact.coexistence.v1', cells };
}

function alongsideCell(tool, major, answers) {
  return Object.assign(
    { tool, mode: 'alongside', node: 22, major, version: major + '.0.0', started: true, traced: true, same: true, proof: 'nodejs/tests/image/test_example.py' },
    answers || {}
  );
}

test('a stood-down workload names the tool, its version, and that it did not start beside it', async () => {
  const run = await started({ 'dd-trace': '5.2.0', openai: '5.0.0' });
  try {
    assert.equal(run.outcome.state, 'pre-existing');
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {
      'impact.setup.tool': 'datadog',
      'impact.setup.tool.version': '5.2.0',
      'impact.setup.alongside': false,
    });
  } finally {
    await run.sink.close();
  }
});

test('a tool the list proves runs alongside: the runtime is armed and the report says so', async () => {
  const run = await started({ openlit: '1.9.0', openai: '5.0.0' }, { settings: { coexistence: list(alongsideCell('openlit', '1')) } });
  try {
    assert.equal(run.outcome.state, 'active');
    assert.equal(run.runtime.calls.length, 1);
    const [report] = await run.reports(1);
    assert.equal(report['impact.setup.state'], 'active');
    assert.equal(report['impact.setup.detail'], '');
    assert.deepEqual(toolAttributes(report), {
      'impact.setup.tool': 'openlit',
      'impact.setup.tool.version': '1.9.0',
      'impact.setup.alongside': true,
    });
  } finally {
    await run.sink.close();
  }
});

for (const [why, table] of [
  ['the shipped list', undefined],
  ['another series', list(alongsideCell('openlit', '2'))],
  ['another Node', list(alongsideCell('openlit', '1', { node: 24 }))],
  ['a failed proof', list(alongsideCell('openlit', '1', { same: false }))],
  ['another tool', list(alongsideCell('langfuse', '1'))],
  ['a list that is not one', { schema: 'x' }],
]) {
  test('a tool the list does not prove keeps the stand-down: ' + why, async () => {
    const run = await started({ openlit: '1.9.0' }, { settings: { coexistence: table } });
    try {
      assert.deepEqual(run.outcome, { state: 'pre-existing', detail: 'openlit 1.9.0' });
      assert.equal(run.runtime.calls.length, 0);
      const [report] = await run.reports(1);
      assert.equal(report['impact.setup.alongside'], false);
    } finally {
      await run.sink.close();
    }
  });
}

test('LangSmith\'s default tracing is named and the package runs beside it', async () => {
  const run = await started({ langsmith: '0.11.8', openai: '5.0.0' }, { env: { LANGSMITH_TRACING: 'true', LANGSMITH_API_KEY: 'lsv2_pt_planted' } });
  try {
    assert.equal(run.outcome.state, 'active');
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {
      'impact.setup.tool': 'langsmith',
      'impact.setup.tool.version': '0.11.8',
      'impact.setup.alongside': true,
    });
    assert.ok(!JSON.stringify(report).includes('lsv2_pt_planted'));
  } finally {
    await run.sink.close();
  }
});

test('LangSmith\'s OpenTelemetry mode stands the package down before it registers anything, and names its OTLP exporter', async () => {
  const env = { LANGSMITH_TRACING: 'true', LANGSMITH_OTEL_ENABLED: 'true', LANGSMITH_API_KEY: 'lsv2_pt_planted' };
  const run = await started({ langsmith: '0.11.8', '@anthropic-ai/sdk': '0.140.0' }, { env });
  try {
    assert.deepEqual(run.outcome, { state: 'pre-existing', detail: 'LANGSMITH_OTEL_ENABLED (set by the workload)' });
    assert.equal(run.runtime.calls.length, 0, 'no provider of ours, so LangSmith\'s can register');
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {
      'impact.setup.tool': 'langsmith',
      'impact.setup.tool.version': '0.11.8',
      'impact.setup.exporter': 'otlp-http',
      'impact.setup.alongside': false,
    });
    assert.ok(!JSON.stringify(report).includes('lsv2_pt_planted'));
  } finally {
    await run.sink.close();
  }
});

test('a workload with no tool has no tool attributes', async () => {
  const run = await started({ langsmith: '0.11.8', openai: '5.0.0' });
  try {
    assert.equal(run.outcome.state, 'active');
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {});
  } finally {
    await run.sink.close();
  }
});

test('LangSmith\'s tracing beside a tracer of no known tool names none: the unknown tracer owns the workload', async () => {
  const packages = { langsmith: '0.11.8', '@arizeai/openinference-instrumentation-openai': '2.3.0', openai: '5.0.0' };
  const run = await started(packages, { env: { LANGSMITH_TRACING: 'true' } });
  try {
    assert.equal(run.outcome.state, 'pre-existing');
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {});
  } finally {
    await run.sink.close();
  }
});

test('OTEL_ENABLED=true without the langsmith package stands the package down and names no tool', async () => {
  const run = await started({ openai: '5.0.0' }, { env: { OTEL_ENABLED: 'true' } });
  try {
    assert.deepEqual(run.outcome, { state: 'pre-existing', detail: 'OTEL_ENABLED (set by the workload)' });
    const [report] = await run.reports(1);
    assert.deepEqual(toolAttributes(report), {});
  } finally {
    await run.sink.close();
  }
});
