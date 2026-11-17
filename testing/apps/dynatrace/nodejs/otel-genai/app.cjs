// SPDX-License-Identifier: Apache-2.0
'use strict';

// The canonical interaction in Node.js, traced by the OpenTelemetry GenAI instrumentation of the OpenAI SDK and exported
// as OTLP to a Dynatrace environment, configured as Dynatrace's documentation shows (the OpenTelemetry SDK's variables):
//
//   OTEL_EXPORTER_OTLP_ENDPOINT=https://<environment>.live.dynatrace.com/api/v2/otlp
//   OTEL_EXPORTER_OTLP_HEADERS="Authorization=Api-Token%20<token>" OTEL_SERVICE_NAME=support-assistant
//   OPENAI_BASE_URL=http://<provider>/v1 node app.cjs
//
// The chats are the instrumentation's; the agent, tool and retrieval spans are written with the OpenTelemetry API in the
// GenAI conventions, as Dynatrace's OpenTelemetry guide writes its own GenAI spans. Content carries CONTENT-CANARY. The
// OpenAI key is a planted, fictitious one.

process.env.OTEL_SEMCONV_STABILITY_OPT_IN ??= 'gen_ai_latest_experimental';
process.env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT ??= 'true';

const api = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { registerInstrumentations } = require('@opentelemetry/instrumentation');
const { OpenAIInstrumentation } = require('@opentelemetry/instrumentation-openai');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { BatchSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');

const CANARY = 'CONTENT-CANARY';
const TOOLS = [
  {
    type: 'function',
    function: {
      name: 'get_order',
      description: 'Look up an order',
      parameters: { type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'] },
    },
  },
];

// The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS, the documented configuration.
const provider = new TracerProvider({
  resource: resourceFromAttributes({
    'service.name': process.env.OTEL_SERVICE_NAME || 'support-assistant',
    'service.version': '4.2.0',
  }),
  spanProcessors: [new BatchSpanProcessor({ exporter: new OTLPTraceExporter() })],
});
api.context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
api.trace.setGlobalTracerProvider(provider);
registerInstrumentations({ tracerProvider: provider, instrumentations: [new OpenAIInstrumentation()] });
const tracer = provider.getTracer('support-assistant');

const OpenAI = require('openai');

// An agent's and a tool's spans are internal, the default kind; a retrieval is a client call.
async function inSpan(name, kind, attributes, work) {
  return tracer.startActiveSpan(name, kind === undefined ? { attributes } : { kind, attributes }, async (span) => {
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
    'invoke_agent support',
    undefined,
    { 'gen_ai.operation.name': 'invoke_agent', 'gen_ai.agent.name': 'support' },
    async () => {
      const first = await client.chat.completions.create({ model: 'gpt-4o', messages: [user], tools: TOOLS });
      const call = first.choices[0].message.tool_calls[0];
      const args = JSON.parse(call.function.arguments);
      const result = { status: 'shipped', note: CANARY };
      await inSpan(
        'execute_tool get_order',
        undefined,
        {
          'gen_ai.operation.name': 'execute_tool',
          'gen_ai.tool.name': 'get_order',
          'gen_ai.tool.call.id': call.id,
          'gen_ai.tool.call.arguments': JSON.stringify(args),
          'gen_ai.tool.call.result': JSON.stringify(result),
        },
        async () => result,
      );
      await inSpan(
        'retrieval help-centre',
        api.SpanKind.CLIENT,
        {
          'gen_ai.operation.name': 'retrieval',
          'gen_ai.data_source.id': 'help-centre',
          'gen_ai.retrieval.top_k': 5,
          'gen_ai.retrieval.query.text': `When does an order ship? ${CANARY}`,
          'gen_ai.retrieval.documents': JSON.stringify([
            { id: 'kb-112', score: 0.91 },
            { id: 'kb-407', score: 0.73 },
            { id: 'kb-019', score: 0.52 },
          ]),
        },
        async () => null,
      );
      await client.chat.completions.create({
        model: 'gpt-4o',
        messages: [user, first.choices[0].message, { role: 'tool', tool_call_id: call.id, content: JSON.stringify(result) }],
        tools: TOOLS,
      });
    },
  );
  await provider.shutdown();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
