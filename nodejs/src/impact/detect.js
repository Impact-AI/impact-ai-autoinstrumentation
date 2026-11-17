// SPDX-License-Identifier: Apache-2.0
'use strict';

// Find out whether the workload already has a tracer of its own (stand down), and what the application is made of.
//
// This runs from the preload, before any application code. It therefore reads directory listings, the small package.json
// of the libraries it names, the command line and environment variable names: it never loads a candidate package.

const fs = require('fs');
const path = require('path');

const MAX_DETAIL = 200;
const KIT_ENDPOINT = 'IMPACT_OTLP_ENDPOINT';

// npm packages that start a tracer in the application's own code or configuration. Finding one in the application's
// environment means the application owns its tracing.
const TRACER_PACKAGES = new Set([
  'dd-trace',
  'langfuse',
  '@langfuse/otel',
  '@langfuse/tracing',
  'openlit',
  'newrelic',
  'elastic-apm-node',
  '@elastic/opentelemetry-node',
  '@splunk/otel',
  '@aws/aws-distro-opentelemetry-node-autoinstrumentation',
  '@azure/monitor-opentelemetry',
  '@honeycombio/opentelemetry-node',
  '@arizeai/openinference-vercel',
  '@arizeai/phoenix-otel',
  '@pydantic/logfire-node',
  // Impact AI's own SDK: an application that uses it sends its traces itself, so the preload does not trace the same
  // calls a second time.
  'impact-ai',
  '@impact-ai/sdk',
]);
// Families: Traceloop's SDK and OpenLLMetry's per-provider packages, and OpenInference's per-library instrumentations.
const TRACER_PREFIXES = ['@traceloop/', '@arizeai/openinference-instrumentation-'];
// Started with --require or --import, these packages are tracers too, although having them installed proves nothing.
const LAUNCHER_PACKAGES = new Set(['@opentelemetry/auto-instrumentations-node', '@opentelemetry/sdk-node']);
// The OpenTelemetry SDK of the application: with exporter settings of its own, a pipeline the workload runs itself.
const OTEL_SDK_PACKAGES = [
  '@opentelemetry/sdk-node',
  '@opentelemetry/sdk-trace-node',
  '@opentelemetry/sdk-trace-base',
  '@opentelemetry/sdk-trace',
];
// Scopes whose directories are listed (the others are never opened).
const SCOPES = new Set([
  '@traceloop',
  '@langfuse',
  '@elastic',
  '@splunk',
  '@aws',
  '@azure',
  '@honeycombio',
  '@arizeai',
  '@opentelemetry',
  '@anthropic-ai',
  '@openai',
  '@langchain',
  '@google',
  '@mistralai',
  '@modelcontextprotocol',
  '@mastra',
  '@ai-sdk',
  '@azure-rest',
  '@huggingface',
  '@aws-sdk',
  '@pydantic',
  '@impact-ai',
]);

// The exporter settings a workload sets when it runs its own OpenTelemetry pipeline. OTEL_EXPORTER_OTLP_PROTOCOL and
// OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp are set by the Operator itself and are not signals.
const EXPORTER_SETTINGS = [
  'OTEL_EXPORTER_OTLP_ENDPOINT',
  'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT',
  'OTEL_EXPORTER_OTLP_HEADERS',
  'OTEL_EXPORTER_OTLP_TRACES_HEADERS',
];
const EXPORTER_PREFIXES = ['OTEL_EXPORTER_ZIPKIN_', 'OTEL_EXPORTER_JAEGER_'];

const RANK = { launcher: 0, runtime: 1, distribution: 2, environment: 3 };

// LangSmith: its default tracing uses callbacks and wrappers and leaves the OpenTelemetry provider alone, so the package
// starts beside it. Its OpenTelemetry mode makes the SDK register the process's provider (initializeOTEL()) and export
// OTLP itself, and then the workload owns its tracing. The SDK reads TRACING_MODE (langsmith or otel) under LANGSMITH_,
// then LANGCHAIN_; when it is not set, OTEL_ENABLED=true, bare or under those prefixes, switches the mode on. The
// values are read here and never leave the process: a finding names the setting.
const LANGSMITH_PREFIXES = ['LANGSMITH_', 'LANGCHAIN_'];

function langsmithSetting(env, name) {
  for (const prefix of LANGSMITH_PREFIXES) {
    if (env[prefix + name]) return { name: prefix + name, value: String(env[prefix + name]) };
  }
  return null;
}

