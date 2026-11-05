// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const defaults = require('../../src/impact/defaults');

test('the defaults are set when the workload set none of them', () => {
  const env = {};
  const changed = defaults.apply(env);
  assert.equal(env.OTEL_LOGS_EXPORTER, 'none');
  assert.equal(env.OTEL_METRICS_EXPORTER, 'none');
  assert.equal(env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE, 'content');
  assert.deepEqual(Object.keys(changed).sort(), Object.keys(env).sort());
});

test('a value the workload set is never replaced', () => {
  const env = { OTEL_LOGS_EXPORTER: 'otlp', ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE: 'metadata_only' };
  const changed = defaults.apply(env);
  assert.equal(env.OTEL_LOGS_EXPORTER, 'otlp');
  assert.equal(env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE, 'metadata_only');
  assert.ok(!('OTEL_LOGS_EXPORTER' in changed));
});

for (const mode of ['NO_CONTENT', 'no_content', 'false', 'EVENT_ONLY']) {
  test('the OpenTelemetry GenAI mode ' + mode + ' keeps content off Anthropic spans too', () => {
    const env = { OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: mode };
    assert.equal(defaults.contentOff(env), true);
    defaults.apply(env);
    assert.equal(env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE, 'metadata_only');
  });
}

for (const mode of ['SPAN_ONLY', 'span_and_event', 'true', undefined]) {
  test('the mode ' + mode + ' leaves content on', () => {
    const env = mode === undefined ? {} : { OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: mode };
    assert.equal(defaults.contentOff(env), false);
    defaults.apply(env);
    assert.equal(env.ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE, 'content');
  });
}

test('the disabled instrumentations are read as a list of names', () => {
  assert.deepEqual(defaults.disabledInstrumentations({ OTEL_NODE_DISABLED_INSTRUMENTATIONS: ' Express, openai ,' }), [
    'express',
    'openai',
  ]);
  assert.deepEqual(defaults.disabledInstrumentations({}), []);
});

for (const url of ['/healthz', '/health', '/health/', '/healthcheck?x=1', '/livez', '/liveness', '/readyz', '/ready', '/readiness', '/ping', '/_health', '/-/healthy', '/-/ready', '/metrics', '/metrics?format=text', '/api/v1/healthz']) {
  test('the probe ' + url + ' is not traced', () => assert.equal(defaults.probe(url), true));
}

for (const url of ['/', '/chat/openai', '/healthy-snacks', '/pingpong', '/metricsish', '/api/ready-made', undefined]) {
  test('the request ' + url + ' is traced', () => assert.equal(defaults.probe(url), false));
}
