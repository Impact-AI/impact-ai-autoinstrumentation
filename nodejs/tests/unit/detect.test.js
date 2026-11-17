// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const detect = require('../../src/impact/detect');
const { tmpdir, fakePackage } = require('./helpers');

const everything = () => true;

function app(files) {
  const root = tmpdir();
  for (const [name, content] of Object.entries(files || {})) {
    fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
    fs.writeFileSync(path.join(root, name), content);
  }
  return root;
}

function find(overrides) {
  return detect.findForeign(Object.assign({ env: {}, execArgv: [], nodeOptions: '', ownRoot: '/otel-auto-instrumentation-nodejs', packages: {}, cache: {}, globalState: {} }, overrides));
}

test('the scan lists the packages of the working directory and of every directory above it', () => {
  const root = app();
  fakePackage(root, 'openai', '5.1.0');
  fakePackage(root, '@anthropic-ai/sdk', '0.134.0');
  const nested = path.join(root, 'services', 'chat');
  fs.mkdirSync(nested, { recursive: true });
  fakePackage(nested, 'express', '5.0.1');
  const found = detect.scan({ cwd: nested, entry: null, env: {}, wanted: everything });
  assert.equal(found.openai, '5.1.0');
  assert.equal(found['@anthropic-ai/sdk'], '0.134.0');
  assert.equal(found.express, '5.0.1');
});

test('the nearest copy of a package wins, as it does when the application resolves it', () => {
  const root = app();
  fakePackage(root, 'openai', '4.0.0');
  const nested = path.join(root, 'packages', 'bot');
  fs.mkdirSync(nested, { recursive: true });
  fakePackage(nested, 'openai', '6.2.0');
  assert.equal(detect.scan({ cwd: nested, entry: null, env: {}, wanted: everything }).openai, '6.2.0');
});

test('the scan follows the entry file as well as the working directory', () => {
  const root = app({ 'dist/server.js': '' });
  fakePackage(root, 'openai', '7.0.0');
  const elsewhere = tmpdir();
  const found = detect.scan({ cwd: elsewhere, entry: path.join(root, 'dist', 'server.js'), env: {}, wanted: everything });
  assert.equal(found.openai, '7.0.0');
});

test('only the names asked for are read, and scopes that are not of interest are never opened', () => {
  const root = app();
  fakePackage(root, 'left-pad', '1.0.0');
  fakePackage(root, '@unrelated/thing', '1.0.0');
  fakePackage(root, '@anthropic-ai/sdk', '0.140.0');
  const found = detect.scan({ cwd: root, entry: null, env: {}, wanted: (name) => name === '@anthropic-ai/sdk' || name === '@unrelated/thing' });
  assert.deepEqual(found, { '@anthropic-ai/sdk': '0.140.0' });
});

test('a missing node_modules, an unreadable manifest and a package without a version are all fine', () => {
  const root = app();
  fakePackage(root, 'openai', '5.0.0');
  fs.writeFileSync(path.join(root, 'node_modules', 'openai', 'package.json'), '{not json');
  fakePackage(root, 'express', '5.0.0');
  fs.writeFileSync(path.join(root, 'node_modules', 'express', 'package.json'), '{"name":"express"}');
  const found = detect.scan({ cwd: root, entry: null, env: {}, wanted: everything });
  assert.equal(found.openai, null);
  assert.equal(found.express, null);
  assert.deepEqual(detect.scan({ cwd: tmpdir(), entry: '/does/not/exist.js', env: {}, wanted: everything }), {});
});

test('NODE_PATH directories are searched too', () => {
  const library = tmpdir();
  fakePackage(library, 'openai', '6.0.0');
  const found = detect.scan({ cwd: tmpdir(), entry: null, env: { NODE_PATH: path.join(library, 'node_modules') }, wanted: everything });
  assert.equal(found.openai, '6.0.0');
});

for (const [name, expected] of [
  ['dd-trace', true],
  ['langfuse', true],
  ['@langfuse/otel', true],
  ['@traceloop/node-server-sdk', true],
  ['@traceloop/instrumentation-openai', true],
  ['@arizeai/openinference-instrumentation-openai', true],
  ['newrelic', true],
  ['@elastic/opentelemetry-node', true],
  ['@arizeai/phoenix-otel', true],
  ['@pydantic/logfire-node', true],
  ['impact-ai', true],
  ['@impact-ai/sdk', true],
  ['@pydantic/logfire-api', false],
  ['@opentelemetry/sdk-node', false],
  ['@opentelemetry/api', false],
  ['openai', false],
  ['@arizeai/openinference-core', false],
]) {
  test(name + ' ' + (expected ? 'is' : 'is not') + ' a tracer of its own', () => assert.equal(detect.isTracerPackage(name), expected));
}

