// SPDX-License-Identifier: Apache-2.0
'use strict';

// What the image's qualification proved. The matrix of Node 22, 24 and 26 by CommonJS and ES modules by the versions of
// OpenAI's and Anthropic's SDKs runs against the mock (nodejs/tests/image/test_nodejs_matrix.py) and writes
// qualification.json beside this package. CommonJS is the supported case and needs no entry. An ES module application is
// covered only where the matrix has a passing cell for its Node, the payload tree that Node uses (a cell says which one
// the workload reported), its module system and the library's major version; for everything else the library is reported
// untraced, with the reason `esm`. (A Node below 22.15 has no tree and no cell: the package does nothing there.)

const fs = require('fs');
const path = require('path');

// IMPACT_NODEJS_ESM=on asks for ES module support whatever the matrix proved (the matrix itself runs this way to find out
// what works); =off keeps the package away from ES modules even where it is proven.
const FORCE = 'IMPACT_NODEJS_ESM';

// The npm packages whose module the instrumentation patches. An ES module that imports one of them goes through the loader
// hook, and the matrix found that the hook can break such an application (openai 4's shims must be imported first, and the
// hook changes the order): the hook is registered only when every patched library the application has is proven.
const PATCHED_PACKAGES = ['openai'];

function load(root) {
  try {
    const table = JSON.parse(fs.readFileSync(path.join(root, 'qualification.json'), 'utf8'));
    return table && Array.isArray(table.cells) ? table : null;
  } catch (error) {
    return null;
  }
}

function majorOf(version) {
  const parsed = parseInt(String(version || '').split('.')[0], 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function forced(env) {
  const value = String((env && env[FORCE]) || '').trim().toLowerCase();
  return value === 'on' || value === 'off' ? value : null;
}

/** Whether the matrix proved the library at this version, in this Node major (with this tree) and module system. */
function proven(table, nodeMajor, moduleSystem, library, version, env, treeName) {
  if (moduleSystem === 'cjs') return true;
  const force = forced(env);
  if (force) return force === 'on';
  if (!table) return false;
  const major = majorOf(version);
  return table.cells.some(
    (cell) =>
      cell.node === nodeMajor &&
      cell.tree === treeName &&
      cell.module === moduleSystem &&
      cell.library === library &&
      cell.major === major &&
      cell.traced === true
  );
}

/** Whether ES modules work at all in this Node (and tree): the matrix has a passing cell for it. */
function hookWanted(table, nodeMajor, env, treeName) {
  const force = forced(env);
  if (force) return force === 'on';
  if (!table) return false;
  return table.cells.some(
    (cell) => cell.node === nodeMajor && cell.tree === treeName && cell.module === 'esm' && cell.traced === true
  );
}

/**
 * Whether to register the loader hook for this ES module application: it works in this Node, and no patched library the
 * application has is outside what the matrix proved (the hook would wrap its module and might break the application).
 * A library the workload switched off is not patched and does not count. decisions: plan.decide, by npm name.
 */
function hook(table, nodeMajor, env, decisions, treeName) {
  for (const name of PATCHED_PACKAGES) {
    const decision = decisions && decisions[name];
    if (decision && decision.action === 'skip' && !decision.disabled) {
      return { register: false, why: name + ' is not proven as an ES module here' };
    }
  }
  if (!hookWanted(table, nodeMajor, env, treeName)) {
    return { register: false, why: 'no ES module cell passed for Node ' + nodeMajor + ' with the ' + treeName + ' tree' };
  }
  return { register: true, why: '' };
}

module.exports = { PATCHED_PACKAGES, FORCE, load, proven, hookWanted, hook, majorOf, forced };
