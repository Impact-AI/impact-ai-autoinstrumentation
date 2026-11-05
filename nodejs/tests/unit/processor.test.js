// SPDX-License-Identifier: Apache-2.0
'use strict';

// The package's batch span processor: a queue bounded by bytes as well as by count, one export at a time, and an exit that
// does not wait for a gateway that does not answer.

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('events');
const { TracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const processors = require('../../src/impact/processor');
const { until } = require('./helpers');

const { BoundedBatchSpanProcessor, spanBytes } = processors;
const SUCCESS = 0;
const FAILED = 1;

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/** Notes every batch it is given. It can wait for the test (a gateway that hangs) or fail (one that refuses). */
class Recorder {
  constructor(options) {
    const settings = options || {};
    this.batches = [];
    this.result = settings.result === undefined ? SUCCESS : settings.result;
    this.hold = settings.hold || null;
    this.first = deferred();
    this.exiting = false;
    this.exitsBegun = 0;
    this.stopped = false;
    this.aborts = 0;
    this.running = 0;
    this.mostRunning = 0;
  }

  get names() {
    return this.batches.flat();
  }

  export(spans, callback) {
    this.batches.push(spans.map((span) => span.name));
    this.first.resolve();
    this.running += 1;
    this.mostRunning = Math.max(this.mostRunning, this.running);
    const finish = () => {
      this.running -= 1;
      callback({ code: typeof this.result === 'function' ? this.result(this.batches.length) : this.result });
    };
    if (this.hold) this.hold.promise.then(finish);
    else setTimeout(finish, 2);
  }

  beginShutdown() {
    this.exiting = true;
    this.exitsBegun += 1;
  }

  endShutdown() {
    this.exiting = false;
  }

  abort() {
    this.aborts += 1;
  }

  shutdown() {
    this.stopped = true;
    return Promise.resolve();
  }
}

/** A finished span of the shape the SDK hands to a processor, as far as the processor looks at it. */
function span(name, size, flags) {
  return {
    name,
    attributes: { content: 'x'.repeat(size === undefined ? 100 : size) },
    events: [],
    links: [],
    spanContext: () => ({ traceFlags: flags === undefined ? 1 : flags }),
  };
}

const range = (from, to) => Array.from({ length: to - from }, (_, index) => from + index);
// Names of one length, so that every span is counted the same.
const numbered = (numbers) => numbers.map((number) => 's' + String(number).padStart(3, '0'));
const one = spanBytes(span('s000'));

function end(processor, names, size) {
  for (const name of names) processor.onEnd(span(name, size));
}

function made(recorder, options) {
  // The schedule never comes (a minute) unless a test asks for it.
  return new BoundedBatchSpanProcessor(recorder, Object.assign({ scheduledDelayMillis: 60000 }, options || {}));
}

// --- the byte bound ----------------------------------------------------------------------------------------------------

test('the queue never holds more than the byte cap, and drops the oldest spans first', { timeout: 15000 }, async () => {
  const hold = deferred();
  const recorder = new Recorder({ hold });
  const processor = made(recorder, { maxQueueBytes: 20 * one, maxExportBatchSize: 5 });
  end(processor, numbered(range(0, 5))); // a full batch: it is taken at once, and the gateway never answers
  await recorder.first.promise;
  for (const name of numbered(range(5, 65))) {
    end(processor, [name]);
    assert.ok(processor.queuedBytes <= 20 * one);
  }
  assert.equal(processor.queuedSpans, 20);
  hold.resolve();
  await until(() => recorder.names.length === 25);
  // The first batch was on its way; of the 60 that queued up behind it only the newest 20 are left.
  assert.deepEqual(recorder.names, numbered([...range(0, 5), ...range(45, 65)]));
  await processor.shutdown();
});

test('the count bound of the SDK still applies, and also drops the oldest', { timeout: 15000 }, async () => {
  const hold = deferred();
  const recorder = new Recorder({ hold });
  const processor = made(recorder, { maxQueueSize: 10, maxExportBatchSize: 5 });
  end(processor, numbered(range(0, 5)));
  await recorder.first.promise;
  end(processor, numbered(range(5, 40)));
  assert.equal(processor.queuedSpans, 10);
  hold.resolve();
  await until(() => recorder.names.length === 15);
  assert.deepEqual(recorder.names, numbered([...range(0, 5), ...range(30, 40)]));
  await processor.shutdown();
});

test('a span that alone exceeds the cap is dropped, and the others stay', async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { maxQueueBytes: 3 * one, maxExportBatchSize: 100 });
  end(processor, numbered([0, 1]));
  end(processor, ['huge'], 4 * one); // more than the whole cap
  end(processor, numbered([2]));
  assert.equal(processor.queuedSpans, 3);
  await processor.forceFlush();
  assert.deepEqual(recorder.names, numbered([0, 1, 2]));
  await processor.shutdown();
});