function langsmithOtelMode(env) {
  const mode = langsmithSetting(env, 'TRACING_MODE');
  if (mode) return mode.value.toLowerCase() === 'otel' ? [{ kind: 'environment', name: mode.name }] : [];
  if (env.OTEL_ENABLED === 'true') return [{ kind: 'environment', name: 'OTEL_ENABLED' }];
  const legacy = langsmithSetting(env, 'OTEL_ENABLED');
  return legacy && legacy.value === 'true' ? [{ kind: 'environment', name: legacy.name }] : [];
}

// The tool a finding shows, by the source ids impact.ai's service knows, and the packages whose version is the tool's
// (the first one installed). A finding of no tool here (an OpenLLMetry or OpenInference instrumentation on its own, which
// several tools start) names none: impact.ai reads the workload as owned by another tracer.
const TOOLS = {
  'dd-trace': 'datadog',
  '@traceloop/node-server-sdk': 'traceloop',
  langfuse: 'langfuse',
  '@langfuse/otel': 'langfuse',
  '@langfuse/tracing': 'langfuse',
  openlit: 'openlit',
  newrelic: 'newrelic',
  '@elastic/opentelemetry-node': 'elastic-edot',
  '@splunk/otel': 'splunk-otel',
  '@honeycombio/opentelemetry-node': 'honeycomb',
  '@arizeai/phoenix-otel': 'phoenix',
  '@pydantic/logfire-node': 'logfire',
  '@opentelemetry/auto-instrumentations-node': 'opentelemetry',
  '@opentelemetry/sdk-node': 'opentelemetry',
};
const TOOL_PACKAGES = {
  datadog: ['dd-trace'],
  traceloop: ['@traceloop/node-server-sdk'],
  langfuse: ['langfuse', '@langfuse/otel', '@langfuse/tracing'],
  openlit: ['openlit'],
  newrelic: ['newrelic'],
  'elastic-edot': ['@elastic/opentelemetry-node'],
  'splunk-otel': ['@splunk/otel'],
  honeycomb: ['@honeycombio/opentelemetry-node'],
  phoenix: ['@arizeai/phoenix-otel'],
  logfire: ['@pydantic/logfire-node'],
  opentelemetry: ['@opentelemetry/sdk-node', '@opentelemetry/sdk-trace-node', '@opentelemetry/sdk-trace-base'],
  langsmith: ['langsmith'],
};
// The exporter kind of an OpenTelemetry SDK's traces, from OTEL_TRACES_EXPORTER and the OTLP protocol it reads. Only these
// words leave the process, never a setting's value.
const TRACES_EXPORTERS = ['zipkin', 'console'];
const OTLP_PROTOCOLS = { grpc: 'otlp-grpc', 'http/protobuf': 'otlp-http', 'http/json': 'otlp-http' };
const LANGSMITH_EXPORTER = 'otlp-http'; // LangSmith's OpenTelemetry mode exports OTLP over HTTP, whatever else is set
const MAX_TOOL_TEXT = 64;
const RUNTIME_PROVIDER = 'an OpenTelemetry tracer provider (registered before this package)';
const LOADED_BEFORE = ' (loaded before this package)';

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

/** LangSmith's tracing is switched on in the environment, as its SDK reads it: TRACING_V2 or TRACING equal to "true". */
function langsmithTracing(env) {
  return ['TRACING_V2', 'TRACING'].some((name) => {
    const setting = langsmithSetting(env, name);
    return setting !== null && setting.value === 'true';
  });
}

/** The tool id a finding shows, or null. */
function findingTool(finding) {
  if (finding.kind === 'environment') {
    if (finding.name === 'OTEL_ENABLED' || LANGSMITH_PREFIXES.some((prefix) => finding.name.indexOf(prefix) === 0)) {
      return 'langsmith';
    }
    return finding.name.indexOf('OTEL_') === 0 ? 'opentelemetry' : null;
  }
  let name = finding.name;
  if (finding.kind === 'runtime') {
    if (name === RUNTIME_PROVIDER) return 'opentelemetry';
    if (name.slice(-LOADED_BEFORE.length) === LOADED_BEFORE) name = name.slice(0, -LOADED_BEFORE.length);
  } else if (finding.kind === 'launcher') {
    name = specifierPackage(name);
  }
  return name && own(TOOLS, name) ? TOOLS[name] : null;
}

/** A name or version impact.ai keeps (a letter or digit first, then letters, digits and ._+!-, at most 64), else null. */
function printableWord(text) {
  if (typeof text !== 'string' || text.length > MAX_TOOL_TEXT) return null;
  return /^[A-Za-z0-9][A-Za-z0-9._+!-]*$/.test(text) ? text : null;
}