test('a tracer package in the application stands the package down, and the detail names it with its version', () => {
  const findings = find({ packages: { 'dd-trace': '5.2.0', openai: '5.0.0' } });
  assert.deepEqual(findings, [{ kind: 'distribution', name: 'dd-trace', version: '5.2.0' }]);
  assert.equal(detect.describe(findings), 'dd-trace 5.2.0');
});

test('Traceloop and Langfuse stand it down as well', () => {
  const detail = detect.describe(find({ packages: { '@traceloop/node-server-sdk': '0.19.0', langfuse: '3.30.0' } }));
  assert.match(detail, /^@traceloop\/node-server-sdk 0\.19\.0, langfuse 3\.30\.0$/);
});

test('--require and --import of a tracer, in NODE_OPTIONS or on the command line, are found', () => {
  const nodeOptions = '--max-old-space-size=512 --require dd-trace/init --import=@opentelemetry/auto-instrumentations-node/register';
  const findings = find({ nodeOptions, execArgv: ['-r', '/datadog-lib/node_modules/newrelic/index.js'] });
  assert.deepEqual(
    findings.map((f) => f.name).sort(),
    ['@opentelemetry/auto-instrumentations-node/register', 'dd-trace/init', 'newrelic/index.js']
  );
  assert.ok(findings.every((f) => f.kind === 'launcher'));
  assert.match(detect.describe(findings), /\(preload\)/);
});

test('our own --require, and the application\'s ordinary preloads, are not tracers', () => {
  const nodeOptions = '--require /otel-auto-instrumentation-nodejs/autoinstrumentation.js -r dotenv/config --require ./setup.js --import tsx';
  assert.deepEqual(find({ nodeOptions }), []);
});

test('a provider an earlier preload registered, or a tracer it loaded, is found at run time', () => {
  const cache = { '/app/node_modules/dd-trace/packages/dd-trace/index.js': {}, '/app/node_modules/express/index.js': {} };
  const findings = find({ cache, globalState: { 'opentelemetry.js.api.1': { trace: {}, version: '1.10.0' } } });
  assert.equal(findings.length, 2);
  assert.match(detect.describe(findings), /dd-trace \(loaded before this package\)/);
  assert.match(detect.describe(findings), /an OpenTelemetry tracer provider \(registered before this package\)/);
});

test('a global API without a provider (only a diag logger was set) is not evidence', () => {
  assert.deepEqual(find({ globalState: { 'opentelemetry.js.api.1': { diag: {}, version: '1.10.0' } } }), []);
});

test('OTEL_TRACES_EXPORTER other than otlp is the workload\'s own pipeline', () => {
  assert.deepEqual(find({ env: { OTEL_TRACES_EXPORTER: 'console' } }), [{ kind: 'environment', name: 'OTEL_TRACES_EXPORTER' }]);
  assert.deepEqual(find({ env: { OTEL_TRACES_EXPORTER: 'otlp' } }), []);
});

