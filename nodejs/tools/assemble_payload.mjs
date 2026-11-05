#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0

// Assemble the payload of the Node.js image: what the Operator's init container copies into the application pod.
//
//   /autoinstrumentation/autoinstrumentation.js   the preload (NODE_OPTIONS=--require ...)
//   /autoinstrumentation/impact/                  its modules
//   /autoinstrumentation/node22/node_modules/     OpenTelemetry JavaScript 3.x, for Node 22.15 and later
//   /autoinstrumentation/qualification.json       what the matrix of Node, module system and library versions proved
//
// The trees are installed before this runs, by `npm ci --ignore-scripts --install-links` from package-lock.json: every
// tarball is checked against the integrity hash in the lock, and nothing of any package is executed. They are plain
// JavaScript, so one build serves both architectures. This script prunes them (type declarations, source maps, sources,
// documentation), refuses what must not be in the payload, and writes next to it licenses/nodejs: the license and notice
// files of every package, copied from the package itself, and an index (INDEX.md and index.json) of name, version,
// license and authors. A package that declares Apache-2.0 and ships no license file gets the Apache License text beside
// it; any other package without a license file, or without a license name, fails the build.
//
// Node's own modules only, so it runs in any Node 20+ build image.

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

export const TREES = ['node22'];
const APACHE = 'Apache-2.0';

// Packages and files that must never be in the payload: the SDKs of the providers (the application has its own), and the
// protocol buffer and gRPC stacks (the exporter speaks JSON on Node's own http module).
export const FORBIDDEN = new Set([
  '@anthropic-ai/sdk',
  'protobufjs',
  '@grpc/grpc-js',
  '@opentelemetry/otlp-transformer',
  '@opentelemetry/otlp-exporter-base',
  'undici',
  'node-fetch',
  'axios',
]);
// The one package called openai is the stand-in of shims/openai, never the SDK.
export const SHIM_VERSION = '0.0.0-impact-shim';

const PRUNED_DIRECTORIES = new Set(['test', 'tests', '__tests__', 'docs', 'doc', 'examples', 'example', '.github', '.bin']);
const PRUNED_FILE = /(\.map|\.tsbuildinfo|\.d\.ts|\.d\.cts|\.d\.mts|\.ts|\.mts|\.cts|\.md|\.markdown|\.npmignore|\.eslintrc.*|\.editorconfig)$/i;
const KEPT_FILE = /^(licen[cs]e|copying|notice|patents)([._-].*)?$/i;
const LICENSE_FILE = /^(licen[cs]e|copying|notice)([._-].*)?$/i;

export function walk(directory, visit) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const full = path.join(directory, entry.name);
    visit(full, entry);
    if (entry.isDirectory() && !entry.isSymbolicLink() && fs.existsSync(full)) walk(full, visit);
  }
}

/** Every package of a node_modules tree, nested ones included: [{name, version, dir, manifest}]. */
export function packagesOf(nodeModules) {
  const found = [];
  const look = (modules) => {
    for (const entry of fs.readdirSync(modules, { withFileTypes: true })) {
      if (entry.name.startsWith('.')) continue;
      const full = path.join(modules, entry.name);
      if (entry.name.startsWith('@')) {
        for (const member of fs.readdirSync(full)) consider(path.join(full, member));
      } else {
        consider(full);
      }
    }
  };
  const consider = (dir) => {
    const manifestFile = path.join(dir, 'package.json');
    if (!fs.existsSync(manifestFile)) return;
    const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
    found.push({ name: manifest.name, version: manifest.version, dir, manifest });
    const nested = path.join(dir, 'node_modules');
    if (fs.existsSync(nested)) look(nested);
  };
  look(nodeModules);
  return found;
}

export function licenseName(manifest) {
  if (typeof manifest.license === 'string' && manifest.license.trim()) return manifest.license.trim();
  if (manifest.license && typeof manifest.license.type === 'string') return manifest.license.type;
  if (Array.isArray(manifest.licenses) && manifest.licenses.length) {
    return manifest.licenses.map((entry) => (typeof entry === 'string' ? entry : entry.type)).join(' OR ');
  }
  return null;
}

