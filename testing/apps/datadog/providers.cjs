// SPDX-License-Identifier: Apache-2.0

// One call to each of OpenAI, Anthropic and Amazon Bedrock, traced by Datadog's Node.js tracer (dd-trace).
//
//   DD_TRACE_AGENT_URL=http://<agent or relay> DD_LLMOBS_ENABLED=1 DD_LLMOBS_ML_APP=support-assistant \
//   DD_SERVICE=support-assistant PROVIDER_URL=http://<recording mock> node providers.cjs
//
// PROVIDER_URL is the kit's recording mock (testing/mock/server.py), which answers each provider's API with fixed
// answers; the /openai and /anthropic paths let the tracer name the provider from the client's base URL. The keys and
// AWS credentials are planted, fictitious ones. Each call carries CONTENT-CANARY in its prompt.

'use strict'

const tracer = require('dd-trace').init()
const OpenAI = require('openai')
const Anthropic = require('@anthropic-ai/sdk')
const { BedrockRuntimeClient, ConverseCommand } = require('@aws-sdk/client-bedrock-runtime')
const { NodeHttpHandler } = require('@smithy/node-http-handler')

const CANARY = 'CONTENT-CANARY'

async function main () {
  const url = process.env.PROVIDER_URL.replace(/\/$/, '')
  const question = `Where is order 1042? ${CANARY}`
  await new OpenAI({ baseURL: `${url}/openai/v1`, apiKey: 'planted-openai-key' }).chat.completions.create({
    model: 'gpt-4o-mini',
    messages: [{ role: 'system', content: 'Answer briefly.' }, { role: 'user', content: question }]
  })
  await new Anthropic({ baseURL: `${url}/anthropic`, apiKey: 'planted-anthropic-key' }).messages.create({
    model: 'claude-sonnet-4-5', max_tokens: 100, system: 'Answer briefly.', messages: [{ role: 'user', content: question }]
  })
  await new BedrockRuntimeClient({
    endpoint: url,
    region: 'us-east-1',
    requestHandler: new NodeHttpHandler(), // the mock speaks HTTP/1.1; the client's default for Bedrock is HTTP/2
    credentials: { accessKeyId: 'AKIAPLANTED0000000000', secretAccessKey: 'planted-aws-secret' }
  }).send(new ConverseCommand({
    modelId: 'amazon.nova-micro-v1:0',
    system: [{ text: 'Answer briefly.' }],
    messages: [{ role: 'user', content: [{ text: question }] }],
    inferenceConfig: { maxTokens: 100 }
  }))
  await tracer.llmobs.flush?.()
  await new Promise((resolve) => tracer._tracer._exporter.flush(resolve))
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