/** The kind of exporter an OpenTelemetry SDK configured by the environment sends its traces with, or null. */
function otelExporter(env) {
  const names = String(env.OTEL_TRACES_EXPORTER || '')
    .split(',')
    .map((name) => name.trim().toLowerCase())
    .filter(Boolean);
  if (names.length && TRACES_EXPORTERS.indexOf(names[0]) >= 0) return names[0];
  if (names.length && names[0] !== 'otlp') return null;
  if (Object.keys(env).some((name) => name.indexOf('OTEL_EXPORTER_ZIPKIN_') === 0 && env[name])) return 'zipkin';
  const protocol = String(env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL || env.OTEL_EXPORTER_OTLP_PROTOCOL || '')
    .trim()
    .toLowerCase();
  return own(OTLP_PROTOCOLS, protocol) ? OTLP_PROTOCOLS[protocol] : null;
}

function byRank(a, b) {
  return RANK[a.kind] - RANK[b.kind] || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
}

/**
 * The tool the setup report names, { id, version, exporter }: that of the first finding (in the detail's order) which
 * shows one (a LangSmith setting, OTEL_ENABLED among them, only when the langsmith package is installed), else, when
 * there is no finding at all, LangSmith when its default tracing is on (it leaves the OpenTelemetry API alone, so it is
 * no finding), else null. A tracer of no known tool is named only in the detail: LangSmith's tracing beside it does not
 * make LangSmith its owner. Never a setting's value.
 */
function toolOf(findings, packages, env) {
  const installed = packages || {};
  const settings = env || {};
  const langsmith = own(installed, 'langsmith');
  const all = findings || [];
  let id = null;
  for (const finding of all.slice().sort(byRank)) {
    id = findingTool(finding);
    if (id === 'langsmith' && !langsmith) id = null;
    if (id) break;
  }
  if (!id) {
    if (all.length || !langsmith || !langsmithTracing(settings)) return null;
    return { id: 'langsmith', version: printableWord(installed.langsmith), exporter: null };
  }
  const owner = (TOOL_PACKAGES[id] || []).find((name) => own(installed, name));
  let exporter = null;
  if (id === 'langsmith') exporter = LANGSMITH_EXPORTER;
  else if (id === 'opentelemetry') exporter = otelExporter(settings);
  return { id, version: owner ? printableWord(installed[owner]) : null, exporter };
}

function isTracerPackage(name) {
  if (TRACER_PACKAGES.has(name)) return true;
  for (const prefix of TRACER_PREFIXES) {
    if (name.indexOf(prefix) === 0) return true;
  }
  return false;
}

function describeFinding(finding) {
  if (finding.kind === 'launcher') return finding.name + ' (preload)';
  if (finding.kind === 'environment') return finding.name + ' (set by the workload)';
  if (finding.kind === 'runtime') return finding.name;
  return finding.version ? finding.name + ' ' + finding.version : finding.name;
}

function describe(findings) {
  const text = findings.slice().sort(byRank).map(describeFinding).join(', ');
  return text.length <= MAX_DETAIL ? text : text.slice(0, MAX_DETAIL - 3) + '...';
}

function realpath(file) {
  try {
    return fs.realpathSync(file);
  } catch (error) {
    return path.resolve(file);
  }
}

function ancestors(start) {
  const out = [];
  let current = path.resolve(start);
  for (;;) {
    out.push(current);
    const parent = path.dirname(current);
    if (parent === current) return out;
    current = parent;
  }
}

/** The directories whose node_modules the application resolves from: the working directory and the entry's, upwards. */
function searchDirectories(cwd, entry, env) {
  const seen = new Set();
  const out = [];
  const add = (directory) => {
    const nodeModules = path.join(directory, 'node_modules');
    if (!seen.has(nodeModules)) {
      seen.add(nodeModules);
      out.push(nodeModules);
    }
  };
  if (entry) ancestors(path.dirname(realpath(entry))).forEach(add);
  ancestors(cwd).forEach(add);
  for (const extra of String(env.NODE_PATH || '').split(path.delimiter)) {
    if (extra && !seen.has(extra)) {
      seen.add(extra);
      out.push(extra);
    }
  }
  return out;
}

function listing(directory) {
  try {
    return fs.readdirSync(directory);
  } catch (error) {
    return [];
  }
}

function versionOf(directory) {
  try {
    const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
    return typeof manifest.version === 'string' ? manifest.version : null;
  } catch (error) {
    return null;
  }
}

/**
 * The packages of the application's own environment that matter here, by directory listing: {name: version|null}.
 *
 * The first occurrence of a name wins, as it does for resolution. `wanted(name)` says which names are worth reading.
 * Our own payload is never part of it: it is not on the application's module path.
 */
