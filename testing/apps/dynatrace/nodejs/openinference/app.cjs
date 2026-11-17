// SPDX-License-Identifier: Apache-2.0
'use strict';

// The canonical interaction in Node.js, traced by OpenInference and exported as OTLP to a Dynatrace environment,
// configured as Dynatrace's OpenInference guide shows: an OTLP/HTTP protobuf exporter whose endpoint comes from
// OTEL_EXPORTER_OTLP_ENDPOINT and whose Authorization header is made from DT_API_TOKEN in code.
//
//   OTEL_EXPORTER_OTLP_ENDPOINT=https://<environment>.live.dynatrace.com/api/v2/otlp/v1/traces DT_API_TOKEN=<token>
//   OTEL_SERVICE_NAME=support-assistant OPENAI_BASE_URL=http://<provider>/v1 node app.cjs
//
// DT_TOKEN_SCHEME is this harness's own (default `Bearer`, a platform token). The chats are the OpenAI instrumentation's;
// the agent, tool and retriever spans carry OpenInference's attributes, made with its own helpers. OpenInference has no
// key for a retrieval's top-k. Content carries CONTENT-CANARY. The OpenAI key is a planted, fictitious one.

const api = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { registerInstrumentations } = require('@opentelemetry/instrumentation');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { BatchSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');
const { getInputAttributes, getOutputAttributes, getRetrieverAttributes, getToolAttributes } = require(
  '@arizeai/openinference-core',
);
const { OpenAIInstrumentation } = require('@arizeai/openinference-instrumentation-openai');

const CANARY = 'CONTENT-CANARY';
const PARAMETERS = { type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'] };
const TOOLS = [{ type: 'function', function: { name: 'get_order', description: 'Look up an order', parameters: PARAMETERS } }];
const DOCUMENTS = [
  { id: 'kb-112', score: 0.91, content: `Orders ship within two days. ${CANARY}` },
  { id: 'kb-407', score: 0.73, content: `Track an order from its page. ${CANARY}` },
  { id: 'kb-019', score: 0.52, content: `Return an order within 30 days. ${CANARY}` },
];

const scheme = process.env.DT_TOKEN_SCHEME || 'Bearer';
const provider = new TracerProvider({
  resource: resourceFromAttributes({
    'service.name': process.env.OTEL_SERVICE_NAME || 'support-assistant',
    'service.version': '4.2.0',
  }),
  spanProcessors: [
    new BatchSpanProcessor({
      exporter: new OTLPTraceExporter({
        url: process.env.OTEL_EXPORTER_OTLP_ENDPOINT,
        headers: { Authorization: `${scheme} ${process.env.DT_API_TOKEN}` },
      }),
    }),
  ],
});
api.context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
api.trace.setGlobalTracerProvider(provider);
registerInstrumentations({ tracerProvider: provider, instrumentations: [new OpenAIInstrumentation()] });
const tracer = provider.getTracer('support-assistant');

const OpenAI = require('openai');

async function inSpan(name, attributes, work) {
  return tracer.startActiveSpan(name, { attributes }, async (span) => {
    try {
      return await work(span);
    } finally {
      span.end();
    }
  });
}

async function main() {
  const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY || 'planted-openai-key' });
  const user = { role: 'user', content: `Where is order 1042? ${CANARY}` };
  await inSpan(
    'support',
    { 'openinference.span.kind': 'AGENT', 'agent.name': 'support', ...getInputAttributes(user.content) },
    async (agent) => {
      const first = await client.chat.completions.create({ model: 'gpt-4o', messages: [user], tools: TOOLS });
      const call = first.choices[0].message.tool_calls[0];
      const args = JSON.parse(call.function.arguments);
      const result = { status: 'shipped', note: CANARY };
      await inSpan(
        'get_order',
        {
          'openinference.span.kind': 'TOOL',
          ...getToolAttributes({ name: 'get_order', description: 'Look up an order', parameters: PARAMETERS }),
          ...getInputAttributes(JSON.stringify(args)),
          ...getOutputAttributes(JSON.stringify(result)),
        },
        async () => result,
      );
      await inSpan(
        'help-centre',
        {
          'openinference.span.kind': 'RETRIEVER',
          ...getInputAttributes(`When does an order ship? ${CANARY}`),
          ...getRetrieverAttributes({ documents: DOCUMENTS }),
        },
        async () => DOCUMENTS,
      );
      const last = await client.chat.completions.create({
        model: 'gpt-4o',
        messages: [user, first.choices[0].message, { role: 'tool', tool_call_id: call.id, content: JSON.stringify(result) }],
        tools: TOOLS,
      });
      agent.setAttributes(getOutputAttributes(last.choices[0].message.content));
    },
  );
  await provider.shutdown();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