test('a batch that is full by bytes is sent without waiting for the schedule', { timeout: 15000 }, async () => {
  // Spans of 100 kB fill the byte cap long before the count of a batch (512): they must not wait for the schedule while the
  // gateway is healthy, or a busy workload would lose most of them to the cap.
  const recorder = new Recorder();
  const processor = made(recorder, { maxExportBatchBytes: 3 * one, maxQueueBytes: 20 * one });
  end(processor, numbered(range(0, 3)));
  await recorder.first.promise;
  await until(() => recorder.names.length === 3);
  assert.equal(recorder.batches.length, 1);
  await processor.shutdown();
});

test('a batch holds at most the batch bytes, but at least one span', async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { maxExportBatchBytes: 2 * one, maxQueueBytes: 50 * one });
  end(processor, numbered(range(0, 5)));
  end(processor, ['huge'], 3 * one); // bigger than a batch: it goes alone
  end(processor, numbered([5]));
  await processor.shutdown();
  assert.deepEqual(recorder.names, [...numbered(range(0, 5)), 'huge', ...numbered([5])]);
  assert.deepEqual(recorder.batches.find((batch) => batch.includes('huge')), ['huge']);
  assert.ok(recorder.batches.every((batch) => batch.length <= 2));
});

test('spans are sent on the schedule', async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { scheduledDelayMillis: 50 });
  end(processor, numbered([0, 1]));
  await until(() => recorder.names.length === 2);
  assert.deepEqual(recorder.names, numbered([0, 1]));
  await processor.shutdown();
});

test('only one export is running at a time, however the exports are asked for', { timeout: 15000 }, async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { maxExportBatchSize: 20, scheduledDelayMillis: 5 });
  end(processor, numbered(range(0, 400)));
  const flushes = [processor.forceFlush(), processor.forceFlush()];
  end(processor, numbered(range(400, 600)));
  await Promise.all(flushes);
  await processor.shutdown();
  assert.equal(recorder.mostRunning, 1);
  assert.equal(recorder.names.length, 600);
});

test('after a failed export the next batch waits for the schedule, and when the gateway answers again the rest goes at once', { timeout: 15000 }, async () => {
  // A gateway that does not answer is not asked again for every batch that is full: one attempt after the other would hold a
  // process whose work is done (a connect in progress holds it), and hammer the gateway that is down.
  const recorder = new Recorder({ result: FAILED });
  const processor = made(recorder, { maxExportBatchSize: 5, scheduledDelayMillis: 300 });
  end(processor, numbered(range(0, 20)));
  await until(() => recorder.batches.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(recorder.batches.length, 1, 'the next batch was sent at once, after a failure');
  recorder.result = SUCCESS;
  await until(() => recorder.names.length === 20, 5000);
  assert.deepEqual(recorder.names, numbered(range(0, 20)));
  await processor.shutdown();
});

test('spans are sent in the order they ended', async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { maxExportBatchSize: 7 });
  end(processor, numbered(range(0, 50)));
  await processor.shutdown();
  assert.deepEqual(recorder.names, numbered(range(0, 50)));
});

// --- the size of a span ------------------------------------------------------------------------------------------------

test('the size of a span follows its content', () => {
  const big = spanBytes(span('s', 100000));
  assert.ok(big >= 100000 && big <= 110000, String(big));
  assert.ok(spanBytes(span('s', 10)) < 5000);
});

