// SPDX-License-Identifier: Apache-2.0
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const runtime = require('../../src/impact/runtime');

// process.emitWarning is replaced by a recorder for the length of a test, so that nothing reaches the output of the runner.
function recording(body) {
  const original = process.emitWarning;
  const seen = [];
  const recorder = function (...args) {
    seen.push(args);
  };
  process.emitWarning = recorder;
  try {
    body(seen, recorder);
  } finally {
    process.emitWarning = original;
  }
}

test('the deprecation of module.register() (DEP0205) is not shown, whichever way Node words the call', () => {
  recording((seen) => {
    runtime.registerLoaderHook(() => {
      process.emitWarning('`module.register()` is deprecated.', 'DeprecationWarning', 'DEP0205');
      process.emitWarning('also', { type: 'DeprecationWarning', code: 'DEP0205' });
      const error = new Error('as an error');
      error.code = 'DEP0205';
      process.emitWarning(error);
    });
    assert.deepEqual(seen, []);
  });
});

test('every other warning goes where it would have gone, and process.emitWarning is put back', () => {
  recording((seen, recorder) => {
    runtime.registerLoaderHook(() => {
      process.emitWarning('one', 'DeprecationWarning', 'DEP0040');
      process.emitWarning('two');
      process.emitWarning('three', { code: 'OTHER' });
    });
    assert.deepEqual(seen, [['one', 'DeprecationWarning', 'DEP0040'], ['two'], ['three', { code: 'OTHER' }]]);
    assert.equal(process.emitWarning, recorder);
    process.emitWarning('after', 'DeprecationWarning', 'DEP0205');
    assert.equal(seen.length, 4, 'the same warning, outside of the call, is not the package\'s and is shown');
  });
});

test('the hook is registered with the arguments it was given, and an error of the registration is the caller\'s', () => {
  recording((seen, recorder) => {
    const calls = [];
    runtime.registerLoaderHook((...args) => calls.push(args), 'file:///hook.mjs', { parentURL: 'file:///here' });
    assert.deepEqual(calls, [['file:///hook.mjs', { parentURL: 'file:///here' }]]);
    assert.throws(
      () =>
        runtime.registerLoaderHook(() => {
          throw new Error('no loader');
        }),
      /no loader/
    );
    assert.equal(process.emitWarning, recorder, 'put back after an error as well');
  });
});
