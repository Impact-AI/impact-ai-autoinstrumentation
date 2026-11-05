// SPDX-License-Identifier: Apache-2.0

// The licenses of what the Node.js image bundles are collected by the build, from each package's own files; and the build
// refuses what must not be in the payload.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import * as assemble from '../../tools/assemble_payload.mjs';

const APACHE = 'Apache License\nVersion 2.0, January 2004\n(the canonical text, here a stand-in)\n';

function scratch() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'assemble-'));
}

/** node_modules/<name> with a manifest and the given files. */
function installed(root, name, version, manifest, files) {
  const directory = path.join(root, 'node_modules', ...name.split('/'));
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify({ name, version, ...manifest }));
  for (const [relative, text] of Object.entries(files || {})) {
    fs.mkdirSync(path.dirname(path.join(directory, relative)), { recursive: true });
    fs.writeFileSync(path.join(directory, relative), text);
  }
  return directory;
}

function collect(trees, out) {
  return assemble.writeLicenses(trees.map((root) => assemble.packagesOf(path.join(root, 'node_modules'))), out, APACHE);
}

test('a license file in the package is copied and indexed', () => {
  const root = scratch();
  const out = scratch();
  installed(root, 'ms', '2.1.3', { license: 'MIT', author: 'Zeit' }, { LICENSE: 'MIT text' });
  const index = collect([root], out);
  assert.equal(fs.readFileSync(path.join(out, 'nodejs', 'ms@2.1.3', 'LICENSE'), 'utf8'), 'MIT text');
  assert.deepEqual(index, [{ name: 'ms', version: '2.1.3', license: 'MIT', authors: ['Zeit'], files: ['LICENSE'], supplied: false }]);
});

test('licence, notice and copying files in their usual spellings are all copied', () => {
  const root = scratch();
  const out = scratch();
  installed(root, 'legacy', '1.0.0', { license: 'MIT' }, { 'LICENSE.md': 'a', 'licence.txt': 'b', NOTICE: 'c', COPYING: 'd', 'README.md': 'not a license', 'index.js': '' });
  const [entry] = collect([root], out);
  assert.deepEqual(entry.files, ['COPYING', 'LICENSE.md', 'NOTICE', 'licence.txt']);
});

test('a LICENSES directory is copied with its files', () => {
  const root = scratch();
  const out = scratch();
  installed(root, '@opentelemetry/instrumentation', '0.300.0', { license: 'Apache-2.0' }, { 'LICENSES/Apache-2.0.txt': 'text', LICENSE: 'main' });
  const [entry] = collect([root], out);
  assert.deepEqual(entry.files, ['LICENSE', path.join('LICENSES', 'Apache-2.0.txt')]);
});

test('scoped names make a folder name without a slash', () => {
  const root = scratch();
  const out = scratch();
  installed(root, '@arizeai/openinference-core', '2.8.1', { license: 'Apache-2.0' }, { LICENSE: 'x' });
  collect([root], out);
  assert.ok(fs.existsSync(path.join(out, 'nodejs', '@arizeai__openinference-core@2.8.1', 'LICENSE')));
});

test('an Apache package without a license file gets the canonical text, and the index says it was supplied', () => {
  const root = scratch();
  const out = scratch();
  installed(root, 'plain-apache', '1.0.0', { license: 'Apache-2.0' });
  const [entry] = collect([root], out);
  assert.equal(fs.readFileSync(path.join(out, 'nodejs', 'plain-apache@1.0.0', 'LICENSE'), 'utf8'), APACHE);
  assert.equal(entry.supplied, true);
  assert.match(fs.readFileSync(path.join(out, 'nodejs', 'INDEX.md'), 'utf8'), /plain-apache \| 1\.0\.0 \| Apache-2\.0 \| - \| LICENSE \(supplied by the build\)/);
});