test('text is counted by the bytes it takes, events and links included', () => {
  const ascii = spanBytes(span('s', 50000));
  const cjk = { ...span('s', 0), attributes: { content: '漢'.repeat(50000) } };
  assert.ok(spanBytes(cjk) > 2.5 * ascii, 'three bytes a character');
  const withEvent = { ...span('s', 10), events: [{ name: 'e', attributes: { detail: 'x'.repeat(20000) } }] };
  const withLink = { ...span('s', 10), links: [{ attributes: { detail: 'x'.repeat(20000) } }] };
  assert.ok(spanBytes(withEvent) > 20000 && spanBytes(withLink) > 20000);
});

test('a span the processor cannot measure never reaches the application', async () => {
  const processor = made(new Recorder());
  assert.doesNotThrow(() => processor.onEnd(undefined));
  assert.doesNotThrow(() => processor.onEnd({}));
  assert.doesNotThrow(() => processor.onEnd({ spanContext: () => ({ traceFlags: 1 }), name: 'x', get attributes() { throw new Error('boom'); } }));
  assert.equal(processor.queuedSpans, 0);
  await processor.shutdown();
});

test('a span that is not sampled is not queued', async () => {
  const processor = made(new Recorder());
  processor.onEnd(span('recorded, not sampled', 10, 0));
  assert.equal(processor.queuedSpans, 0);
  await processor.shutdown();
});

// --- the exit ----------------------------------------------------------------------------------------------------------

test('the exit sends what is left to a healthy gateway', async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { maxExportBatchSize: 5 });
  end(processor, numbered(range(0, 12)));
  await processor.flushAtExit();
  assert.deepEqual(recorder.names, numbered(range(0, 12)));
  assert.equal(recorder.exitsBegun, 1);
  await processor.shutdown();
  assert.ok(recorder.stopped);
});

test('the exporter is told that the process is exiting while its export is still running', { timeout: 15000 }, async () => {
  // The batch in flight is the last attempt: told at once, the exporter does not try it again.
  const hold = deferred();
  const recorder = new Recorder({ hold });
  const processor = made(recorder, { maxExportBatchSize: 5 });
  end(processor, numbered(range(0, 5)));
  await recorder.first.promise;
  const exiting = processor.flushAtExit();
  assert.ok(recorder.exiting, 'the exporter was not told about the exit while its export was running');
  hold.resolve();
  await exiting;
});

test('a failed export at exit is the last, and the rest is dropped', { timeout: 15000 }, async () => {
  const hold = deferred();
  const recorder = new Recorder({ result: FAILED, hold });
  const processor = made(recorder, { maxExportBatchSize: 5 });
  end(processor, numbered(range(0, 5)));
  await recorder.first.promise;
  end(processor, numbered(range(5, 30))); // five more batches wait behind the one that hangs
  const exiting = processor.flushAtExit();
  hold.resolve();
  await exiting;
  assert.equal(recorder.batches.length, 1, 'the exit went on to more batches after a failure');
  assert.equal(processor.queuedSpans, 0);
});

test('when the last export failed, the exit sends nothing', async () => {
  const recorder = new Recorder({ result: FAILED });
  const processor = made(recorder, { maxExportBatchSize: 3 });
  end(processor, numbered(range(0, 3)));
  await until(() => recorder.batches.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 20)); // the processor has seen the failure
  end(processor, numbered([3, 4])); // buffered, not yet a batch
  await processor.flushAtExit();
  assert.deepEqual(recorder.names, numbered(range(0, 3)));
});

test('the exit waits for a stuck exporter only up to the exit limit, and ends what it was doing', { timeout: 15000 }, async () => {
  const hold = deferred();
  const recorder = new Recorder({ hold });
  const processor = made(recorder, { maxExportBatchSize: 5, exitFlushMillis: 200 });
  end(processor, numbered(range(0, 5)));
  await recorder.first.promise;
  end(processor, numbered(range(5, 20)));
  const started = Date.now();
  await processor.flushAtExit();
  assert.ok(Date.now() - started < 3000);
  assert.ok(recorder.aborts >= 1, 'the export that did not end was not aborted');
  hold.resolve();
});

