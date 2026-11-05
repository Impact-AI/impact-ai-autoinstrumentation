// SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixture workload (CommonJS): an Express application that calls OpenAI and Anthropic and has no tracing of its own.
// It is the workload the overhead of the instrumentation is measured on.

const express = require('express');
const openaiModule = require('openai');
const anthropicModule = require('@anthropic-ai/sdk');

const OpenAI = openaiModule.OpenAI || openaiModule.default || openaiModule;
const Anthropic = anthropicModule.Anthropic || anthropicModule.default || anthropicModule;
const stub = process.env.STUB_URL;
const openai = new OpenAI({ apiKey: 'sk-test', baseURL: stub + '/v1', maxRetries: 0 });
const anthropic = new Anthropic({ apiKey: 'sk-ant-test', baseURL: stub, maxRetries: 0 });

const app = express();
app.use(express.json());

app.get('/healthz', (request, response) => response.json({ ok: true }));

app.get('/debug/runtime', (request, response) => {
  response.json({
    node: process.versions.node,
    nodeOptions: process.env.NODE_OPTIONS || null,
    moduleSystem: 'cjs',
    environment: Object.fromEntries(Object.entries(process.env).filter(([name]) => /^(OTEL_|ANTHROPIC_|IMPACT_)/.test(name))),
  });
});

app.post('/chat/openai', async (request, response) => {
  try {
    const messages = [
      { role: 'system', content: 'be brief' },
      { role: 'user', content: (request.body && request.body.prompt) || 'hi there' },
    ];
    if (request.body && request.body.stream) {
      // the answer read as a stream, chunk by chunk, and handed back whole
      let answer = '';
      const stream = await openai.chat.completions.create({ model: 'gpt-4o-mini', messages, stream: true });
      for await (const chunk of stream) answer += (chunk.choices[0] && chunk.choices[0].delta.content) || '';
      return response.json({ answer });
    }
    const reply = await openai.chat.completions.create({ model: 'gpt-4o-mini', messages });
    response.json({ answer: reply.choices[0].message.content });
  } catch (error) {
    response.status(500).json({ error: String(error && error.message) });
  }
});

app.post('/chat/anthropic', async (request, response) => {
  try {
    const reply = await anthropic.messages.create({
      model: 'claude-stub',
      max_tokens: 64,
      system: 'be brief',
      messages: [{ role: 'user', content: (request.body && request.body.prompt) || 'hi there' }],
    });
    response.json({ answer: reply.content[0].text });
  } catch (error) {
    response.status(500).json({ error: String(error && error.message) });
  }
});

app.listen(8000, '0.0.0.0');
