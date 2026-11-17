// SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixture workload (CommonJS): an application that already traces with LangSmith, the way LangSmith's own guides set it
// up. LangSmith's default tracing is on (LANGSMITH_TRACING=true) and sends to LANGSMITH_ENDPOINT, which the tests point at
// the recording mock's `theirs` instance. With LangSmith's OpenTelemetry mode on (LANGSMITH_OTEL_ENABLED=true or
// LANGSMITH_TRACING_MODE=otel) the app calls initializeOTEL() first, as LangSmith's guide says it must.
//
//   POST /langsmith/langchain   a LangChain v1 agent (createAgent): LangChain's callbacks
//   POST /langsmith/langgraph   LangGraph's prebuilt ReAct agent: LangGraph's callbacks
//   POST /langsmith/traceable   a function wrapped in traceable() that calls OpenAI through LangSmith's wrapOpenAI
//   POST /flush                 waits until LangSmith has sent everything it holds
//   GET  /healthz               the probe

const otelMode =
  process.env.LANGSMITH_OTEL_ENABLED === 'true' || (process.env.LANGSMITH_TRACING_MODE || '').toLowerCase() === 'otel';
const otel = otelMode ? require('langsmith/experimental/otel/setup').initializeOTEL() : undefined;

const http = require('node:http');
const { traceable } = require('langsmith/traceable');
const { wrapOpenAI } = require('langsmith/wrappers');
const { Client } = require('langsmith');
const { ChatOpenAI } = require('@langchain/openai');
const { tool } = require('@langchain/core/tools');
const { awaitAllCallbacks } = require('@langchain/core/callbacks/promises');
// LangChain's own LangSmith client is not exported; its file is read by path (the package's exports do not list it).
const { getDefaultLangChainClientSingleton } = require(
  require.resolve('@langchain/core/package.json').replace(/package\.json$/, 'dist/singletons/tracer.cjs'),
);
const OpenAI = require('openai').OpenAI;

const STUB = process.env.STUB_URL;
const PROMPT = 'What is the weather in Paris?';
const client = new Client();

const getWeather = tool(async ({ city }) => `Sunny in ${city}`, {
  name: 'get_weather',
  description: 'Weather of a city.',
  schema: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
});

function chatModel() {
  return new ChatOpenAI({ model: 'gpt-4o-mini', apiKey: 'sk-test', configuration: { baseURL: STUB + '/v1' } });
}

async function langchainAgent(prompt) {
  const { createAgent } = require('langchain');
  const agent = createAgent({ model: chatModel(), tools: [getWeather], name: 'weather_agent', systemPrompt: 'be brief' });
  const out = await agent.invoke({ messages: [{ role: 'user', content: prompt }] });
  return out.messages[out.messages.length - 1].content;
}

async function langgraphAgent(prompt) {
  const { createReactAgent } = require('@langchain/langgraph/prebuilt');
  const agent = createReactAgent({ llm: chatModel(), tools: [getWeather], name: 'react_agent' });
  const out = await agent.invoke({ messages: [{ role: 'user', content: prompt }] });
  return out.messages[out.messages.length - 1].content;
}

const openai = wrapOpenAI(new OpenAI({ apiKey: 'sk-test', baseURL: STUB + '/v1', maxRetries: 0 }), { client });
const weather = traceable(async (city) => `Sunny in ${city}`, { name: 'get_weather', run_type: 'tool', client });
const planTrip = traceable(
  async (question) => {
    const answer = await openai.chat.completions.create({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: question }],
    });
    return `${answer.choices[0].message.content} ${await weather('Paris')}`;
  },
  { name: 'plan_trip', client },
);

async function flush() {
  await awaitAllCallbacks();
  await getDefaultLangChainClientSingleton().awaitPendingTraceBatches();
  await client.awaitPendingTraceBatches();
  if (otel) await otel.DEFAULT_LANGSMITH_SPAN_PROCESSOR.forceFlush();
}

const ROUTES = {
  '/langsmith/langchain': langchainAgent,
  '/langsmith/langgraph': langgraphAgent,
  '/langsmith/traceable': (prompt) => planTrip(prompt),
};

function body(request) {
  return new Promise((resolve) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));
      } catch (error) {
        resolve({});
      }
    });
  });
}

function send(response, status, value) {
  response.statusCode = status;
  response.setHeader('content-type', 'application/json');
  response.end(JSON.stringify(value));
}

http
  .createServer(async (request, response) => {
    try {
      if (request.url === '/healthz') return send(response, 200, { ok: true });
      const ask = await body(request);
      if (request.url === '/flush') {
        await flush();
        return send(response, 200, { ok: true });
      }
      const route = ROUTES[request.url];
      if (!route) return send(response, 404, { error: 'no such route' });
      return send(response, 200, { answer: await route(ask.prompt || PROMPT) });
    } catch (error) {
      return send(response, 500, { error: 'request failed' });
    }
  })
  .listen(Number(process.env.PORT || 8000), '0.0.0.0');
