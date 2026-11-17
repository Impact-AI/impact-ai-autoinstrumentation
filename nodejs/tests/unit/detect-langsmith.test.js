// SPDX-License-Identifier: Apache-2.0
'use strict';

// LangSmith: the package starts beside its default tracing and stands down in its OpenTelemetry mode. Its default tracing
// never touches the OpenTelemetry API, so it is no finding (nodejs/tests/image/test_nodejs_langsmith.py proves that
// LangSmith receives the same runs beside the package). In its OpenTelemetry mode the SDK registers the process's provider
// and exports OTLP itself. These cases follow how the langsmith SDK 0.11.8 resolves its mode (resolveTracingMode in
// langsmith/lib/utils/env.js).

const test = require('node:test');
const assert = require('node:assert/strict');
const detect = require('../../src/impact/detect');

const TRACING = { LANGSMITH_TRACING: 'true', LANGSMITH_API_KEY: 'lsv2_pt_planted', LANGSMITH_PROJECT: 'support' };

function find(env) {
  return detect.findForeign({
    env,
    execArgv: [],
    nodeOptions: '',
    ownRoot: '/otel-auto-instrumentation-nodejs',
    packages: { langsmith: '0.11.8', '@langchain/core': '1.3.5', '@opentelemetry/sdk-trace-base': '2.12.0' },
    cache: {},
    globalState: {},
  });
}

test('LangSmith\'s default tracing is no finding', () => {
  assert.deepEqual(find(TRACING), []);
  assert.deepEqual(find({ ...TRACING, LANGSMITH_TRACING_MODE: 'langsmith' }), []);
  assert.deepEqual(find({ ...TRACING, LANGSMITH_OTEL_ENABLED: 'false' }), []);
  // The SDK compares with "true" exactly.
  assert.deepEqual(find({ ...TRACING, LANGSMITH_OTEL_ENABLED: '1' }), []);
});

for (const [setting, value] of [
  ['LANGSMITH_OTEL_ENABLED', 'true'],
  ['LANGCHAIN_OTEL_ENABLED', 'true'],
  ['OTEL_ENABLED', 'true'],
  ['LANGSMITH_TRACING_MODE', 'otel'],
  ['LANGSMITH_TRACING_MODE', 'OTEL'],
  ['LANGCHAIN_TRACING_MODE', 'otel'],
]) {
  test(`${setting}=${value} is LangSmith's OpenTelemetry mode: the workload's own tracing`, () => {
    const findings = find({ ...TRACING, [setting]: value });
    assert.deepEqual(findings, [{ kind: 'environment', name: setting }]);
    // The detail names the setting, never its value.
    assert.equal(detect.describe(findings), `${setting} (set by the workload)`);
  });
}

test('the mode setting decides before the switches, and LANGSMITH_ before LANGCHAIN_', () => {
  assert.deepEqual(find({ ...TRACING, LANGSMITH_TRACING_MODE: 'langsmith', LANGSMITH_OTEL_ENABLED: 'true' }), []);
  assert.deepEqual(find({ ...TRACING, LANGSMITH_TRACING_MODE: 'langsmith', LANGCHAIN_TRACING_MODE: 'otel' }), []);
  assert.deepEqual(
    find({ ...TRACING, LANGSMITH_TRACING_MODE: 'otel', OTEL_ENABLED: 'false' }).map((f) => f.name),
    ['LANGSMITH_TRACING_MODE'],
  );
  // An empty mode is not set: the switches decide.
  assert.deepEqual(
    find({ ...TRACING, LANGSMITH_TRACING_MODE: '', LANGSMITH_OTEL_ENABLED: 'true' }).map((f) => f.name),
    ['LANGSMITH_OTEL_ENABLED'],
  );
});

// --- the tool the setup report names -----------------------------------------------------------------------------------

const PACKAGES = { langsmith: '0.11.8', '@langchain/core': '1.3.5', '@opentelemetry/sdk-trace-base': '2.12.0' };

function toolOf(env, packages) {
  return detect.toolOf(find(env), packages || PACKAGES, env);
}

for (const tracing of [{ LANGSMITH_TRACING: 'true' }, { LANGCHAIN_TRACING_V2: 'true' }, { LANGSMITH_TRACING_V2: 'true', LANGSMITH_TRACING: 'false' }]) {
  test('default tracing names LangSmith with its version and no exporter: ' + JSON.stringify(tracing), () => {
    assert.deepEqual(toolOf(tracing), { id: 'langsmith', version: '0.11.8', exporter: null });
  });
}

for (const env of [
  {}, // every LangChain app has the package: it alone is no tool
  { LANGSMITH_API_KEY: 'lsv2_pt_planted' },
  { LANGSMITH_TRACING: 'false' },
  { LANGSMITH_TRACING: 'True' }, // the SDK compares with "true" exactly
]) {
  test('without tracing switched on LangSmith is no tool: ' + JSON.stringify(env), () => {
    assert.equal(toolOf(env), null);
  });
}

test('tracing switched on without the package is no tool', () => {
  assert.equal(toolOf(TRACING, { openai: '7.50.0' }), null);
});

for (const [setting, value] of [['LANGSMITH_OTEL_ENABLED', 'true'], ['OTEL_ENABLED', 'true'], ['LANGSMITH_TRACING_MODE', 'otel']]) {
  test(`${setting}=${value}: LangSmith's OpenTelemetry mode names LangSmith exporting OTLP over HTTP`, () => {
    // Whatever OTLP protocol the environment names: LangSmith's exporter is its own, OTLP over HTTP.
    const env = { ...TRACING, OTEL_EXPORTER_OTLP_PROTOCOL: 'grpc', [setting]: value };
    assert.deepEqual(toolOf(env), { id: 'langsmith', version: '0.11.8', exporter: 'otlp-http' });
  });
}

for (const tracer of ['@arizeai/openinference-instrumentation-openai', '@azure/monitor-opentelemetry']) {
  test(`LangSmith's tracing beside ${tracer}, a tracer of no known tool, names no tool`, () => {
    // The unknown tracer made the package stand down; LangSmith's default tracing beside it does not make LangSmith the
    // owner, so the report names no tool and impact.ai reads "owned by another tracer".
    const packages = { ...PACKAGES, [tracer]: '1.0.0' };
    const findings = detect.findForeign({
      env: TRACING,
      execArgv: [],
      nodeOptions: '',
      ownRoot: '/otel-auto-instrumentation-nodejs',
      packages,
      cache: {},
      globalState: {},
    });
    assert.deepEqual(findings.map((f) => f.name), [tracer]);
    assert.equal(detect.toolOf(findings, packages, TRACING), null);
  });
}

for (const [setting, value] of [['OTEL_ENABLED', 'true'], ['LANGSMITH_OTEL_ENABLED', 'true'], ['LANGSMITH_TRACING_MODE', 'otel']]) {
  test(`${setting}=${value} without the langsmith package names no tool`, () => {
    // OTEL_ENABLED is a switch many apps use for their own OpenTelemetry setup: it is LangSmith's only beside its package.
    const packages = { openai: '6.9.0', '@opentelemetry/sdk-trace-base': '2.12.0' };
    const findings = [{ kind: 'environment', name: setting }];
    assert.equal(detect.toolOf(findings, packages, { [setting]: value }), null);
  });
}
