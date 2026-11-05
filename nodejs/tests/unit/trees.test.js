// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const trees = require('../../src/impact/trees');

const CASES = [
  ['16.20.2', null],
  ['18.20.4', null],
  ['20.6.0', null],
  ['20.20.2', null],
  ['22.0.0', null],
  ['22.14.0', null],
  ['22.14.99', null],
  ['22.15.0', 'node22'],
  ['22.23.4', 'node22'],
  ['24.18.0', 'node22'],
  ['26.10.0', 'node22'],
];

for (const [version, expected] of CASES) {
  test('Node ' + version + ' gets ' + (expected || 'no tree'), () => {
    const tree = trees.select(version);
    assert.equal(tree ? tree.name : null, expected);
  });
}

test('a Node that is too old is told what Impact AI traces and what the workload runs, in one plain sentence', () => {
  const said = (version) => 'Impact AI traces Node.js 22.15 and later; this workload runs Node.js ' + version;
  assert.equal(
    trees.unsupported('18.20.4'),
    'Impact AI traces Node.js 22.15 and later; this workload runs Node.js 18.20.4'
  );
  assert.equal(trees.unsupported('20.20.2'), said('20.20.2'));
  assert.equal(trees.unsupported('22.14.0'), said('22.14.0'));
});

test('the floor the message names is the floor of the tree', () => {
  assert.deepEqual(
    trees.TREES.map((tree) => tree.min.slice(0, 2).join('.')),
    [trees.FLOOR]
  );
});

test('a tree says how its SDK builds a provider', () => {
  assert.deepEqual(trees.TREES.map((tree) => [tree.name, tree.provider.name]), [['node22', 'TracerProvider']]);
});

test('nightlies and release candidates are read by their numbers', () => {
  assert.equal(trees.select('22.15.0-nightly20260101').name, 'node22');
  assert.equal(trees.major('24.0.0-rc.1'), 24);
});
