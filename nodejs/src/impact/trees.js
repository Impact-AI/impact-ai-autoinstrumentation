// SPDX-License-Identifier: Apache-2.0
'use strict';

// The image carries one payload tree, and the process uses it when the Node it runs on is new enough:
//
//   node22   OpenTelemetry JavaScript 3.x (the release train of auto-instrumentations-node 0.82.0), Node 22.15 and later
//
// Older Node has no tree: the package reports why and does nothing else, and the application starts as it would without
// the kit. Node 20 reached its end of life on 2026-04-30, so there is no frozen OpenTelemetry 2.x tree for it. The
// descriptor says how the tree builds a tracer provider, so that the rest of the package does not care which line of the
// SDK it holds and a later line can sit beside this one (the batch processor is the package's own, see processor.js). The
// packages are required by absolute path from the tree, never from the application's node_modules.

const TREES = [
  {
    name: 'node22',
    min: [22, 15, 0],
    provider: { module: '@opentelemetry/sdk-trace', name: 'TracerProvider' },
  },
];

const FLOOR = '22.15';

function parse(version) {
  const parts = String(version).split('.');
  return [parseInt(parts[0], 10) || 0, parseInt(parts[1], 10) || 0, parseInt(parts[2], 10) || 0];
}

function atLeast(version, minimum) {
  for (let i = 0; i < 3; i += 1) {
    if (version[i] !== minimum[i]) return version[i] > minimum[i];
  }
  return true;
}

/** The tree for this version of Node, or null when no tree supports it. */
function select(nodeVersion) {
  const version = parse(nodeVersion);
  for (const tree of TREES) {
    if (atLeast(version, tree.min)) return tree;
  }
  return null;
}

/** What the setup report says of a Node below the floor: what Impact AI traces, and the version the workload runs. */
function unsupported(nodeVersion) {
  return 'Impact AI traces Node.js ' + FLOOR + ' and later; this workload runs Node.js ' + nodeVersion;
}

function major(nodeVersion) {
  return parse(nodeVersion)[0];
}

module.exports = { TREES, FLOOR, select, unsupported, major, parse };
