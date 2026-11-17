// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const frameworks = require('../../src/impact/frameworks');

// The impact.ai service's grammar for an entry: a name, and a version after one space.
const ENTRY = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}( [A-Za-z0-9][A-Za-z0-9._+!-]{0,63})?$/;

test('every catalogued library has a label the impact.ai service accepts', () => {
  for (const name of Object.keys(frameworks.CATALOG)) {
    assert.match(frameworks.label(name, '1.2.3'), ENTRY, name);
    assert.match(frameworks.label(name, null), ENTRY, name);
  }
});

test('scoped names lose the @ and the slash', () => {
  assert.equal(frameworks.label('@anthropic-ai/sdk', '0.134.0'), 'anthropic-ai-sdk 0.134.0');
  assert.equal(frameworks.label('@openai/agents', '0.5.1'), 'openai-agents 0.5.1');
  assert.equal(frameworks.label('openai', '7.50.0'), 'openai 7.50.0');
  assert.equal(frameworks.label('ai', ''), 'ai');
});

test('a name or a version is cut at 64 characters and a version has no whitespace', () => {
  const entry = frameworks.label('n'.repeat(100), '1.0 .0\n' + 'x'.repeat(100));
  const [name, version] = entry.split(' ');
  assert.equal(name.length, 64);
  assert.equal(version.length, 64);
  assert.ok(version.startsWith('1.0.0xxx'));
});

test('agent frameworks come before provider SDKs, each alphabetically, and langchain-core is said once', () => {
  const found = frameworks.survey(
    { openai: '7.0.0', '@langchain/langgraph': '1.0.0', '@langchain/core': '1.0.0', '@openai/agents': '0.5.0', '@anthropic-ai/sdk': '0.140.0', unrelated: '1.0.0' },
    new Set(['openai', '@anthropic-ai/sdk'])
  );
  assert.deepEqual(found.frameworks, ['langchain-langgraph 1.0.0', 'openai-agents 0.5.0', 'anthropic-ai-sdk 0.140.0', 'openai 7.0.0']);
  assert.deepEqual(found.untraced, ['langchain-langgraph 1.0.0', 'openai-agents 0.5.0']);
});

test('langchain-core alone is listed, as an agent framework', () => {
  const found = frameworks.survey({ '@langchain/core': '0.3.0' }, new Set());
  assert.deepEqual(found.frameworks, ['langchain-core 0.3.0']);
  assert.deepEqual(found.untraced, ['langchain-core 0.3.0']);
});

test('nothing found is two empty lists', () => {
  assert.deepEqual(frameworks.survey({}, new Set()), { frameworks: [], untraced: [] });
  assert.deepEqual(frameworks.survey(undefined, undefined), { frameworks: [], untraced: [] });
});

test('a list holds at most 20 entries', () => {
  const packages = {};
  for (const name of Object.keys(frameworks.CATALOG)) packages[name] = '1.0.0';
  const found = frameworks.survey(packages, new Set());
  assert.ok(Object.keys(packages).length > 20);
  assert.equal(found.frameworks.length, 20);
  assert.equal(found.untraced.length, 20);
});

for (const [name, version, outside] of [
  ['openai', '3.9.0', true],
  ['openai', '4.0.0', false],
  ['openai', '5.23.2', false],
  ['openai', '7.50.0', false],
  ['openai', '8.0.0', true],
  ['openai', '8.1.0-beta.1', true],
  ['@anthropic-ai/sdk', '0.133.9', true],
  ['@anthropic-ai/sdk', '0.134.0', false],
  ['@anthropic-ai/sdk', '1.4.0', false],
  ['@anthropic-ai/sdk', null, false],
  ['express', '5.0.0', false],
]) {
  test(name + ' ' + version + (outside ? ' is outside' : ' is inside') + ' the range this image traces', () => {
    const why = frameworks.outsideRange(name, version);
    assert.equal(Boolean(why), outside, why);
    if (outside) assert.match(why, new RegExp(name.replace(/[/@]/g, '.') + ' ' + String(version).replace(/\./g, '\\.') + ' installed, supported '));
  });
}
