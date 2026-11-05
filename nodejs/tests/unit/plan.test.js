// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const plan = require('../../src/impact/plan');

const TABLE = { cells: [{ node: 22, tree: 'node22', module: 'esm', library: 'openai', major: 6, version: '6.1.0', traced: true }] };

function decide(packages, extra) {
  return plan.decide({ packages, moduleSystem: 'cjs', nodeMajor: 22, table: TABLE, env: {}, tree: 'node22', ...extra });
}

test('libraries the application does not have are absent', () => {
  const decisions = decide({});
  assert.equal(decisions.openai.action, 'absent');
  assert.equal(decisions['@anthropic-ai/sdk'].action, 'absent');
  assert.equal(plan.tracedSet(decisions).size, 0);
});

test('openai 4 to 7 and Anthropic 0.134 and later are traced in CommonJS', () => {
  for (const version of ['4.104.0', '5.23.2', '6.9.1', '7.50.0']) {
    assert.equal(decide({ openai: version }).openai.action, 'trace', version);
  }
  for (const version of ['0.134.0', '0.137.0', '1.4.0']) {
    assert.equal(decide({ '@anthropic-ai/sdk': version })['@anthropic-ai/sdk'].action, 'trace', version);
  }
  assert.deepEqual([...plan.tracedSet(decide({ openai: '6.0.0', '@anthropic-ai/sdk': '1.0.0' }))].sort(), ['@anthropic-ai/sdk', 'openai']);
});

test('a version outside the range is skipped and says so', () => {
  const decisions = decide({ openai: '3.9.0', '@anthropic-ai/sdk': '0.133.0' });
  assert.deepEqual(decisions.openai, { action: 'skip', why: 'openai 3.9.0 installed, supported 4 to 7' });
  assert.equal(decisions['@anthropic-ai/sdk'].why, '@anthropic-ai/sdk 0.133.0 installed, supported 0.134.0 or later');
  assert.equal(plan.tracedSet(decisions).size, 0);
});

test('an ES module is traced only where the matrix proved it, and the reason is esm', () => {
  const proven = decide({ openai: '6.2.0' }, { moduleSystem: 'esm' });
  assert.equal(proven.openai.action, 'trace');
  const unproven = decide({ openai: '5.2.0' }, { moduleSystem: 'esm' });
  assert.equal(unproven.openai.action, 'skip');
  assert.match(unproven.openai.why, /^esm: not proven for Node 22 \(5\.2\.0\)$/);
  assert.equal(decide({ openai: '6.2.0' }, { moduleSystem: 'esm', nodeMajor: 20 }).openai.action, 'skip');
});

test('the workload can switch a library off, or everything', () => {
  const off = decide({ openai: '6.0.0', '@anthropic-ai/sdk': '1.0.0' }, { env: { OTEL_NODE_DISABLED_INSTRUMENTATIONS: 'openai' } });
  assert.equal(off.openai.action, 'skip');
  assert.equal(off.openai.disabled, true);
  assert.equal(off['@anthropic-ai/sdk'].action, 'trace');
  const all = decide({ openai: '6.0.0', '@anthropic-ai/sdk': '1.0.0' }, { env: { OTEL_NODE_DISABLED_INSTRUMENTATIONS: 'all' } });
  assert.equal(plan.tracedSet(all).size, 0);
  assert.equal(decide({ '@anthropic-ai/sdk': '1.0.0' }, { env: { OTEL_NODE_DISABLED_INSTRUMENTATIONS: 'anthropic' } })['@anthropic-ai/sdk'].disabled, true);
});

test('what was skipped is written as name: reason, in order, with the extras', () => {
  const decisions = decide({ openai: '3.9.0', '@anthropic-ai/sdk': '0.133.0' });
  assert.equal(
    plan.skippedText(decisions, { esm: 'the loader hook did not register: x' }),
    'anthropic-ai-sdk: @anthropic-ai/sdk 0.133.0 installed, supported 0.134.0 or later; esm: the loader hook did not register: x; openai: openai 3.9.0 installed, supported 4 to 7'
  );
  assert.equal(plan.skippedText(decide({ openai: '6.0.0' })), '');
});
