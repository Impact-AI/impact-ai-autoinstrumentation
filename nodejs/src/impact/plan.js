// SPDX-License-Identifier: Apache-2.0
'use strict';

// What this image would trace in this process, library by library. The same answer is used when the package starts (to
// arm what can be armed) and when it stands down (so that a stood-down workload still says which of its AI libraries the
// image could have traced, as the Python package does).

const defaults = require('./defaults');
const frameworks = require('./frameworks');
const qualification = require('./qualification');

// The libraries the image traces, as npm names, and the short name the workload's disabled-instrumentations list uses.
const LIBRARIES = { openai: 'openai', '@anthropic-ai/sdk': 'anthropic' };

/**
 * For each traced library: absent, trace, or skip with the reason.
 *   skip: the installed version is outside the supported range, or the application is an ES module and the matrix did not
 *   prove this Node, module system and version (reason starting `esm:`), or the workload switched it off.
 */
function decide(input) {
  const { packages, moduleSystem, nodeMajor, table, env, tree } = input;
  const off = defaults.disabledInstrumentations(env);
  const result = {};
  for (const npmName of Object.keys(LIBRARIES)) {
    if (!(npmName in packages)) {
      result[npmName] = { action: 'absent' };
      continue;
    }
    const version = packages[npmName];
    const range = frameworks.outsideRange(npmName, version);
    if (range) {
      result[npmName] = { action: 'skip', why: range };
    } else if (off.indexOf('all') >= 0 || off.indexOf(LIBRARIES[npmName]) >= 0) {
      result[npmName] = {
        action: 'skip',
        disabled: true,
        why: LIBRARIES[npmName] + ' is switched off by OTEL_NODE_DISABLED_INSTRUMENTATIONS',
      };
    } else if (!qualification.proven(table, nodeMajor, moduleSystem, LIBRARIES[npmName] === 'openai' ? 'openai' : 'anthropic', version, env, tree)) {
      result[npmName] = { action: 'skip', why: 'esm: not proven for Node ' + nodeMajor + ' (' + version + ')' };
    } else {
      result[npmName] = { action: 'trace' };
    }
  }
  return result;
}

function tracedSet(decisions) {
  const traced = new Set();
  for (const npmName of Object.keys(decisions)) {
    if (decisions[npmName].action === 'trace') traced.add(npmName);
  }
  return traced;
}

/** `name: why; name: why`, the shape of impact.setup.skipped. */
function skippedText(decisions, extra) {
  const parts = [];
  for (const npmName of Object.keys(decisions)) {
    if (decisions[npmName].action === 'skip') parts.push(frameworks.label(npmName, '').trim() + ': ' + decisions[npmName].why);
  }
  for (const name of Object.keys(extra || {})) parts.push(name + ': ' + extra[name]);
  return parts.sort().join('; ');
}

module.exports = { LIBRARIES, decide, tracedSet, skippedText };
