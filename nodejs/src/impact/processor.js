// SPDX-License-Identifier: Apache-2.0
'use strict';

// The package's batch span processor: the SDK's behaviour, with a queue bounded by bytes, one export at a time, and an exit
// that does not wait.
//
// The SDK's BatchSpanProcessor bounds its queue by the number of spans (2,048). With the kit's defaults a span carries the
// prompt and the answer, easily 100 kB, so a gateway that does not answer (a NetworkPolicy that drops the packets, a wedged
// pod) let every covered process grow by hundreds of megabytes: a pod with a 512 MiB limit aborted with "JavaScript heap out
// of memory" after 21 seconds. Its export timeout (30 s) also lets the next batch start while the last one is still waiting,
// so requests piled up in the exporter's agent with their spans and their compressed bodies. And a process that ends waits
// for every one of those requests and for their retries: 19 to 49 seconds against a gateway that hangs, 7 to 11 minutes
// when the packets are dropped.
//
// This processor takes the same settings (OTEL_BSP_*), batches in the order the spans ended, a timer that does not keep the
// process alive, and the same flush when the event loop is empty (see flushOnExit). It differs in that:
//
//   * the queue holds at most MAX_QUEUE_BYTES. When that, or the count of spans, is passed, the oldest spans are dropped (the
//     SDK drops the newest when its count is passed). A span larger than the whole bound is dropped itself;
//   * a batch is at most MAX_BATCH_BYTES (and at least one span), so that a request stays small and encoding it, which takes
//     several times its size for a moment, does not triple the memory of a full queue. A batch that is full by bytes is sent
//     at once, as one that is full by count is: large spans do not wait for the schedule, or a busy workload would lose most
//     of them to the bound;
//   * one batch is exported at a time. An export that does not come back in OTEL_BSP_EXPORT_TIMEOUT is aborted, not left
//     running, and after one that failed the next batch waits for the schedule: a gateway that does not answer is not asked
//     again for every batch that is full;
//   * the exit sends what the gateway takes, stops at the first failure, and waits at most EXIT_FLUSH_MS: see flushAtExit().
//
// Nothing here needs the OpenTelemetry packages, so the same processor serves any payload tree.

const { writeWarning } = require('./warn');

const MAX_QUEUE_BYTES = 16 * 1024 * 1024;
const MAX_BATCH_BYTES = 4 * 1024 * 1024;
// The longest an exit waits for the spans left in the queue. A gateway that does not answer ends the wait much sooner: an
// attempt takes two seconds and a failure ends the flush. This is for one that answers, slowly, batch after batch.
const EXIT_FLUSH_MS = 4000;
const WARN_EVERY_MS = 60000;
const SAMPLED = 1;
const SUCCESS = 0;
const FAILED = 1;

// What a span costs in memory beyond its text: the objects of a span, of an attribute (its slot and, for text, the header of
// the string), of an event and of a link. Measured on spans of 10 and 25 attributes (2.5 and 4.4 kB) in Node 22; the text,
// which is what grows, is counted by the bytes it takes.
const SPAN_OVERHEAD = 2048;
const ATTRIBUTE_OVERHEAD = 64;
const STRING_HEADER = 16;
const EVENT_OVERHEAD = 256;
const NUMBER = 16;

