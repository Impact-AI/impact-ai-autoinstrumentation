// SPDX-License-Identifier: Apache-2.0
'use strict';

// The AI libraries and agent frameworks found in the application's node_modules, for the `impact.setup` span.
//
// They are read from the package directories that detect.scan listed: nothing of the application is loaded or run. Each
// one is either traced by an instrumentation this image ships or it is not; `untraced` names the ones the image cannot
// trace (yet), so that Cover your AI can say what is missing rather than show the workload as covered.
//
// The wire shape is fixed by the monorepo, which drops an entry that does not fit it: at most 20 entries per list, each
// "name" or "name version", one space between, a name of letters, digits, dot, underscore and hyphen. npm names with a
// scope are written without the `@` and with a hyphen for the slash: `@anthropic-ai/sdk` is `anthropic-ai-sdk`.

const MAX_ENTRIES = 20;
const MAX_NAME = 64;
const MAX_VERSION = 64;

// npm package -> its kind. "agent": agent and orchestration frameworks. "provider": model provider SDKs. When more are
// found than a list holds, agent frameworks are kept before provider SDKs.
const CATALOG = {
  '@openai/agents': 'agent',
  '@langchain/langgraph': 'agent',
  '@langchain/core': 'agent',
  langchain: 'agent',
  llamaindex: 'agent',
  '@mastra/core': 'agent',
  '@modelcontextprotocol/sdk': 'agent',
  '@google/adk': 'agent',
  '@strands-agents/sdk': 'agent',
  '@voltagent/core': 'agent',
  openai: 'provider',
  '@anthropic-ai/sdk': 'provider',
  '@google/genai': 'provider',
  '@google/generative-ai': 'provider',
  '@aws-sdk/client-bedrock-runtime': 'provider',
  '@mistralai/mistralai': 'provider',
  'cohere-ai': 'provider',
  'groq-sdk': 'provider',
  ollama: 'provider',
  ai: 'provider',
  '@azure/openai': 'provider',
  'together-ai': 'provider',
  replicate: 'provider',
  '@huggingface/inference': 'provider',
};
const RANK = { agent: 0, provider: 1 };

// What this image traces, by the npm package it is a library of.
//   openai      OpenInference's instrumentation of the openai package: 5 to 7 by the current release, 4 by the older one
//   anthropic   the SDK's own spans, from 0.134.0 on (no module patching: it does not depend on the module system)
const TRACED = {
  openai: { name: 'openai', min: [4, 0, 0], below: [8, 0, 0], supported: '4 to 7' },
  '@anthropic-ai/sdk': { name: 'anthropic', min: [0, 134, 0], below: null, supported: '0.134.0 or later' },
};

function isCatalogued(name) {
  return Object.prototype.hasOwnProperty.call(CATALOG, name);
}

/** `@scope/name` as the wire shape has it: scope and name joined by a hyphen, no `@`. */
function label(name, version) {
  const base = String(name).replace(/^@/, '').replace(/\//g, '-').slice(0, MAX_NAME);
  const cleaned = String(version || '').replace(/\s+/g, '').slice(0, MAX_VERSION);
  return cleaned ? base + ' ' + cleaned : base;
}

function parseVersion(version) {
  const numbers = String(version || '').split(/[-+]/)[0].split('.');
  return [parseInt(numbers[0], 10) || 0, parseInt(numbers[1], 10) || 0, parseInt(numbers[2], 10) || 0];
}

function compare(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

/** Why the installed version is outside the range this image traces; empty when it is inside (or unknown). */
function outsideRange(npmName, version) {
  const traced = TRACED[npmName];
  if (!traced || !version) return '';
  const parsed = parseVersion(version);
  const tooOld = compare(parsed, traced.min) < 0;
  const tooNew = traced.below !== null && compare(parsed, traced.below) >= 0;
  return tooOld || tooNew ? npmName + ' ' + version + ' installed, supported ' + traced.supported : '';
}

/**
 * The catalogued libraries among `packages` ({npm name: version}), and which of them the image cannot trace.
 *
 * `traced` is the set of npm names the image traces for this process: armed, inside the supported range, not switched
 * off, and loadable in the application's module system. Agent frameworks come before provider SDKs, each alphabetically;
 * a list holds at most MAX_ENTRIES of them.
 */
function survey(packages, traced) {
  const present = Object.keys(packages || {}).filter(isCatalogued);
  if (present.indexOf('@langchain/langgraph') >= 0 || present.indexOf('langchain') >= 0) {
    const at = present.indexOf('@langchain/core');
    if (at >= 0) present.splice(at, 1); // the larger package says it, once
  }
  present.sort((a, b) => RANK[CATALOG[a]] - RANK[CATALOG[b]] || (a < b ? -1 : a > b ? 1 : 0));
  const found = [];
  const untraced = [];
  for (const name of present) {
    const entry = label(name, packages[name]);
    found.push(entry);
    if (!(traced && traced.has(name))) untraced.push(entry);
  }
  return { frameworks: found.slice(0, MAX_ENTRIES), untraced: untraced.slice(0, MAX_ENTRIES) };
}

module.exports = { CATALOG, TRACED, MAX_ENTRIES, isCatalogued, label, outsideRange, survey, parseVersion, compare };