test('a package without a license file, whose license we have no text for, fails the build', () => {
  const root = scratch();
  installed(root, 'mystery', '1.0.0', { license: 'MIT' });
  assert.throws(() => collect([root], scratch()), /mystery 1\.0\.0: no license file in the package/);
});

test('a package that names no license fails the build', () => {
  const root = scratch();
  installed(root, 'silent', '1.0.0', {}, { LICENSE: 'text' });
  assert.throws(() => collect([root], scratch()), /silent 1\.0\.0: no license name/);
});

test('the license is read from the old forms too', () => {
  assert.equal(assemble.licenseName({ license: { type: 'ISC' } }), 'ISC');
  assert.equal(assemble.licenseName({ licenses: [{ type: 'MIT' }, { type: 'Apache-2.0' }] }), 'MIT OR Apache-2.0');
  assert.equal(assemble.licenseName({ license: '  MIT  ' }), 'MIT');
  assert.equal(assemble.licenseName({}), null);
});

test('authors are names only: no e-mail address, no home page', () => {
  const names = assemble.authorNames({
    author: 'Ada Lovelace <ada@example.test> (https://example.test)',
    contributors: ['oss-devs@example.test', { name: 'Grace Hopper', email: 'grace@example.test' }, 'Ada Lovelace'],
    maintainers: [{ name: 'Linus' }],
  });
  assert.deepEqual(names, ['Ada Lovelace', 'Grace Hopper', 'Linus']);
});

test('the same package in two trees is listed once, two versions are two entries', () => {
  const [one, two, out] = [scratch(), scratch(), scratch()];
  installed(one, 'api', '1.10.0', { license: 'Apache-2.0' }, { LICENSE: 'x' });
  installed(two, 'api', '1.10.0', { license: 'Apache-2.0' }, { LICENSE: 'x' });
  installed(two, 'core', '2.0.0', { license: 'Apache-2.0' }, { LICENSE: 'x' });
  installed(one, 'core', '3.0.0', { license: 'Apache-2.0' }, { LICENSE: 'x' });
  const index = collect([one, two], out);
  assert.deepEqual(index.map((e) => `${e.name}@${e.version}`), ['api@1.10.0', 'core@2.0.0', 'core@3.0.0']);
});

test('nested packages are found, and so are scoped ones', () => {
  const root = scratch();
  const parent = installed(root, 'outer', '1.0.0', { license: 'MIT' }, { LICENSE: 'x' });
  installed(parent, 'inner', '2.0.0', { license: 'MIT' }, { LICENSE: 'y' });
  installed(root, '@scope/pkg', '3.0.0', { license: 'MIT' }, { LICENSE: 'z' });
  const names = assemble.packagesOf(path.join(root, 'node_modules')).map((p) => `${p.name}@${p.version}`).sort();
  assert.deepEqual(names, ['@scope/pkg@3.0.0', 'inner@2.0.0', 'outer@1.0.0']);
});

test('pruning removes declarations, maps, sources and docs, and keeps the code and every license file', () => {
  const root = scratch();
  const directory = installed(root, 'pkg', '1.0.0', { license: 'MIT' }, {
    'index.js': 'code',
    'index.cjs': 'code',
    'hook.mjs': 'code',
    'index.d.ts': 'x',
    'index.d.cts': 'x',
    'index.js.map': 'x',
    'src/index.ts': 'x',
    'README.md': 'x',
    'docs/guide.html': 'x',
    'test/a.js': 'x',
    LICENSE: 'keep',
    NOTICE: 'keep',
    'LICENSES/MIT.txt': 'keep',
  });
  assemble.prune(path.join(root, 'node_modules'));
  const left = [];
  assemble.walk(directory, (full, entry) => entry.isFile() && left.push(path.relative(directory, full)));
  assert.deepEqual(left.sort(), ['LICENSE', 'LICENSES/MIT.txt', 'NOTICE', 'hook.mjs', 'index.cjs', 'index.js', 'package.json']);
});

