// SPDX-License-Identifier: Apache-2.0
'use strict';

// A gateway that hangs, or whose packets are dropped, must not slow a covered process at exit or make it grow, and a closed
// stderr must not crash it. Real processes (exit-app.js: the package's exporter and processor behind the SDK's provider),
// against a gateway that accepts and never answers, a black hole, one that refuses and one that is healthy. The time
// measured is the one between the application finishing its work and the process ending, which is what a rollout, a Job or
// a CronJob waits for.

const test = require('node:test');
const { describe } = test;
const assert = require('node:assert/strict');
const path = require('path');
const { Sink, hangingServer, blackHole, runNode } = require('./helpers');

const APP = path.join(__dirname, 'exit-app.js');
const LINUX = process.platform === 'linux' ? false : 'a black hole needs the full accept queue of Linux';

async function exitOf(gateway, spans, characters, options) {
  return runNode([APP, String(spans), String(characters || 200)], Object.assign({ env: { IMPACT_OTLP_ENDPOINT: gateway }, cap: 30000 }, options));
}

function assertEnded(result, seconds, what) {
  assert.notEqual(result.afterWork, null, what + ': the work was never done: ' + result.stderr.slice(-300));
  assert.ok(!result.capped, what + ': the process was still running ' + result.afterWork.toFixed(1) + ' s after its work');
  assert.equal(result.code, 0, what + ': ' + result.stderr.slice(-300));
  assert.ok(result.afterWork <= seconds, what + ': the process needed ' + result.afterWork.toFixed(1) + ' s to end');
}

// The scenarios are independent and mostly wait, so they run side by side.
describe('the end of a process with spans buffered', { concurrency: 6 }, () => {
  for (const spans of [1, 1200]) {
    test('a gateway that hangs delays it by at most five seconds, with ' + spans + ' span(s) buffered', { timeout: 60000 }, async () => {
      const gateway = await hangingServer();
      try {
        // It took 19 s for one span, 49 s for 1,200: three tries of five seconds for each batch, and nothing that cut them off.
        assertEnded(await exitOf(gateway.url, spans), 5, 'hanging, ' + spans + ' spans');
      } finally {
        await gateway.close();
      }
    });

    test('a gateway whose packets are dropped delays it by at most five seconds, with ' + spans + ' span(s) buffered', { timeout: 60000, skip: LINUX }, async () => {
      const gateway = await blackHole();
      try {
        // A connect that nothing answers waited for the kernel: 135 s for each try, 409 s for one span, 675 s for 1,200.
        assertEnded(await exitOf(gateway.url, spans), 5, 'black hole, ' + spans + ' spans');
      } finally {
        await gateway.close();
      }
    });
  }

  test('a gateway that refuses delays it by at most a second', { timeout: 60000 }, async () => {
    assertEnded(await exitOf('http://127.0.0.1:9', 1200), 1, 'refused, 1200 spans');
  });

  test('a healthy gateway costs it no time and receives every span', { timeout: 60000 }, async () => {
    const sink = await new Sink().listen();
    try {
      assertEnded(await exitOf(sink.url, 1200), 1, 'healthy, 1200 spans');
      assert.equal(sink.spans().filter((span) => span.name === 'chat gpt-4o').length, 1200);
    } finally {
      await sink.close();
    }
  });
});

// 100 kB of prompt and answer per span, 300 spans a second, in a process whose heap may hold 128 MB: against a gateway that
// does not answer, the queue of 2,048 spans alone was 205 MB, and a 512 MiB pod aborted with "JavaScript heap out of memory"
// after 21 s at 100 spans a second.
const HEAP = ['--expose-gc', '--max-old-space-size=128'];

async function memoryOf(gateway) {
  const result = await runNode([APP, '30', '100000', 'memory', '6000'], { execArgv: HEAP, env: { IMPACT_OTLP_ENDPOINT: gateway }, cap: 60000 });
  assert.equal(result.code, 0, 'the process ended with ' + (result.signal || result.code) + ': ' + result.stderr.slice(-300));
  return JSON.parse(result.stdout.trim().split('\n').pop());
}

describe('the memory of a process while the gateway does not answer', { concurrency: 2 }, () => {
  test('a gateway that hangs does not make the process grow beyond what it queues', { timeout: 90000 }, async () => {
    const gateway = await hangingServer();
    try {
      const { growthMb, spans } = await memoryOf(gateway.url);
      assert.ok(spans >= 600, 'only ' + spans + ' spans were offered');
      assert.ok(growthMb < 64, 'the process grew by ' + growthMb + ' MB while the gateway hung (the queue is bounded at 16 MiB, a batch at 4 MiB)');
    } finally {
      await gateway.close();
    }
  });

  test('a gateway whose packets are dropped does not make the process grow beyond what it queues', { timeout: 90000, skip: LINUX }, async () => {
    const gateway = await blackHole();
    try {
      const { growthMb } = await memoryOf(gateway.url);
      assert.ok(growthMb < 64, 'the process grew by ' + growthMb + ' MB while the packets were dropped');
    } finally {
      await gateway.close();
    }
  });
});

// The kit warns once a minute that it could not export. A stderr whose reader is gone made that write an uncaught EPIPE
// and the application exited with 1 (without the preload: 0).
test('a closed stderr does not crash the application (EPIPE)', { timeout: 60000 }, async () => {
  const result = await runNode([APP, '20', '200'], {
    env: { IMPACT_OTLP_ENDPOINT: 'http://127.0.0.1:9' },
    setup: (child) => child.stderr.destroy(),
  });
  assertEnded(result, 5, 'broken stderr pipe');
});

test('a stderr that the application closed does not crash it either (EBADF)', { timeout: 60000 }, async () => {
  const result = await runNode([APP, '20', '200', 'badfd'], { env: { IMPACT_OTLP_ENDPOINT: 'http://127.0.0.1:9' } });
  assertEnded(result, 5, 'closed descriptor');
});
