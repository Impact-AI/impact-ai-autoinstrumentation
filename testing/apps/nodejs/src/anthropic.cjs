// SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixture workload (CommonJS): an HTTP server that calls an Anthropic provider and has no tracing of its own.
// POST /chat/anthropic asks the provider (STUB_URL) and answers with its text; GET /healthz is the probe.

const http = require('node:http');
const module_ = require('@anthropic-ai/sdk');
const Anthropic = module_.Anthropic || module_.default || module_;

const client = new Anthropic({ apiKey: 'sk-ant-test', baseURL: process.env.STUB_URL, maxRetries: 0 });

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
      if (request.url === '/debug/runtime') {
        return send(response, 200, {
          node: process.versions.node,
          nodeOptions: process.env.NODE_OPTIONS || null,
          moduleSystem: 'cjs',
          environment: Object.fromEntries(Object.entries(process.env).filter(([name]) => /^(OTEL_|ANTHROPIC_|IMPACT_)/.test(name))),
        });
      }
      const ask = await body(request);
      const reply = await client.messages.create({
        model: 'claude-stub',
        max_tokens: 64,
        system: 'be brief',
        messages: [{ role: 'user', content: ask.prompt || 'hi there' }],
      });
      return send(response, 200, { answer: reply.content[0].text });
    } catch (error) {
      return send(response, 500, { error: String(error && error.message) });
    }
  })
  .listen(8000, '0.0.0.0');