function positive(env, name, fallback) {
  const parsed = Number(env[name]);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

/**
 * The SDK's settings and defaults (OTEL_BSP_*); its defaults for a value that makes no sense. A batch cannot be larger than
 * the queue (the SDK warns and shrinks it; so does this).
 */
function settings(env) {
  const maxQueueSize = Math.floor(positive(env, 'OTEL_BSP_MAX_QUEUE_SIZE', 2048));
  return {
    maxQueueSize,
    scheduledDelayMillis: positive(env, 'OTEL_BSP_SCHEDULE_DELAY', 5000),
    maxExportBatchSize: Math.min(Math.floor(positive(env, 'OTEL_BSP_MAX_EXPORT_BATCH_SIZE', 512)), maxQueueSize),
    exportTimeoutMillis: positive(env, 'OTEL_BSP_EXPORT_TIMEOUT', 30000),
  };
}

function valueBytes(value) {
  if (typeof value === 'string') return STRING_HEADER + Buffer.byteLength(value, 'utf8');
  if (Array.isArray(value)) {
    let total = STRING_HEADER + 8 * value.length;
    for (const item of value) total += valueBytes(item);
    return total;
  }
  return NUMBER;
}

function attributesBytes(attributes) {
  if (!attributes) return 0;
  let total = 0;
  for (const key of Object.keys(attributes)) total += ATTRIBUTE_OVERHEAD + valueBytes(attributes[key]);
  return total;
}

/** About what a finished span holds in memory: its text and its objects. Cheap enough for span.end(). */
function spanBytes(span) {
  const text = (value) => Buffer.byteLength(String(value || ''), 'utf8');
  let total = SPAN_OVERHEAD + text(span.name) + attributesBytes(span.attributes);
  for (const event of span.events || []) total += EVENT_OVERHEAD + text(event.name) + attributesBytes(event.attributes);
  for (const link of span.links || []) total += EVENT_OVERHEAD + attributesBytes(link.attributes);
  return total;
}

function unref(timer) {
  if (timer && typeof timer.unref === 'function') timer.unref();
}

class BoundedBatchSpanProcessor {
  constructor(exporter, options) {
    const config = options || {};
    const base = settings(config.env || process.env);
    this.exporter = exporter;
    this.maxQueueSize = config.maxQueueSize || base.maxQueueSize;
    this.maxExportBatchSize = Math.min(config.maxExportBatchSize || base.maxExportBatchSize, this.maxQueueSize);
    this.scheduledDelayMillis =
      config.scheduledDelayMillis === undefined ? base.scheduledDelayMillis : config.scheduledDelayMillis;
    this.exportTimeoutMillis = config.exportTimeoutMillis || base.exportTimeoutMillis;
    this.maxQueueBytes = config.maxQueueBytes || MAX_QUEUE_BYTES;
    this.maxExportBatchBytes = config.maxExportBatchBytes || MAX_BATCH_BYTES;
    this.exitFlushMillis = config.exitFlushMillis || EXIT_FLUSH_MS;
    this.suppress = config.suppress || ((run) => run());
    this.warn = config.warn || writeWarning;

    // The queue: the oldest span is at `head`. Spans leave from there, so nothing is shifted; the arrays are cut when the
    // head has gone far.
    this.spans = [];
    this.sizes = [];
    this.head = 0;
    this.bytes = 0;

    this.timer = undefined; // the schedule
    this.immediate = undefined; // an export asked for by a full batch, started outside of span.end()
    this.chain = Promise.resolve(); // the exports, one after the other
    this.pending = 0; // the exports asked for and not finished
    this.closed = false; // shutdown() was called
    this.exiting = false; // the process is ending: see flushAtExit()
    this.abandoned = false; // ... and it has stopped waiting for us
    this.lastFailed = false;
    this.dropped = 0; // the spans dropped since the last warning
    this.lastWarning = -Infinity;
    this.exitFlush = null;
    this.shutdownDone = null;
  }

  get queuedSpans() {
    return this.spans.length - this.head;
  }

  get queuedBytes() {
    return this.bytes;
  }

  // --- the application's side: span.end() ---------------------------------------------------------------------------------

  onStart() {}

  onEnd(span) {
    try {
      if (this.closed) return;
      if ((span.spanContext().traceFlags & SAMPLED) === 0) return;
      this.add(span, spanBytes(span));
    } catch (error) {
      // called by span.end() in the application's code: nothing may reach it
    }
  }

  add(span, size) {
    let dropped = 0;
    if (size > this.maxQueueBytes) {
      dropped = 1; // it would push out every other span and not fit itself
    } else {
      this.spans.push(span);
      this.sizes.push(size);
      this.bytes += size;
      while (this.bytes > this.maxQueueBytes || this.queuedSpans > this.maxQueueSize) {
        this.bytes -= this.sizes[this.head];
        this.spans[this.head] = undefined;
        this.head += 1;
        dropped += 1;
      }
      this.cut();
    }
    if (dropped) this.warnDropped(dropped);
    this.kick();
  }

  cut() {
    if (this.head >= 1024 && this.head * 2 >= this.spans.length) {
      this.spans = this.spans.slice(this.head);
      this.sizes = this.sizes.slice(this.head);
      this.head = 0;
    }
  }

  warnDropped(count) {
    this.dropped += count;
    const now = Date.now();
    if (now - this.lastWarning < WARN_EVERY_MS) return;
    this.lastWarning = now;
    const total = this.dropped;
    this.dropped = 0;
    try {
      this.warn(
        'the span queue is full (' + Math.round(this.maxQueueBytes / 1048576) + ' MiB): dropped ' + total +
          ' of the oldest spans, the gateway is too slow or unreachable'
      );
    } catch (error) {
      // a warning is not worth the application's time
    }
  }

  // --- what is sent, and when ---------------------------------------------------------------------------------------------

  full() {
    return this.queuedSpans >= this.maxExportBatchSize || this.bytes >= this.maxExportBatchBytes;
  }

  /**
   * Make sure that what is queued will be sent: now if a batch is full, otherwise on the schedule. After an export that
   * failed, on the schedule in any case: a gateway that does not answer is not asked again for every batch that is full,
   * and a process whose work is done is not held by one attempt after the other (a connect in progress holds it).
   */
  kick() {
    if (this.closed || this.exiting || this.pending > 0 || this.queuedSpans === 0) return;
    if (this.full() && !this.lastFailed) {
      if (this.immediate === undefined) {
        // outside of span.end(): the application's call must not pay for encoding a batch. It runs within this turn of the
        // event loop, so it holds nothing open.
        this.immediate = setImmediate(() => {
          this.immediate = undefined;
          this.enqueue({ first: false }).catch(() => {});
        });
      }
    } else if (this.timer === undefined) {
      this.timer = setTimeout(() => {
        this.timer = undefined;
        this.enqueue({ first: true }).catch(() => {});
      }, this.scheduledDelayMillis);
      unref(this.timer);
    }
  }

  clearTimer() {
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  /** The oldest spans, up to a batch by count and by bytes (and at least one); none if the batch is not full and not forced. */
  take(force) {
    if (this.queuedSpans === 0 || this.abandoned) return [];
    if (!force && !this.full()) return [];
    const batch = [];
    let total = 0;
    while (this.queuedSpans > 0 && batch.length < this.maxExportBatchSize) {
      const size = this.sizes[this.head];
      if (batch.length > 0 && total + size > this.maxExportBatchBytes) break;
      batch.push(this.spans[this.head]);
      this.spans[this.head] = undefined;
      this.head += 1;
      this.bytes -= size;
      total += size;
    }
    this.cut();
    return batch;
  }

  discard() {
    this.spans = [];
    this.sizes = [];
    this.head = 0;
    this.bytes = 0;
  }

  abortExports() {
    if (typeof this.exporter.abort !== 'function') return;
    try {
      this.exporter.abort();
    } catch (error) {
      // the exit goes on
    }
  }

  /** One batch to the exporter, under suppressed tracing. Resolves when it has answered or has been given up on. */
  send(batch) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (code) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this.lastFailed = code !== SUCCESS;
        resolve();
      };
      const timer = setTimeout(() => {
        this.abortExports();
        finish(FAILED);
      }, this.exportTimeoutMillis);
      unref(timer);
      try {
        this.suppress(() => this.exporter.export(batch, (result) => finish(result && result.code)));
      } catch (error) {
        finish(FAILED);
      }
    });
  }

  /**
   * Send full batches: the first one whatever its size when `first` (the schedule has come), every one when `everything`.
   *
   * Once the process is exiting, the gateway failing is the end of it: what is left is dropped, not tried.
   */
  async drain(mode) {
    this.clearTimer();
    let force = Boolean(mode.first || mode.everything);
    for (;;) {
      if (this.exiting && this.lastFailed) {
        this.discard();
        return;
      }
      const batch = this.take(force);
      if (batch.length === 0) return;
      await this.send(batch);
      if (this.lastFailed && !mode.everything) return; // the rest waits for the schedule
      force = Boolean(mode.everything);
    }
  }

  /** Queue an export behind the ones before it. */
  enqueue(mode) {
    this.pending += 1;
    const release = () => {
      this.pending -= 1;
      if (this.pending === 0) this.kick();
    };
    const job = this.chain.then(() => this.drain(mode));
    this.chain = job.then(release, release);
    return job;
  }

  forceFlush() {
    if (this.shutdownDone) return this.shutdownDone;
    return this.enqueue({ everything: true });
  }

  /**
   * The process is exiting (the event loop is empty). The exporter is told at once, so that the batch it is sending ends with
   * its attempt instead of trying again. Then what is left is sent, until the first batch that fails; if the last export
   * before the exit had failed, nothing is sent (the gateway is not answering, and asking again costs the exit its seconds).
   * The wait is at most EXIT_FLUSH_MS, and what is still running then is aborted.
   *
   * The timer of the wait is what holds the process open meanwhile: the requests of the exporter do not, so that an
   * application that is done is not kept by a gateway that hangs for longer than this.
   *
   * The exit mode lasts as long as the flush. A process that goes on afterwards (a `beforeExit` listener started more work)
   * is served as before, and a flush with nothing to wait for makes no work, or `beforeExit` would repeat for ever.
   */
  flushAtExit() {
    if (this.exitFlush) return this.exitFlush;
    if (this.queuedSpans === 0 && this.pending === 0) return Promise.resolve();
    this.exiting = true;
    this.clearTimer();
    this.tellExporter('beginShutdown');
    const drained = this.enqueue({ everything: true }).catch(() => {});
    const finished = new Promise((resolve) => {
      const cap = setTimeout(() => {
        this.abandoned = true;
        this.abortExports();
        resolve();
      }, this.exitFlushMillis);
      drained.then(() => {
        clearTimeout(cap);
        resolve();
      });
    });
    this.exitFlush = finished.then(() => {
      this.exitFlush = null;
      if (this.closed) return;
      this.exiting = false;
      this.abandoned = false;
      this.tellExporter('endShutdown');
      this.kick();
    });
    return this.exitFlush;
  }

  tellExporter(method) {
    if (typeof this.exporter[method] !== 'function') return;
    try {
      this.exporter[method]();
    } catch (error) {
      // the exit goes on
    }
  }

  shutdown() {
    if (this.shutdownDone) return this.shutdownDone;
    this.closed = true;
    this.clearTimer();
    this.shutdownDone = this.flushAtExit()
      .then(() => this.exporter.shutdown())
      .catch(() => {});
    return this.shutdownDone;
  }
}

/**
 * Flush when the event loop is empty (`beforeExit`): a Job, a CronJob or a service that closed its server and let the loop
 * drain. What it waits for is bounded by flushAtExit(). A process that ends with process.exit() or a signal never gets here,
 * as before.
 */
function flushOnExit(processor, target) {
  const emitter = target || process;
  emitter.on('beforeExit', () => {
    processor.flushAtExit().catch(() => {});
  });
}

module.exports = {
  BoundedBatchSpanProcessor,
  flushOnExit,
  settings,
  spanBytes,
  MAX_QUEUE_BYTES,
  MAX_BATCH_BYTES,
  EXIT_FLUSH_MS,
};