function scan(options) {
  const settings = options || {};
  const wanted = settings.wanted || (() => false);
  const found = {};
  const directories = searchDirectories(settings.cwd || process.cwd(), settings.entry || null, settings.env || process.env);
  for (const nodeModules of directories) {
    for (const entry of listing(nodeModules)) {
      if (entry.charAt(0) === '.') continue;
      if (entry.charAt(0) === '@') {
        if (!SCOPES.has(entry)) continue;
        for (const member of listing(path.join(nodeModules, entry))) {
          const name = entry + '/' + member;
          if (!(name in found) && wanted(name)) found[name] = versionOf(path.join(nodeModules, entry, member));
        }
      } else if (!(entry in found) && wanted(entry)) {
        found[entry] = versionOf(path.join(nodeModules, entry));
      }
    }
  }
  return found;
}

/** The package a --require or --import specifier names: `dd-trace/init` and `/x/node_modules/dd-trace/init.js` are dd-trace. */
function specifierPackage(specifier) {
  const text = String(specifier).replace(/^file:\/\//, '');
  const marker = '/node_modules/';
  const at = text.lastIndexOf(marker);
  const rest = at >= 0 ? text.slice(at + marker.length) : text;
  if (at < 0 && (rest.charAt(0) === '.' || rest.charAt(0) === '/' || /^[A-Za-z]:[\\/]/.test(rest))) return null;
  const parts = rest.split('/');
  if (rest.charAt(0) === '@') return parts.length >= 2 ? parts[0] + '/' + parts[1] : null;
  return parts[0] || null;
}

const PRELOAD_FLAGS = ['--require', '-r', '--import', '--loader', '--experimental-loader'];

/** Every module specifier handed to --require, --import and --loader, in NODE_OPTIONS and on the command line. */
function preloads(execArgv, nodeOptions) {
  const tokens = String(nodeOptions || '').split(/\s+/).filter(Boolean).concat(execArgv || []);
  const out = [];
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    const equals = token.indexOf('=');
    const flag = equals > 0 ? token.slice(0, equals) : token;
    if (PRELOAD_FLAGS.indexOf(flag) < 0) continue;
    if (equals > 0) out.push(token.slice(equals + 1));
    else if (i + 1 < tokens.length) out.push(tokens[(i += 1)]);
  }
  return out.map((value) => value.replace(/^["']|["']$/g, ''));
}

function launcherFindings(execArgv, nodeOptions, ownRoot) {
  const found = {};
  for (const specifier of preloads(execArgv, nodeOptions)) {
    if (ownRoot && specifier.indexOf(ownRoot) >= 0) continue;
    const name = specifierPackage(specifier);
    if (name && (isTracerPackage(name) || LAUNCHER_PACKAGES.has(name))) {
      const at = specifier.lastIndexOf('/node_modules/');
      found[name] = at >= 0 ? specifier.slice(at + '/node_modules/'.length) : specifier;
    }
  }
  return Object.keys(found).map((name) => ({ kind: 'launcher', name: found[name] }));
}

/** Evidence from the running process: a tracer package an earlier preload already loaded, or a provider already registered. */
function runtimeFindings(cache, globalState) {
  const found = [];
  const marker = '/node_modules/';
  const seen = new Set();
  for (const file of Object.keys(cache || {})) {
    const at = file.lastIndexOf(marker);
    if (at < 0) continue;
    const name = specifierPackage(file.slice(at + marker.length));
    if (name && isTracerPackage(name) && !seen.has(name)) {
      seen.add(name);
      found.push({ kind: 'runtime', name: name + LOADED_BEFORE });
    }
  }
  const api = globalState && globalState['opentelemetry.js.api.1'];
  if (api && api.trace) {
    found.push({ kind: 'runtime', name: RUNTIME_PROVIDER });
  }
  return found;
}

function environmentFindings(env, hasOtelSdk) {
  const found = [];
  const tracesExporter = String(env.OTEL_TRACES_EXPORTER || '').trim().toLowerCase();
  if (tracesExporter && tracesExporter !== 'otlp') found.push({ kind: 'environment', name: 'OTEL_TRACES_EXPORTER' });
  // Inside the cluster kit our own endpoint is IMPACT_OTLP_ENDPOINT, so any OTEL_EXPORTER_* setting is the workload's.
  // It only means a pipeline of its own when the workload also ships the OpenTelemetry SDK.
  if (env[KIT_ENDPOINT] && hasOtelSdk) {
    const names = EXPORTER_SETTINGS.filter((name) => env[name]);
    for (const name of Object.keys(env).sort()) {
      if (EXPORTER_PREFIXES.some((prefix) => name.indexOf(prefix) === 0) && env[name]) names.push(name);
    }
    names.forEach((name) => found.push({ kind: 'environment', name }));
  }
  return found;
}

/** Everything that shows the workload runs a tracer of its own; empty when it is safe to start. */
function findForeign(input) {
  const found = launcherFindings(input.execArgv, input.nodeOptions, input.ownRoot);
  found.push(...runtimeFindings(input.cache, input.globalState));
  const packages = input.packages || {};
  for (const name of Object.keys(packages)) {
    if (isTracerPackage(name)) found.push({ kind: 'distribution', name, version: packages[name] });
  }
  const hasOtelSdk = OTEL_SDK_PACKAGES.some((name) => name in packages);
  found.push(...environmentFindings(input.env || {}, hasOtelSdk));
  found.push(...langsmithOtelMode(input.env || {}));
  return found;
}

/** The names `scan` reads: tracers, the OpenTelemetry SDK, LangSmith, and what `frameworks.js` catalogues (passed in). */
function wantedNames(catalogued) {
  return (name) =>
    isTracerPackage(name) || OTEL_SDK_PACKAGES.indexOf(name) >= 0 || name === 'langsmith' || catalogued(name);
}

function entryFile(argv1) {
  if (!argv1) return null;
  for (const candidate of [argv1, argv1 + '.js', argv1 + '.cjs', argv1 + '.mjs', path.join(argv1, 'index.js')]) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch (error) {
      // not this one
    }
  }
  return null;
}

function nearestPackageType(directory) {
  for (const dir of ancestors(directory)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
      return typeof manifest.type === 'string' ? manifest.type : null;
    } catch (error) {
      // no package.json here, or not readable
    }
  }
  return null;
}

const ESM_SYNTAX = /^\s*(import\s+[\w{*"']|export\s+(default\b|const\b|let\b|var\b|function\b|class\b|async\b|\{|\*))/m;

function looksLikeEsm(file) {
  try {
    const descriptor = fs.openSync(file, 'r');
    try {
      const buffer = Buffer.alloc(32768);
      const length = fs.readSync(descriptor, buffer, 0, buffer.length, 0);
      return ESM_SYNTAX.test(buffer.toString('utf8', 0, length));
    } finally {
      fs.closeSync(descriptor);
    }
  } catch (error) {
    return false;
  }
}

/**
 * Whether the application is an ES module or CommonJS, as far as the entry file tells before it runs: its extension, the
 * "type" of its package, a default-type flag; a file without any of them that has module syntax is ESM too (Node 22.7 and
 * later run it so). Libraries a CommonJS application loads with import() are not seen.
 */
function moduleSystem(argv1, execArgv, nodeOptions) {
  const flags = String(nodeOptions || '').split(/\s+/).concat(execArgv || []);
  if (flags.indexOf('--experimental-default-type=module') >= 0) return 'esm';
  const file = entryFile(argv1);
  if (!file) return 'cjs';
  const extension = path.extname(file).toLowerCase();
  if (extension === '.mjs') return 'esm';
  if (extension === '.cjs') return 'cjs';
  const type = nearestPackageType(path.dirname(realpath(file)));
  if (type === 'module') return 'esm';
  if (type === 'commonjs') return 'cjs';
  return looksLikeEsm(file) ? 'esm' : 'cjs';
}

// Processes that only start the application: a package manager keeps its own Node process alive for as long as the
// application runs, and loading the instrumentation there would cost memory for nothing.
const LAUNCHER_PROGRAMS = ['npm-cli.js', 'npx-cli.js', 'yarn.js', 'yarn.cjs', 'pnpm.cjs', 'pnpm.js', 'corepack.js'];

function isPackageManager(argv1) {
  if (!argv1) return false;
  const base = path.basename(String(argv1));
  return LAUNCHER_PROGRAMS.indexOf(base) >= 0 || /[\\/]corepack[\\/]/.test(String(argv1));
}

module.exports = {
  MAX_DETAIL,
  TRACER_PACKAGES,
  OTEL_SDK_PACKAGES,
  isTracerPackage,
  describe,
  scan,
  wantedNames,
  specifierPackage,
  preloads,
  findForeign,
  findingTool,
  toolOf,
  TOOLS,
  TOOL_PACKAGES,
  TRACES_EXPORTERS,
  OTLP_PROTOCOLS,
  LANGSMITH_EXPORTER,
  moduleSystem,
  isPackageManager,
  ancestors,
};