test('inside the kit, exporter settings with the OpenTelemetry SDK installed are a pipeline of the workload\'s own', () => {
  const env = { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://theirs:4318', OTEL_EXPORTER_OTLP_HEADERS: 'a=b' };
  const findings = find({ env, packages: { '@opentelemetry/sdk-node': '0.300.0' } });
  assert.deepEqual(findings.map((f) => f.name), ['OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_HEADERS']);
  assert.equal(detect.describe(findings), 'OTEL_EXPORTER_OTLP_ENDPOINT (set by the workload), OTEL_EXPORTER_OTLP_HEADERS (set by the workload)');
});

test('exporter settings without the SDK, or outside the kit, are left alone', () => {
  const env = { OTEL_EXPORTER_OTLP_ENDPOINT: 'http://theirs:4318' };
  assert.deepEqual(find({ env: Object.assign({ IMPACT_OTLP_ENDPOINT: 'http://g:4318' }, env) }), []);
  assert.deepEqual(find({ env, packages: { '@opentelemetry/sdk-node': '0.300.0' } }), []);
});

test('Zipkin and Jaeger settings count as exporter settings', () => {
  const env = { IMPACT_OTLP_ENDPOINT: 'http://g:4318', OTEL_EXPORTER_ZIPKIN_ENDPOINT: 'http://z:9411' };
  assert.deepEqual(find({ env, packages: { '@opentelemetry/sdk-trace-node': '2.12.0' } }).map((f) => f.name), ['OTEL_EXPORTER_ZIPKIN_ENDPOINT']);
});

test('the description is cut at 200 characters, and a launcher comes before a distribution', () => {
  const many = {};
  for (let i = 0; i < 40; i += 1) many['@traceloop/instrumentation-provider' + i] = '1.0.0';
  const detail = detect.describe(find({ packages: many, nodeOptions: '--require dd-trace/init' }));
  assert.ok(detail.length <= 200 && detail.endsWith('...'));
  assert.ok(detail.startsWith('dd-trace/init (preload)'));
});

test('specifiers name their package whatever the form', () => {
  const cases = [
    ['dd-trace/init', 'dd-trace'],
    ['@opentelemetry/auto-instrumentations-node/register', '@opentelemetry/auto-instrumentations-node'],
    ['/datadog-lib/node_modules/dd-trace/init.js', 'dd-trace'],
    ['file:///app/node_modules/@scope/pkg/index.mjs', '@scope/pkg'],
    ['./tracing.js', null],
    ['/app/tracing.js', null],
    ['C:\\app\\tracing.js', null],
  ];
  for (const [specifier, expected] of cases) assert.equal(detect.specifierPackage(specifier), expected, specifier);
});

test('NODE_OPTIONS in both forms of every preload flag', () => {
  assert.deepEqual(detect.preloads(['--require=a'], '--require b -r c --import=d --import e --loader f --experimental-loader=g --other h'), ['b', 'c', 'd', 'e', 'f', 'g', 'a']);
  assert.deepEqual(detect.preloads(undefined, undefined), []);
  assert.deepEqual(detect.preloads([], '--require'), []);
});

for (const [files, argv1, execArgv, expected, why] of [
  [{ 'a.mjs': '' }, 'a.mjs', [], 'esm', 'the extension .mjs'],
  [{ 'a.cjs': '' }, 'a.cjs', [], 'cjs', 'the extension .cjs'],
  [{ 'package.json': '{"type":"module"}', 'a.js': 'console.log(1)' }, 'a.js', [], 'esm', 'the package type module'],
  [{ 'package.json': '{"type":"commonjs"}', 'a.js': 'import x from "y"' }, 'a.js', [], 'cjs', 'the package type commonjs'],
  [{ 'package.json': '{"name":"x"}', 'a.js': 'const x = require("y")' }, 'a.js', [], 'cjs', 'a plain script'],
  [{ 'package.json': '{"name":"x"}', 'a.js': '// c\nimport OpenAI from "openai";\nconsole.log(OpenAI)' }, 'a.js', [], 'esm', 'module syntax without a type'],
  [{ 'package.json': '{"name":"x"}', 'a.js': 'export default 1' }, 'a.js', [], 'esm', 'an export without a type'],
  [{ 'package.json': '{"type":"module"}', 'sub/b.js': '' }, 'sub/b', [], 'esm', 'an entry without its extension'],
  [{ 'a.js': 'x' }, 'a.js', ['--experimental-default-type=module'], 'esm', 'the default-type flag'],
  [{}, undefined, [], 'cjs', 'no entry (a REPL or -e)'],
  [{}, 'missing.js', [], 'cjs', 'an entry that is not there'],
]) {
  test('the module system is ' + expected + ' for ' + why, () => {
    const root = app(files);
    const entry = argv1 === undefined ? undefined : path.join(root, argv1);
    assert.equal(detect.moduleSystem(entry, execArgv, ''), expected);
  });
}

test('package managers are recognised by the program they run, and the application is not one', () => {
  for (const program of ['/usr/local/lib/node_modules/npm/bin/npm-cli.js', '/usr/local/lib/node_modules/npm/bin/npx-cli.js', '/opt/yarn-v1.22.19/bin/yarn.js', '/usr/local/lib/node_modules/corepack/dist/corepack.js']) {
    assert.equal(detect.isPackageManager(program), true, program);
  }
  for (const program of ['/app/server.js', '/app/node_modules/.bin/next', undefined, '']) assert.equal(detect.isPackageManager(program), false, String(program));
});

// --- the tool the setup report names: the tool beside the package ---------------------------------------------------

function named(packages, overrides) {
  const findings = find(Object.assign({ packages }, overrides || {}));
  return detect.toolOf(findings, packages, (overrides && overrides.env) || {});
}

for (const [packages, tool] of [
  [{ 'dd-trace': '5.60.0' }, { id: 'datadog', version: '5.60.0', exporter: null }],
  [{ '@traceloop/node-server-sdk': '0.19.0', '@traceloop/instrumentation-openai': '0.19.0' }, { id: 'traceloop', version: '0.19.0', exporter: null }],
  [{ langfuse: '3.38.0' }, { id: 'langfuse', version: '3.38.0', exporter: null }],
  [{ '@langfuse/otel': '4.2.0' }, { id: 'langfuse', version: '4.2.0', exporter: null }],
  [{ openlit: '1.9.0' }, { id: 'openlit', version: '1.9.0', exporter: null }],
  [{ newrelic: '13.4.0' }, { id: 'newrelic', version: '13.4.0', exporter: null }],
  [{ '@elastic/opentelemetry-node': '1.3.0' }, { id: 'elastic-edot', version: '1.3.0', exporter: null }],
  [{ '@splunk/otel': '3.1.0' }, { id: 'splunk-otel', version: '3.1.0', exporter: null }],
  [{ '@honeycombio/opentelemetry-node': '0.7.0' }, { id: 'honeycomb', version: '0.7.0', exporter: null }],
  [{ '@arizeai/phoenix-otel': '0.3.0' }, { id: 'phoenix', version: '0.3.0', exporter: null }],
  [{ '@pydantic/logfire-node': '0.9.0' }, { id: 'logfire', version: '0.9.0', exporter: null }],
]) {
  test('a tracer names its tool by the source id and its version: ' + Object.keys(packages)[0], () => {
    assert.deepEqual(named(packages), tool);
  });
}

for (const packages of [
  { '@arizeai/openinference-instrumentation-openai': '4.4.2' }, // OpenInference's instrumentation alone: several tools start it
  { '@traceloop/instrumentation-openai': '0.19.0' },
  { '@aws/aws-distro-opentelemetry-node-autoinstrumentation': '0.8.0' },
]) {
  test('a tracer of no known tool names none: ' + Object.keys(packages)[0], () => {
    assert.equal(named(packages), null);
  });
}

test('a preload names its tool, with the version of its package when it is installed', () => {
  assert.deepEqual(named({ 'dd-trace': '5.60.0' }, { nodeOptions: '--require dd-trace/init' }), { id: 'datadog', version: '5.60.0', exporter: null });
  assert.deepEqual(named({}, { nodeOptions: '--require /app/node_modules/newrelic/index.js' }), { id: 'newrelic', version: null, exporter: null });
  assert.deepEqual(named({}, { nodeOptions: '--require @opentelemetry/auto-instrumentations-node/register' }), { id: 'opentelemetry', version: null, exporter: null });
});

test('a tracer loaded before this package, or a provider registered before it, names its tool', () => {
  assert.deepEqual(named({}, { cache: { '/app/node_modules/dd-trace/index.js': {} } }), { id: 'datadog', version: null, exporter: null });
  const globalState = { 'opentelemetry.js.api.1': { trace: {} } };
  assert.deepEqual(named({ '@opentelemetry/sdk-trace-node': '2.1.0' }, { globalState }), { id: 'opentelemetry', version: '2.1.0', exporter: null });
});

test('no finding names no tool', () => {
  assert.equal(named({ openai: '7.50.0', langsmith: '0.11.8' }), null);
});

for (const [env, exporter] of [
  [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf' }, 'otlp-http'],
  [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/json' }, 'otlp-http'],
  [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'grpc' }, 'otlp-grpc'],
  [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: 'grpc' }, 'otlp-grpc'],
  [{ OTEL_TRACES_EXPORTER: 'zipkin', OTEL_EXPORTER_OTLP_PROTOCOL: 'grpc' }, 'zipkin'],
  [{ OTEL_TRACES_EXPORTER: 'Console' }, 'console'],
  [{ OTEL_TRACES_EXPORTER: 'otlp', OTEL_EXPORTER_ZIPKIN_ENDPOINT: 'http://zipkin:9411' }, 'zipkin'],
  [{}, null], // set up in code, or the SDK's default: not shown by the settings
  [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'carrier-pigeon' }, null],
  [{ OTEL_TRACES_EXPORTER: 'none' }, null],
]) {
  test('an OpenTelemetry pipeline names its exporter kind and never a value: ' + JSON.stringify(env), () => {
    const packages = { '@opentelemetry/sdk-node': '0.300.0' };
    const tool = named(packages, { env: Object.assign({ IMPACT_OTLP_ENDPOINT: 'http://gw:4318', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318' }, env) });
    assert.deepEqual(tool, { id: 'opentelemetry', version: '0.300.0', exporter });
  });
}

test('the first finding, in the detail\'s order, that shows a tool names it', () => {
  const env = { IMPACT_OTLP_ENDPOINT: 'http://gw:4318', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318' };
  const packages = { '@arizeai/openinference-instrumentation-openai': '4.4.2', 'dd-trace': '5.60.0', '@opentelemetry/sdk-node': '0.300.0' };
  assert.deepEqual(named(packages, { env }), { id: 'datadog', version: '5.60.0', exporter: null });
});

for (const version of ['1.0 beta', 'x'.repeat(65), '-1', '1.0\n', 'é1']) {
  test('a version impact.ai would not keep is left out: ' + JSON.stringify(version), () => {
    assert.deepEqual(named({ openlit: version }), { id: 'openlit', version: null, exporter: null });
  });
}

test('scan reads LangSmith\'s version, so the report can name it', () => {
  assert.equal(detect.wantedNames(() => false)('langsmith'), true);
});
