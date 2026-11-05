// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const qualification = require('../../src/impact/qualification');
const { tmpdir } = require('./helpers');

const TABLE = {
  schema: 'impact.nodejs.qualification.v1',
  cells: [
    { node: 22, tree: 'node22', module: 'esm', library: 'openai', major: 6, version: '6.1.0', traced: true },
    { node: 22, tree: 'node22', module: 'esm', library: 'openai', major: 5, version: '5.9.0', traced: false },
    { node: 22, tree: 'node22', module: 'esm', library: 'anthropic', major: 1, version: '1.4.0', traced: true },
    { node: 26, tree: 'node22', module: 'esm', library: 'anthropic', major: 0, version: '0.134.0', traced: true },
  ],
};

test('CommonJS needs no entry: it is the supported case', () => {
  assert.equal(qualification.proven(null, 26, 'cjs', 'openai', '5.0.0', {}, 'node22'), true);
  assert.equal(qualification.proven(TABLE, 24, 'cjs', 'anthropic', '1.0.0', {}, 'node22'), true);
});

test('an ES module is covered only by a passing cell for its Node, library and major version', () => {
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '6.3.1', {}, 'node22'), true);
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '5.1.0', {}, 'node22'), false, 'the cell failed');
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '7.0.0', {}, 'node22'), false, 'no cell');
  assert.equal(qualification.proven(TABLE, 24, 'esm', 'openai', '6.3.1', {}, 'node22'), false, 'another Node');
  assert.equal(qualification.proven(TABLE, 26, 'esm', 'anthropic', '0.140.0', {}, 'node22'), true);
});

test('without a table nothing about ES modules is proven', () => {
  assert.equal(qualification.proven(null, 22, 'esm', 'openai', '6.0.0', {}, 'node22'), false);
  assert.equal(qualification.hookWanted(null, 22, {}, 'node22'), false);
});

test('ES modules work in a Node where the matrix has a passing cell, and not elsewhere', () => {
  assert.equal(qualification.hookWanted(TABLE, 22, {}, 'node22'), true);
  assert.equal(qualification.hookWanted(TABLE, 26, {}, 'node22'), true, 'Anthropic\'s cell passes there');
  assert.equal(qualification.hookWanted(TABLE, 24, {}, 'node22'), false);
});

test('a cell is for the payload tree the workload ran with: a cell of another tree proves nothing', () => {
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '6.3.1', {}, 'another-tree'), false);
  assert.equal(qualification.hookWanted(TABLE, 22, {}, 'another-tree'), false);
});

test('IMPACT_NODEJS_ESM=on or off overrides the table', () => {
  assert.equal(qualification.proven(null, 24, 'esm', 'openai', '5.0.0', { IMPACT_NODEJS_ESM: 'on' }, 'node22'), true);
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '6.0.0', { IMPACT_NODEJS_ESM: 'OFF' }, 'node22'), false);
  assert.equal(qualification.proven(TABLE, 22, 'esm', 'openai', '6.0.0', { IMPACT_NODEJS_ESM: 'maybe' }, 'node22'), true);
  assert.equal(qualification.hookWanted(null, 24, { IMPACT_NODEJS_ESM: 'on' }, 'node22'), true);
  assert.equal(qualification.hookWanted(TABLE, 22, { IMPACT_NODEJS_ESM: 'off' }, 'node22'), false);
  assert.equal(qualification.proven(null, 24, 'cjs', 'openai', '5.0.0', { IMPACT_NODEJS_ESM: 'off' }, 'node22'), true, 'CommonJS is not affected');
});

test('the hook is registered unless it could break a library the application has', () => {
  const traced = { openai: { action: 'trace' }, '@anthropic-ai/sdk': { action: 'absent' } };
  assert.deepEqual(qualification.hook(TABLE, 22, {}, traced, 'node22'), { register: true, why: '' });
  assert.deepEqual(qualification.hook(TABLE, 22, {}, { openai: { action: 'absent' } }, 'node22'), { register: true, why: '' });
  const unproven = { openai: { action: 'skip', why: 'esm: not proven for Node 22 (4.104.0)' } };
  assert.deepEqual(qualification.hook(TABLE, 22, {}, unproven, 'node22'), { register: false, why: 'openai is not proven as an ES module here' });
  const switchedOff = { openai: { action: 'skip', disabled: true, why: 'switched off' } };
  assert.equal(qualification.hook(TABLE, 22, {}, switchedOff, 'node22').register, true, 'a library that is switched off is not patched');
  assert.deepEqual(qualification.hook(TABLE, 24, {}, traced, 'node22'), {
    register: false,
    why: 'no ES module cell passed for Node 24 with the node22 tree',
  });
});

test('a table is read from qualification.json beside the package, and a missing or broken one is no table', () => {
  const root = tmpdir();
  assert.equal(qualification.load(root), null);
  fs.writeFileSync(path.join(root, 'qualification.json'), '{not json');
  assert.equal(qualification.load(root), null);
  fs.writeFileSync(path.join(root, 'qualification.json'), '{"cells":"no"}');
  assert.equal(qualification.load(root), null);
  fs.writeFileSync(path.join(root, 'qualification.json'), JSON.stringify(TABLE));
  assert.deepEqual(qualification.load(root), TABLE);
});

test('major versions are read from the number before the first dot', () => {
  assert.equal(qualification.majorOf('7.50.0'), 7);
  assert.equal(qualification.majorOf('0.134.0'), 0);
  assert.equal(qualification.majorOf(null), null);
  assert.equal(qualification.majorOf('x'), null);
});
