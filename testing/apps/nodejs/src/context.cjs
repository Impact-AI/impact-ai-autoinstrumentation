// SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixture workload (CommonJS): an HTTP server that calls OpenAI and Anthropic in the business context of the request, and
// has no tracing of its own. It depends on @opentelemetry/api for the one line a coding agent adds once the application
// has authenticated the request: the user, the session and the use case put in baggage (here the test names them in the
// query string). A key impact.ai does not read (tenant.plan) is set too.
//
//   POST /chat/openai/in-context?user=...&session=...&use_case=...      OpenAI: the kit's own provider
//   POST /chat/anthropic/in-context?user=...&session=...&use_case=...   Anthropic: its SDK's spans, the global provider
//   GET  /healthz

const http = require('node:http');
const { context, propagation } = require('@opentelemetry/api');
const openaiModule = require('openai');
const anthropicModule = require('@anthropic-ai/sdk');

const OpenAI = openaiModule.OpenAI || openaiModule.default || openaiModule;
const Anthropic = anthropicModule.Anthropic || anthropicModule.default || anthropicModule;
const stub = process.env.STUB_URL;
const openai = new OpenAI({ apiKey: 'sk-test', baseURL: stub + '/v1', maxRetries: 0 });
const anthropic = new Anthropic({ apiKey: 'sk-ant-test', baseURL: stub, maxRetries: 0 });

const calls = {
  openai: async (prompt) => {
    const reply = await openai.chat.completions.create({
      model: 'gpt-4o-mini',
      messages: [
        { role: 'system', content: 'be brief' },
        { role: 'user', content: prompt },
      ],
    });
    return reply.choices[0].message.content;
  },
  anthropic: async (prompt) => {
    const reply = await anthropic.messages.create({
      model: 'claude-stub',
      max_tokens: 64,
      system: 'be brief',
      messages: [{ role: 'user', content: prompt }],
    });
    return reply.content[0].text;
  },
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
      const url = new URL(request.url, 'http://fixture');
      if (url.pathname === '/healthz') return send(response, 200, { ok: true });
      const match = /^\/chat\/(openai|anthropic)\/in-context$/.exec(url.pathname);
      if (request.method !== 'POST' || !match) return send(response, 404, { error: 'not found' });
      const ask = await body(request);
      const query = url.searchParams;
      // The baggage of the request so far, with the application's entries added (a key without a value is left as it was).
      let baggage = propagation.getBaggage(context.active()) || propagation.createBaggage();
      const entries = {
        'user.id': query.get('user'),
        'session.id': query.get('session'),
        'impact.use_case': query.get('use_case'),
        'tenant.plan': 'gold',
      };
      for (const [key, value] of Object.entries(entries)) {
        if (value) baggage = baggage.setEntry(key, { value });
      }
      const answer = await context.with(propagation.setBaggage(context.active(), baggage), () =>
        calls[match[1]](ask.prompt || 'hi there'),
      );
      return send(response, 200, { answer });
    } catch (error) {
      return send(response, 500, { error: String(error && error.message) });
    }
  })
  .listen(8000, '0.0.0.0');
