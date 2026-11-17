// SPDX-License-Identifier: Apache-2.0
'use strict';

// Who, which session and which use case: the business context the application puts in OpenTelemetry baggage. The package
// copies four keys of it onto every span it makes (user.id, session.id, gen_ai.conversation.id, impact.use_case), nothing
// else of the baggage, at most 256 bytes a value, and never breaks the application.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const api = require('@opentelemetry/api');
const { TracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const businessContext = require('../../src/impact/business-context');

const { BusinessContext, bounded, KEYS, MAX_VALUE_BYTES } = businessContext;

function traced() {
  const exporter = new InMemorySpanExporter();
  const provider = new TracerProvider({
    resource: resourceFromAttributes({ 'service.name': 'returns' }),
    spanProcessors: [new BusinessContext(api), new SimpleSpanProcessor({ exporter })],
  });
  return { tracer: provider.getTracer('test'), exporter };
}

function withBaggage(entries) {
  const bag = {};
  for (const key of Object.keys(entries)) bag[key] = { value: entries[key] };
  return api.propagation.setBaggage(api.ROOT_CONTEXT, api.propagation.createBaggage(bag));
}

function spanUnder(entries, attributes) {
  const { tracer, exporter } = traced();
  tracer.startSpan('chat gpt-4o-mini', { attributes }, withBaggage(entries)).end();
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  return spans[0];
}

test('the keys are the ones the platform reads', () => {
  assert.deepEqual(KEYS, ['user.id', 'session.id', 'gen_ai.conversation.id', 'impact.use_case']);
  assert.equal(MAX_VALUE_BYTES, 256);
});

test('the four keys are copied from baggage onto the span', () => {
  const values = {
    'user.id': 'u-1842',
    'session.id': 's-77',
    'gen_ai.conversation.id': 'conv-9',
    'impact.use_case': 'returns assistant',
  };
  assert.deepEqual(spanUnder(values).attributes, values);
});

test('every span started in the context gets them, the child spans too', () => {
  const { tracer, exporter } = traced();
  const context = withBaggage({ 'user.id': 'u-1', 'impact.use_case': 'refunds' });
  const agent = tracer.startSpan('invoke_agent refunds', {}, context);
  const inside = api.trace.setSpan(context, agent);
  tracer.startSpan('chat gpt-4o-mini', {}, inside).end();
  tracer.startSpan('execute_tool lookup_order', {}, inside).end();
  agent.end();
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 3);
  for (const span of spans) {
    assert.equal(span.attributes['user.id'], 'u-1');
    assert.equal(span.attributes['impact.use_case'], 'refunds');
  }
});

test('no other baggage is copied', () => {
  const span = spanUnder({
    'user.id': 'u-1',
    'enduser.id': 'e-1', // deprecated in the semantic conventions: the application uses user.id
    'user.email': 'someone@example.com',
    tenant: 't-1',
    authorization: 'Bearer abc',
    'USER.ID': 'upper',
  });
  assert.deepEqual(span.attributes, { 'user.id': 'u-1' });
});

test('a long value is cut to 256 bytes', () => {
  assert.equal(spanUnder({ 'user.id': 'a'.repeat(1000) }).attributes['user.id'], 'a'.repeat(256));
});

test('a value is cut on a character boundary', () => {
  const cases = [
    ['é'.repeat(200), 'é'.repeat(128)], // 2 bytes each: exactly 256
    ['€'.repeat(100), '€'.repeat(85)], // 3 bytes each: 255, the 86th would straddle the cut
    ['a' + '😀'.repeat(70), 'a' + '😀'.repeat(63)], // 4 bytes each: 253
    ['ok', 'ok'],
    ['€'.repeat(85), '€'.repeat(85)], // 255 bytes: kept whole
  ];
  for (const [value, expected] of cases) {
    const cut = spanUnder({ 'session.id': value }).attributes['session.id'];
    assert.equal(cut, expected);
    assert.ok(Buffer.byteLength(cut, 'utf8') <= 256);
  }
});

test('empty and non-text values are dropped, and one bad value costs only itself', () => {
  for (const value of ['', '   ', 42, 1.5, true, null, undefined, ['a'], { a: 1 }]) {
    const span = spanUnder({ 'user.id': value, 'impact.use_case': 'refunds' });
    assert.equal('user.id' in span.attributes, false, String(value));
    assert.equal(span.attributes['impact.use_case'], 'refunds');
  }
  assert.equal(bounded(Symbol('x')), undefined);
});

test('without baggage nothing changes', () => {
  const { tracer, exporter } = traced();
  tracer.startSpan('chat gpt-4o-mini', { attributes: { 'gen_ai.request.model': 'gpt-4o-mini' } }, api.ROOT_CONTEXT).end();
  assert.deepEqual(exporter.getFinishedSpans()[0].attributes, { 'gen_ai.request.model': 'gpt-4o-mini' });
});

test('an attribute the span already has is kept', () => {
  const span = spanUnder(
    { 'gen_ai.conversation.id': 'from-baggage', 'user.id': 'u-1' },
    { 'gen_ai.conversation.id': 'from-the-instrumentation' },
  );
  assert.equal(span.attributes['gen_ai.conversation.id'], 'from-the-instrumentation');
  assert.equal(span.attributes['user.id'], 'u-1');
});

