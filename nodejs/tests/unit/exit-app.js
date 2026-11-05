// SPDX-License-Identifier: Apache-2.0
'use strict';

// A covered process for the exit-time tests (it is not a test): the package's exporter and batch processor behind the SDK's
// tracer provider, wired as runtime.js wires them, a number of finished spans, and nothing else to do. It prints
// "WORK_DONE" when the spans are made and lets the event loop drain, which is how a Job, a CronJob or a rollout ends it.
//
//   node exit-app.js SPANS [CHARACTERS_PER_SPAN [MODE]]       env IMPACT_OTLP_ENDPOINT = the gateway
//
// MODE: natural (default: the loop drains) | badfd (as natural, with file descriptor 2 closed first) | memory (SPANS spans
// every 100 ms for the milliseconds in argument 5, the most the heap held beyond what it held at the start, no exit flush).

const crypto = require('crypto');
const { TracerProvider } = require('@opentelemetry/sdk-trace');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { ImpactSpanExporter } = require('../../src/impact/exporter');
const { BoundedBatchSpanProcessor, flushOnExit } = require('../../src/impact/processor');

if (process.argv[4] === 'badfd') require('fs').closeSync(2);

const spans = Number(process.argv[2] || 1);
const characters = Number(process.argv[3] || 200);
const mode = process.argv[4] || 'natural';

const exporter = new ImpactSpanExporter({ env: process.env });
const processor = new BoundedBatchSpanProcessor(exporter, { env: process.env });
const provider = new TracerProvider({ resource: resourceFromAttributes({ 'service.name': 'exit-app' }), spanProcessors: [processor] });
if (mode !== 'memory') flushOnExit(processor);

const tracer = provider.getTracer('chat');
const content = () => crypto.randomBytes(Math.ceil(characters / 2)).toString('hex').slice(0, characters);

function finishSpan() {
  const span = tracer.startSpan('chat gpt-4o');
  span.setAttribute('gen_ai.input.messages', content());
  span.end();
}

if (mode === 'memory') {
  // 100 kB of prompt and answer per span, offered faster than any gateway that hangs takes them: what matters is what the
  // process holds. A full collection first, so that what is counted is what is still referenced.
  const megabytes = (bytes) => Math.round((bytes / 1048576) * 10) / 10;
  const used = () => {
    global.gc();
    const memory = process.memoryUsage();
    return megabytes(memory.heapUsed + memory.external);
  };
  const before = used();
  let peak = 0;
  let made = 0;
  const started = Date.now();
  const timer = setInterval(() => {
    for (let i = 0; i < spans; i += 1) finishSpan();
    made += spans;
    peak = Math.max(peak, used() - before);
    if (Date.now() - started >= Number(process.argv[5] || 5000)) {
      clearInterval(timer);
      process.stdout.write(JSON.stringify({ growthMb: peak, spans: made }) + '\n');
      process.exit(0);
    }
  }, 100);
} else {
  for (let i = 0; i < spans; i += 1) finishSpan();
  process.stdout.write('WORK_DONE\n');
}
