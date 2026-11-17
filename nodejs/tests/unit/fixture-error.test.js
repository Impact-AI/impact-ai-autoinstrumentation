// SPDX-License-Identifier: Apache-2.0
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

test('LangSmith fixture errors do not expose the exception or stack in HTTP responses', async () => {
  let handle;
  const secret = 'synthetic-private-error-detail';
  const modules = {
    'node:http': { createServer(fn) { handle = fn; return { listen() {} }; } },
    'langsmith/traceable': { traceable: (fn) => fn },
    'langsmith/wrappers': { wrapOpenAI: (value) => value },
    langsmith: { Client: class {} },
    '@langchain/openai': { ChatOpenAI: class {} },
    '@langchain/core/tools': { tool: (fn) => fn },
    '@langchain/core/callbacks/promises': {},
    'dist/singletons/tracer.cjs': {},
    openai: { OpenAI: class { constructor() {
      this.chat = { completions: { async create() { throw new Error(secret); } } };
    } } },
  };
  const requireFixture = (name) => {
    assert.ok(name in modules, name);
    return modules[name];
  };
  requireFixture.resolve = () => 'package.json';
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../../testing/apps/nodejs/src/langsmith.cjs'), 'utf8'), {
    require: requireFixture, process: { env: {} }, Buffer, console,
  });
  const request = new EventEmitter();
  request.url = '/langsmith/traceable';
  let body;
  const response = { setHeader() {}, end(value) { body = value; } };
  const pending = handle(request, response);
  request.emit('end');
  await pending;
  assert.equal(response.statusCode, 500);
  assert.deepEqual(JSON.parse(body), { error: 'request failed' });
  assert.ok(!body.includes(secret));
});