test('a failure never reaches the application', () => {
  const brokenApi = {
    propagation: {
      getBaggage() {
        throw new Error('baggage is broken');
      },
    },
  };
  const processor = new BusinessContext(brokenApi);
  assert.doesNotThrow(() => processor.onStart({ attributes: {}, setAttribute() {} }, api.ROOT_CONTEXT));
  const refusing = {
    attributes: {},
    setAttribute() {
      throw new Error('no');
    },
  };
  assert.doesNotThrow(() => new BusinessContext(api).onStart(refusing, withBaggage({ 'user.id': 'u-1' })));
  assert.doesNotThrow(() => new BusinessContext(undefined).onStart(refusing, withBaggage({ 'user.id': 'u-1' })));
  assert.doesNotThrow(() => new BusinessContext(api).onStart(undefined, undefined));
  const baggageThatThrows = {
    getEntry() {
      throw new Error('no');
    },
  };
  const odd = { propagation: { getBaggage: () => baggageThatThrows } };
  assert.doesNotThrow(() => new BusinessContext(odd).onStart({ attributes: {}, setAttribute() {} }, api.ROOT_CONTEXT));
});

test('the rest of the processor interface does nothing and resolves', async () => {
  const processor = new BusinessContext(api);
  processor.onEnd({});
  await processor.forceFlush();
  await processor.shutdown();
});

test('the runtime puts it on the kit provider and registers the propagator business-context.js chooses', () => {
  const runtime = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'impact', 'runtime.js'), 'utf8');
  assert.match(runtime, /spanProcessors: \[new BusinessContext\(api\), processor\]/);
  assert.match(runtime, /setGlobalPropagator\(propagator\(core, env\)\)/);
  assert.doesNotMatch(runtime, /W3CBaggagePropagator|CompositePropagator|W3CTraceContextPropagator/);
});

// --- a caller's baggage ---------------------------------------------------------------------------------------------

const core = require('@opentelemetry/core');

const TRACE_ID = '0af7651916cd43dd8448eb211c80319c';
const CALLER = {
  traceparent: `00-${TRACE_ID}-b7ad6b7169203331-01`,
  baggage: 'user.id=someone-else,session.id=forged,tenant.plan=gold',
};

function extracted(env, own) {
  const getter = { keys: (carrier) => Object.keys(carrier), get: (carrier, key) => carrier[key] };
  return businessContext.propagator(core, env).extract(own || api.ROOT_CONTEXT, CALLER, getter);
}

function entries(ctx) {
  const bag = api.propagation.getBaggage(ctx);
  return bag ? Object.fromEntries(bag.getAllEntries().map(([key, entry]) => [key, entry.value])) : {};
}

test('by default nothing of a caller\'s baggage is read, and its trace context is', () => {
  for (const env of [{}, { IMPACT_TRUST_CALLER_BAGGAGE: '' }, { IMPACT_TRUST_CALLER_BAGGAGE: 'false' }, undefined]) {
    assert.ok(businessContext.propagator(core, env) instanceof core.W3CTraceContextPropagator);
    const ctx = extracted(env);
    assert.deepEqual(entries(ctx), {});
    assert.equal(api.trace.getSpanContext(ctx).traceId, TRACE_ID);
  }
});

test('a workload that trusts its callers reads their baggage, as OpenTelemetry does by default', () => {
  for (const value of ['true', 'TRUE', ' True ']) {
    const env = { IMPACT_TRUST_CALLER_BAGGAGE: value };
    assert.equal(businessContext.trustsCallers(env), true);
    const ctx = extracted(env);
    assert.deepEqual(entries(ctx), { 'user.id': 'someone-else', 'session.id': 'forged', 'tenant.plan': 'gold' });
    assert.equal(api.trace.getSpanContext(ctx).traceId, TRACE_ID);
  }
  assert.deepEqual(businessContext.propagator(core, { IMPACT_TRUST_CALLER_BAGGAGE: 'true' }).fields().sort(), [
    'baggage',
    'traceparent',
    'tracestate',
  ]);
});

test('the value the application sets after the request arrived wins over a trusted caller\'s', () => {
  const ctx = extracted({ IMPACT_TRUST_CALLER_BAGGAGE: 'true' });
  const bag = api.propagation.getBaggage(ctx).setEntry('user.id', { value: 'u-1842' });
  const { tracer, exporter } = traced();
  tracer.startSpan('chat gpt-4o-mini', {}, api.propagation.setBaggage(ctx, bag)).end();
  const [span] = exporter.getFinishedSpans();
  assert.equal(span.attributes['user.id'], 'u-1842');
  assert.equal(span.attributes['session.id'], 'forged'); // the trusted caller's, where the application set none
  assert.equal(span.attributes['tenant.plan'], undefined);
});

test('a core without the baggage pair still gets trace context', () => {
  const partial = {
    W3CTraceContextPropagator: core.W3CTraceContextPropagator,
    CompositePropagator() {
      throw new Error('no');
    },
  };
  const trusting = { IMPACT_TRUST_CALLER_BAGGAGE: 'true' };
  const older = { W3CTraceContextPropagator: core.W3CTraceContextPropagator };
  for (const tree of [partial, older]) {
    assert.ok(businessContext.propagator(tree, trusting) instanceof core.W3CTraceContextPropagator);
  }
});