export function authorNames(manifest) {
  const names = new Set();
  const take = (value) => {
    const text = typeof value === 'string' ? value : value && value.name;
    // names only: an e-mail address or a URL of a third party does not belong in an index we publish
    const name = String(text || '').replace(/<[^>]*>/g, '').replace(/\([^)]*\)/g, '').trim();
    if (name && !name.includes('@')) names.add(name);
  };
  take(manifest.author);
  for (const list of [manifest.contributors, manifest.maintainers]) {
    if (Array.isArray(list)) list.forEach(take);
  }
  return [...names].sort();
}

/** The license and notice files of a package directory, by relative name (the LICENSES directory included). */
export function licenseFiles(dir) {
  const found = {};
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isFile() && LICENSE_FILE.test(entry.name)) found[entry.name] = path.join(dir, entry.name);
    if (entry.isDirectory() && /^licenses?$/i.test(entry.name)) {
      walk(path.join(dir, entry.name), (full, child) => {
        if (child.isFile()) found[path.relative(dir, full)] = full;
      });
    }
  }
  return found;
}

function folderName(name, version) {
  return `${name.replaceAll('/', '__')}@${version}`;
}

export function prune(nodeModules) {
  walk(nodeModules, (full, entry) => {
    if (entry.isDirectory() && PRUNED_DIRECTORIES.has(entry.name) && !/licen[cs]es?/i.test(entry.name)) {
      fs.rmSync(full, { recursive: true, force: true });
    } else if (entry.isFile() && PRUNED_FILE.test(entry.name) && !KEPT_FILE.test(entry.name)) {
      fs.rmSync(full, { force: true });
    }
  });
}

/** Refuse what must not be in a tree: a provider SDK, a native add-on, a link out of the tree. */
export function check(tree, nodeModules, packages) {
  const problems = [];
  for (const pkg of packages) {
    if (FORBIDDEN.has(pkg.name)) problems.push(`${tree}: ${pkg.name} ${pkg.version} must not be in the payload`);
    if (pkg.name === 'openai' && pkg.version !== SHIM_VERSION) {
      problems.push(`${tree}: openai ${pkg.version} is the provider SDK; only the stand-in ${SHIM_VERSION} may be here`);
    }
    if (pkg.manifest.gypfile || pkg.manifest.binary) problems.push(`${tree}: ${pkg.name} is a native add-on`);
  }
  walk(nodeModules, (full, entry) => {
    if (entry.isSymbolicLink()) problems.push(`${tree}: ${path.relative(nodeModules, full)} is a symbolic link`);
    if (entry.isFile() && /\.(node|so|dylib|dll|exe)$/.test(entry.name)) {
      problems.push(`${tree}: ${path.relative(nodeModules, full)} is a binary`);
    }
  });
  if (problems.length) throw new Error(problems.join('\n'));
}

