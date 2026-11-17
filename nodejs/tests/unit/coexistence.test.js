// SPDX-License-Identifier: Apache-2.0
'use strict';

// The coexistence list: the package starts beside a tool only where a passing proof says so, else it stands down. The
// mechanism is proven with a generic fixture list, never with the shipped one: the shipped list holds a tool only with a
// passing proof, named by the test that showed it.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const coexistence = require('../../src/impact/coexistence');
const detect = require('../../src/impact/detect');
const { tmpdir } = require('./helpers');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const NODE = 22;
const CELL_KEYS = ['major', 'mode', 'node', 'proof', 'same', 'started', 'tool', 'traced', 'version'];

/** A cell of the generic fixture: the tool's series `major` proven on Node 22, unless an answer says otherwise. */
function cell(answers) {
  const major = (answers && answers.major) || '1';
  return Object.assign(
    {
      tool: 'openlit',
      mode: 'alongside',
      node: NODE,
      major,
      version: major + '.4.0',
      started: true,
      traced: true,
      same: true,
      proof: 'nodejs/tests/image/test_example.py',
    },
    answers || {}
  );
}

function fixture(...cells) {
  return { schema: coexistence.SCHEMA, cells };
}

function found(name, version) {
  const findings = [{ kind: 'distribution', name, version }];
  return { findings, tool: detect.toolOf(findings, { [name]: version }, {}) };
}

test('the shipped list is a coexistence list', () => {
  const table = coexistence.load();
  assert.equal(table.schema, 'impact.coexistence.v1');
  assert.deepEqual(JSON.parse(fs.readFileSync(coexistence.FILE, 'utf8')), table);
});

test('every shipped cell has its answers and names a proof that exists', () => {
  for (const entry of coexistence.load().cells) {
    assert.deepEqual(Object.keys(entry).sort(), CELL_KEYS, JSON.stringify(entry));
    assert.ok(coexistence.MODES.includes(entry.mode));
    assert.ok(fs.statSync(path.join(ROOT, entry.proof)).isFile(), entry.proof);
  }
});

test('an alongside entry is shipped only with a passing proof', () => {
  const cells = coexistence.load().cells;
  for (const tool of new Set(cells.filter((c) => c.mode === 'alongside').map((c) => c.tool))) {
    assert.ok(cells.some((c) => c.tool === tool && coexistence.passes(c)), tool);
  }
});

test('a tool a passing cell proves is run alongside', () => {
  const { findings, tool } = found('openlit', '1.34.0');
  assert.equal(coexistence.alongside(findings, tool, NODE, fixture(cell())), true);
});

for (const [why, table, version] of [
  ['an empty list', fixture(), '1.34.0'],
  ['another tool', fixture(cell({ tool: 'langfuse' })), '1.34.0'],
  ['another series', fixture(cell()), '2.0.1'],
  ['a series between two proven ones', fixture(cell(), cell({ major: '3' })), '2.0.1'],
  ['another Node', fixture(cell({ node: 24 })), '1.34.0'],
  ['it did not start', fixture(cell({ started: false })), '1.34.0'],
  ['its spans did not arrive', fixture(cell({ traced: false })), '1.34.0'],
  ["the tool's backend recorded something else", fixture(cell({ same: false })), '1.34.0'],
  ['no answer', fixture(cell({ same: null })), '1.34.0'],
  ['a copy cell, not an alongside one', fixture(cell({ mode: 'copy' })), '1.34.0'],
  ['no version', fixture(cell()), null],
]) {
  test('anything else keeps the stand-down: ' + why, () => {
    const findings = [{ kind: 'distribution', name: 'openlit', version }];
    const tool = detect.toolOf(findings, { openlit: version }, {});
    assert.equal(coexistence.alongside(findings, tool, NODE, table), false);
  });
}

test('a 0.x tool is proven by its minor', () => {
  let { findings, tool } = found('openlit', '0.22.3');
  assert.equal(coexistence.alongside(findings, tool, NODE, fixture(cell({ major: '0.22' }))), true);
  ({ findings, tool } = found('openlit', '0.23.0'));
  assert.equal(coexistence.alongside(findings, tool, NODE, fixture(cell({ major: '0.22' }))), false);
});

test('every finding must show the proven tool', () => {
  // The workload also exports through its own OpenTelemetry pipeline: another tool, so the package stands down.
  const findings = [
    { kind: 'distribution', name: 'openlit', version: '1.34.0' },
    { kind: 'environment', name: 'OTEL_EXPORTER_OTLP_ENDPOINT' },
  ];
  const tool = detect.toolOf(findings, { openlit: '1.34.0' }, {});
  assert.equal(tool.id, 'openlit');
  assert.equal(coexistence.alongside(findings, tool, NODE, fixture(cell())), false);
  // So does a provider registered before this package.
  const registered = [findings[0], { kind: 'runtime', name: 'an OpenTelemetry tracer provider (registered before this package)' }];
  assert.equal(coexistence.alongside(registered, tool, NODE, fixture(cell())), false);
});

test('no finding or no tool is never alongside', () => {
  assert.equal(coexistence.alongside([], { id: 'openlit', version: '1.34.0' }, NODE, fixture(cell())), false);
  const findings = [{ kind: 'distribution', name: '@arizeai/openinference-instrumentation-openai', version: '4.4.2' }];
  assert.equal(detect.toolOf(findings, {}, {}), null);
  assert.equal(coexistence.alongside(findings, null, NODE, fixture(cell())), false);
});

test('a list that cannot be read proves nothing', () => {
  const directory = tmpdir();
  for (const text of ['{', '[]', '{"schema": "impact.nodejs.qualification.v1", "cells": []}', '{"cells": {}}']) {
    const file = path.join(directory, 'coexistence.json');
    fs.writeFileSync(file, text);
    assert.throws(() => coexistence.load(file));
  }
  const { findings, tool } = found('openlit', '1.34.0');
  assert.equal(coexistence.alongside(findings, tool, NODE, { schema: 'x' }), false);
});

test('the report names the tool, its version, its exporter and whether the package started beside it', () => {
  assert.deepEqual(coexistence.attributes({ id: 'langsmith', version: '0.11.8', exporter: 'otlp-http' }, false), {
    'impact.setup.tool': 'langsmith',
    'impact.setup.tool.version': '0.11.8',
    'impact.setup.exporter': 'otlp-http',
    'impact.setup.alongside': false,
  });
  assert.deepEqual(coexistence.attributes({ id: 'langsmith', version: '0.11.8', exporter: null }, true), {
    'impact.setup.tool': 'langsmith',
    'impact.setup.tool.version': '0.11.8',
    'impact.setup.alongside': true,
  });
  assert.deepEqual(coexistence.attributes({ id: 'datadog', version: null, exporter: null }, false), {
    'impact.setup.tool': 'datadog',
    'impact.setup.alongside': false,
  });
  assert.deepEqual(coexistence.attributes(null, true), {});
});

test('series: the major version, or for 0.x the minor', () => {
  assert.deepEqual(['3.42.1', '0.22.3', '10.0.1', '1.0.0-rc.1', 'x', null].map(coexistence.series), ['3', '0.22', '10', '1', null, null]);
});