test('the exit has nothing to wait for when nothing is queued', async () => {
  const recorder = new Recorder();
  const processor = made(recorder);
  const started = Date.now();
  await processor.flushAtExit();
  assert.ok(Date.now() - started < 100);
  assert.deepEqual(recorder.batches, []);
});

test('spans that end after the shutdown are ignored, and shutting down twice is harmless', async () => {
  const recorder = new Recorder();
  const processor = made(recorder);
  end(processor, ['a']);
  await processor.shutdown();
  await processor.shutdown();
  end(processor, ['late']);
  assert.equal(processor.queuedSpans, 0);
  assert.deepEqual(recorder.names, ['a']);
});

test('flushOnExit sends what is queued when the event loop is empty, once', async () => {
  const recorder = new Recorder();
  const processor = made(recorder);
  const fake = new EventEmitter();
  processors.flushOnExit(processor, fake);
  end(processor, ['a', 'b']);
  fake.emit('beforeExit');
  fake.emit('beforeExit');
  await until(() => recorder.names.length === 2);
  assert.deepEqual(recorder.batches, [['a', 'b']]);
  assert.equal(recorder.exitsBegun, 1);
});

test('a process that goes on after the exit flush is served as before', { timeout: 15000 }, async () => {
  const recorder = new Recorder();
  const processor = made(recorder, { scheduledDelayMillis: 50 });
  end(processor, ['a']);
  await processor.flushAtExit();
  assert.deepEqual(recorder.names, ['a']);
  assert.equal(recorder.exiting, false, 'the exporter was left in its exit mode');
  end(processor, ['b']);
  await until(() => recorder.names.length === 2);
  assert.deepEqual(recorder.names, ['a', 'b']);
  await processor.shutdown();
});

test('an exit flush with nothing to wait for makes no work, so that beforeExit does not repeat for ever', async () => {
  const processor = made(new Recorder());
  const timers = () => process.getActiveResourcesInfo().filter((name) => name === 'Timeout').length;
  const before = timers();
  await processor.flushAtExit();
  assert.equal(timers(), before);
});

test('what the exit waits for is a timer of its own, so that the requests of the exporter need not keep the process open', async () => {
  const hold = deferred();
  const processor = made(new Recorder({ hold }), { exitFlushMillis: 60000 });
  end(processor, ['a']);
  const before = process.getActiveResourcesInfo().filter((name) => name === 'Timeout').length;
  const exiting = processor.flushAtExit();
  const during = process.getActiveResourcesInfo().filter((name) => name === 'Timeout').length;
  assert.ok(during > before, 'nothing holds the process while the exit flush runs');
  hold.resolve();
  await exiting;
  assert.ok(process.getActiveResourcesInfo().filter((name) => name === 'Timeout').length <= before, 'the timer of the wait was left running');
});

test('the requests of the exporter are made not to hold the process open: the exit flush is what does', async () => {
  const { ImpactSpanExporter } = require('../../src/impact/exporter');
  const made = [];
  const exporter = new ImpactSpanExporter({
    env: { IMPACT_OTLP_ENDPOINT: 'http://gateway:4318' },
    post: async (url, payload, options) => {
      made.push(options);
      return { status: 200, retryAfter: null };
    },
  });
  const memory = new InMemorySpanExporter();
  const provider = new TracerProvider({ resource: resourceFromAttributes({ 'service.name': 'x' }), spanProcessors: [new SimpleSpanProcessor({ exporter: memory })] });
  provider.getTracer('t').startSpan('a').end();
  await new Promise((resolve) => exporter.export(memory.getFinishedSpans(), resolve));
  assert.equal(made.length, 1);
  assert.equal(made[0].unref, true);
  assert.ok(made[0].signal instanceof AbortSignal, 'the attempt cannot be ended from outside');
  await exporter.shutdown();
});

// --- never into the application ----------------------------------------------------------------------------------------

test('an exporter that throws does not end the processor', async () => {
  let calls = 0;
  const exporter = {
    export() {
      calls += 1;
      throw new Error('boom');
    },
  };
  const processor = made(exporter, { maxExportBatchSize: 2 });
  end(processor, numbered(range(0, 6)));
  await processor.forceFlush();
  assert.equal(calls, 3);
  await processor.shutdown();
});

