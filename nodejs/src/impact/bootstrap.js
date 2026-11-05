// SPDX-License-Identifier: Apache-2.0
'use strict';

// Process start (called from autoinstrumentation.js): stand down beside another tracer, or arm the instrumentation.
//
// Whatever happens, the application starts: every failure is caught, reported in the `impact.setup` span, and the
// application's environment is left as it was found.

const path = require('path');
const trees = require('./trees');
const defaults = require('./defaults');
const detect = require('./detect');
const frameworks = require('./frameworks');
const plan = require('./plan');
const qualification = require('./qualification');
const setupReport = require('./setup-report');

// state: active | pre-existing | failed | disabled | skipped
function outcome(state, detail) {
  return { state, detail: detail || '' };
}

function payloadRoot() {
  return path.dirname(__dirname);
}

// A working directory that was deleted under the process is no reason to give up.
function workingDirectory() {
  try {
    return process.cwd();
  } catch (error) {
    return path.parse(process.execPath).root;
  }
}

function failed(error, env, root, applied, extra) {
  const detail = (String((error && error.name) || 'Error') + ': ' + String((error && error.message) || error))
    .split('\n')[0]
    .slice(0, setupReport.MAX_DETAIL);
  for (const name of Object.keys(applied)) {
    if (env[name] === applied[name]) delete env[name];
  }
  setupReport.report('failed', detail, env, root, extra);
  return outcome('failed', detail);
}

// The two lists are on every report; "nothing found", or "could not look", is an empty list, never a missing attribute.
const NO_FRAMEWORKS = { 'impact.setup.frameworks': [], 'impact.setup.untraced': [] };

function survey(packages, traced) {
  try {
    const found = frameworks.survey(packages, traced);
    return { 'impact.setup.frameworks': found.frameworks, 'impact.setup.untraced': found.untraced };
  } catch (error) {
    return Object.assign({}, NO_FRAMEWORKS);
  }
}

function start(options) {
  const settings = options || {};
  const env = settings.env || process.env;
  const root = settings.root || payloadRoot();
  const argv = settings.argv || process.argv;
  const execArgv = settings.execArgv || process.execArgv;
  const nodeVersion = settings.nodeVersion || process.versions.node;
  let applied = {};
  let found = Object.assign({}, NO_FRAMEWORKS);
  try {
    if (String(env.OTEL_SDK_DISABLED || '').trim().toLowerCase() === 'true') return outcome('disabled');
    if (detect.isPackageManager(argv[1])) return outcome('skipped', 'a package manager');

    const tree = trees.select(nodeVersion);
    if (!tree) {
      const detail = trees.unsupported(nodeVersion);
      setupReport.report('failed', detail, env, root, found);
      return outcome('failed', detail);
    }
    const nodeMajor = trees.major(nodeVersion);

    const packages = detect.scan({
      cwd: settings.cwd || workingDirectory(),
      entry: argv[1],
      env,
      wanted: detect.wantedNames((name) => frameworks.isCatalogued(name) || name === 'express' || name === 'koa'),
    });
    const moduleSystem = detect.moduleSystem(argv[1], execArgv, env.NODE_OPTIONS);
    const table = qualification.load(root);
    const decisions = plan.decide({ packages, moduleSystem, nodeMajor, table, env, tree: tree.name });
    found = survey(packages, plan.tracedSet(decisions));

    const findings = detect.findForeign({
      env,
      execArgv,
      nodeOptions: env.NODE_OPTIONS,
      ownRoot: root,
      packages,
      cache: settings.cache || require.cache,
      globalState: settings.globalState || { 'opentelemetry.js.api.1': globalThis[Symbol.for('opentelemetry.js.api.1')] },
    });
    if (findings.length) {
      const detail = detect.describe(findings);
      setupReport.report('pre-existing', detail, env, root, found);
      return outcome('pre-existing', detail);
    }

    applied = defaults.apply(env);
    const runtime = settings.runtime || require('./runtime');
    const armed = runtime.arm({ tree, env, root, packages, moduleSystem, nodeMajor, table, decisions });
    const extra = Object.assign(
      {
        'impact.setup.instrumentations': armed.armed.join(','),
        'impact.setup.node.tree': tree.name,
      },
      survey(packages, armed.traced)
    );
    const skipped = plan.skippedText(decisions, armed.skipped);
    if (skipped) extra['impact.setup.skipped'] = skipped;
    armed.suppress(() => setupReport.report('active', '', env, root, extra));
    return outcome('active', armed.armed.join(','));
  } catch (error) {
    return failed(error, env, root, applied, found);
  }
}

module.exports = { start, payloadRoot, outcome };