test('the build refuses a provider SDK, a protocol buffer stack, a native add-on, a link and a stand-in that is not the stand-in', () => {
  const root = scratch();
  const modules = path.join(root, 'node_modules');
  installed(root, '@anthropic-ai/sdk', '1.4.0', { license: 'MIT' });
  installed(root, 'protobufjs', '7.0.0', { license: 'BSD-3-Clause' });
  installed(root, 'native', '1.0.0', { license: 'MIT', gypfile: true }, { 'build/addon.node': '' });
  installed(root, 'openai', '7.50.0', { license: 'Apache-2.0' });
  fs.symlinkSync('/etc', path.join(modules, 'linked'));
  assert.throws(
    () => assemble.check('node22', modules, assemble.packagesOf(modules)),
    (error) =>
      /@anthropic-ai\/sdk 1\.4\.0 must not be in the payload/.test(error.message) &&
      /protobufjs 7\.0\.0 must not be in the payload/.test(error.message) &&
      /native is a native add-on/.test(error.message) &&
      /openai 7\.50\.0 is the provider SDK/.test(error.message) &&
      /linked is a symbolic link/.test(error.message) &&
      /addon\.node is a binary/.test(error.message)
  );
});

test('the stand-in for openai is allowed', () => {
  const root = scratch();
  installed(root, 'openai', assemble.SHIM_VERSION, { license: 'Apache-2.0', private: true }, { 'index.js': '' });
  const modules = path.join(root, 'node_modules');
  assert.doesNotThrow(() => assemble.check('node22', modules, assemble.packagesOf(modules)));
});

test('a whole payload is assembled: the preload, the modules, the tree, the version, the licenses and the modes', () => {
  const [trees, out, src] = [scratch(), scratch(), scratch()];
  for (const tree of assemble.TREES) {
    fs.mkdirSync(path.join(trees, tree), { recursive: true });
    fs.writeFileSync(path.join(trees, tree, 'package.json'), '{}');
    installed(path.join(trees, tree), 'ms', '2.1.3', { license: 'MIT' }, { LICENSE: 'MIT text', 'index.js': 'module.exports = 1' });
  }
  fs.mkdirSync(path.join(src, 'impact'));
  fs.writeFileSync(path.join(src, 'autoinstrumentation.js'), '// preload');
  fs.writeFileSync(path.join(src, 'impact', 'bootstrap.js'), '// bootstrap');
  fs.writeFileSync(path.join(src, 'impact', 'version.js'), "exports.VERSION = '0.0.0-dev';");
  const qualification = path.join(src, 'qualification.json');
  fs.writeFileSync(qualification, '{"cells":[]}');
  const license = path.join(src, 'LICENSE');
  fs.writeFileSync(license, APACHE);
  const target = path.join(out, 'out');
  assemble.assemble({ trees, src, out: target, version: '1.2.3', license, qualification });
  const root = path.join(target, 'autoinstrumentation');
  assert.equal(fs.readFileSync(path.join(root, 'impact', 'version.js'), 'utf8').includes('exports.VERSION = "1.2.3";'), true);
  assert.deepEqual(
    fs.readdirSync(root).filter((name) => /^node\d+$/.test(name)),
    ['node22'],
    'the payload carries the one tree, and none for a Node that is past its end of life'
  );
  assert.ok(fs.existsSync(path.join(root, 'node22', 'node_modules', 'ms', 'index.js')));
  assert.equal(fs.readFileSync(path.join(root, 'qualification.json'), 'utf8'), '{"cells":[]}');
  assert.ok(fs.existsSync(path.join(target, 'licenses', 'nodejs', 'ms@2.1.3', 'LICENSE')));
  const mode = fs.statSync(path.join(root, 'autoinstrumentation.js')).mode & 0o777;
  assert.equal(mode & 0o444, 0o444, 'readable by any user');
  assert.equal(mode & 0o022, 0, 'writable by none but the owner');
});