test('an export that never calls back is given up on after the export timeout, and aborted', { timeout: 15000 }, async () => {
  const hold = deferred();
  const recorder = new Recorder({ hold });
  const processor = made(recorder, { maxExportBatchSize: 2, exportTimeoutMillis: 100, scheduledDelayMillis: 50 });
  end(processor, numbered(range(0, 4)));
  await until(() => recorder.batches.length === 2, 5000);
  assert.ok(recorder.aborts >= 1);
  hold.resolve();
  await processor.shutdown();
});

test('exports run with tracing suppressed, as the SDK runs its own', async () => {
  let inside = 0;
  const seen = [];
  const recorder = new Recorder();
  const original = recorder.export.bind(recorder);
  recorder.export = (spans, callback) => {
    seen.push(inside);
    original(spans, callback);
  };
  const processor = made(recorder, {
    suppress: (run) => {
      inside += 1;
      try {
        return run();
      } finally {
        inside -= 1;
      }
    },
  });
  end(processor, ['a']);
  await processor.forceFlush();
  assert.deepEqual(seen, [1]);
  await processor.shutdown();
});

test('a warning is given once a minute when spans are dropped', async () => {
  const warnings = [];
  const processor = made(new Recorder({ hold: deferred() }), { maxQueueSize: 4, maxExportBatchSize: 2, warn: (message) => warnings.push(message) });
  end(processor, numbered(range(0, 50)));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /the span queue is full \(16 MiB\): dropped \d+ of the oldest spans/);
});

test('forceFlush sends what is queued', async () => {
  const recorder = new Recorder();
  const processor = made(recorder);
  end(processor, ['a', 'b']);
  await processor.forceFlush();
  assert.deepEqual(recorder.names, ['a', 'b']);
  assert.equal(processor.queuedSpans, 0);
  await processor.shutdown();
});

// --- the settings of the SDK -------------------------------------------------------------------------------------------

test('the settings of the SDK are honoured', () => {
  assert.deepEqual(processors.settings({}), { maxQueueSize: 2048, scheduledDelayMillis: 5000, maxExportBatchSize: 512, exportTimeoutMillis: 30000 });
  assert.deepEqual(
    processors.settings({ OTEL_BSP_MAX_QUEUE_SIZE: '100', OTEL_BSP_SCHEDULE_DELAY: '250', OTEL_BSP_MAX_EXPORT_BATCH_SIZE: '10', OTEL_BSP_EXPORT_TIMEOUT: '2000' }),
    { maxQueueSize: 100, scheduledDelayMillis: 250, maxExportBatchSize: 10, exportTimeoutMillis: 2000 }
  );
});

test('settings that make no sense never stop the package', () => {
  const defaults = { maxQueueSize: 2048, scheduledDelayMillis: 5000, maxExportBatchSize: 512, exportTimeoutMillis: 30000 };
  assert.deepEqual(processors.settings({ OTEL_BSP_MAX_QUEUE_SIZE: 'lots', OTEL_BSP_SCHEDULE_DELAY: '-1', OTEL_BSP_MAX_EXPORT_BATCH_SIZE: '0' }), defaults);
  // A batch cannot be larger than the queue: the SDK warns and shrinks it; so does the package.
  assert.deepEqual(processors.settings({ OTEL_BSP_MAX_QUEUE_SIZE: '100' }), { ...defaults, maxQueueSize: 100, maxExportBatchSize: 100 });
});

// --- behind the SDK's tracer provider ----------------------------------------------------------------------------------

test('behind the SDK provider: spans arrive in order on a flush, and a shutdown flushes and stops the exporter', async () => {
  const recorder = new Recorder();
  const processor = made(recorder);
  const provider = new TracerProvider({ resource: resourceFromAttributes({ 'service.name': 'x' }), spanProcessors: [processor] });
  const tracer = provider.getTracer('t');
  for (const name of ['a', 'b', 'c']) tracer.startSpan(name).end();
  await provider.forceFlush();
  assert.deepEqual(recorder.names, ['a', 'b', 'c']);
  tracer.startSpan('d').end();
  await provider.shutdown();
  assert.deepEqual(recorder.names, ['a', 'b', 'c', 'd']);
  assert.ok(recorder.stopped);
});