export function writeLicenses(trees, out, apacheText) {
  const target = path.join(out, 'nodejs');
  fs.rmSync(target, { recursive: true, force: true });
  fs.mkdirSync(target, { recursive: true });
  const index = new Map();
  for (const packages of trees) {
    for (const pkg of packages) {
      const key = `${pkg.name}@${pkg.version}`;
      if (index.has(key)) continue;
      const spdx = licenseName(pkg.manifest);
      if (!spdx) throw new Error(`${pkg.name} ${pkg.version}: no license name in its package.json`);
      const files = licenseFiles(pkg.dir);
      const folder = path.join(target, folderName(pkg.name, pkg.version));
      fs.mkdirSync(folder, { recursive: true });
      let supplied = false;
      let written;
      if (Object.keys(files).length === 0) {
        if (spdx !== APACHE) {
          throw new Error(`${pkg.name} ${pkg.version}: no license file in the package, and no text to supply for ${spdx}`);
        }
        fs.writeFileSync(path.join(folder, 'LICENSE'), apacheText);
        supplied = true;
        written = ['LICENSE'];
      } else {
        written = Object.keys(files).sort();
        for (const relative of written) {
          fs.mkdirSync(path.dirname(path.join(folder, relative)), { recursive: true });
          fs.copyFileSync(files[relative], path.join(folder, relative));
        }
      }
      index.set(key, { name: pkg.name, version: pkg.version, license: spdx, authors: authorNames(pkg.manifest), files: written, supplied });
    }
  }
  const entries = [...index.values()].sort((a, b) => (a.name + a.version < b.name + b.version ? -1 : 1));
  fs.writeFileSync(path.join(target, 'index.json'), JSON.stringify(entries, null, 2) + '\n');
  const rows = entries.map(
    (d) =>
      `| ${d.name} | ${d.version} | ${d.license} | ${d.authors.join(', ') || '-'} | ${d.files.join(', ')}${d.supplied ? ' (supplied by the build)' : ''} |`
  );
  fs.writeFileSync(
    path.join(target, 'INDEX.md'),
    '# Node.js packages in this image\n\n' +
      'The license and notice files of each package are in the directory named after it, next to this index.\n\n' +
      '| Package | Version | License | Authors | Files |\n|---|---|---|---|---|\n' +
      rows.join('\n') +
      '\n'
  );
  return entries;
}

export function setModes(root) {
  const apply = (target, isDirectory) => {
    const mode = fs.statSync(target).mode;
    fs.chmodSync(target, (mode | (isDirectory ? 0o555 : 0o444)) & ~0o022);
  };
  apply(root, true);
  walk(root, (full, entry) => apply(full, entry.isDirectory()));
}

function parseArguments(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i].startsWith('--')) throw new Error(`unexpected argument ${argv[i]}`);
    options[argv[i].slice(2)] = argv[i + 1];
  }
  for (const required of ['trees', 'src', 'out', 'license', 'qualification']) {
    if (!options[required]) throw new Error(`--${required} is required`);
  }
  options.version = options.version || '0.0.0+dev';
  return options;
}

export function assemble(options) {
  const root = path.join(options.out, 'autoinstrumentation');
  fs.rmSync(options.out, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });
  const listings = [];
  for (const tree of TREES) {
    const source = path.join(options.trees, tree);
    const modules = path.join(source, 'node_modules');
    if (!fs.existsSync(modules)) throw new Error(`${modules} is missing: run npm ci --ignore-scripts --install-links first`);
    prune(modules);
    const packages = packagesOf(modules);
    check(tree, modules, packages);
    listings.push(packages);
    const destination = path.join(root, tree);
    fs.mkdirSync(destination, { recursive: true });
    fs.cpSync(modules, path.join(destination, 'node_modules'), { recursive: true, dereference: false });
    fs.copyFileSync(path.join(source, 'package.json'), path.join(destination, 'package.json'));
  }
  fs.copyFileSync(path.join(options.src, 'autoinstrumentation.js'), path.join(root, 'autoinstrumentation.js'));
  fs.cpSync(path.join(options.src, 'impact'), path.join(root, 'impact'), { recursive: true });
  fs.writeFileSync(
    path.join(root, 'impact', 'version.js'),
    `// SPDX-License-Identifier: Apache-2.0\n'use strict';\n\nexports.VERSION = ${JSON.stringify(options.version)};\n`
  );
  fs.copyFileSync(options.qualification, path.join(root, 'qualification.json'));
  const index = writeLicenses(listings, path.join(options.out, 'licenses'), fs.readFileSync(options.license, 'utf8'));
  setModes(options.out);
  console.error(`assembled ${root}; licenses of ${index.length} packages in ${path.join(options.out, 'licenses', 'nodejs')}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  try {
    assemble(parseArguments(process.argv.slice(2)));
  } catch (error) {
    console.error(String(error.message || error));
    process.exitCode = 1;
  }
}
