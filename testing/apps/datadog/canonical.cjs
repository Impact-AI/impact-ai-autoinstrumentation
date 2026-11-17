// SPDX-License-Identifier: Apache-2.0

// The canonical interaction, traced by Datadog's Node.js tracer (dd-trace) with LLM Observability on.
//
// An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a
// retrieval from `help-centre` returns three documents of a top-k of 5; the model answers. Content carries
// CONTENT-CANARY.
//
//   DD_TRACE_AGENT_URL=http://<agent or relay> DD_LLMOBS_ENABLED=1 DD_LLMOBS_ML_APP=support-assistant \
//   DD_SERVICE=support-assistant DD_VERSION=4.2.0 OPENAI_BASE_URL=http://<canonical_provider>/openai/v1 \
//   node canonical.cjs
//
// Without DD_LLMOBS_ENABLED the same code runs with APM tracing alone (the OpenAI integration's spans). The OpenAI key
// is a planted, fictitious one.

'use strict'

const tracer = require('dd-trace').init()
const OpenAI = require('openai')

const llmobs = tracer.llmobs
const CANARY = 'CONTENT-CANARY'
const TOOLS = [{
  type: 'function',
  function: {
    name: 'get_order',
    description: 'Look up an order',
    parameters: { type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'] }
  }
}]
const DOCUMENTS = [
  { id: 'kb-112', score: 0.91, name: 'Shipping times', text: `Orders ship within two days. ${CANARY}` },
  { id: 'kb-407', score: 0.73, name: 'Tracking', text: `Track an order from its page. ${CANARY}` },
  { id: 'kb-019', score: 0.52, name: 'Returns', text: `Return an order within 30 days. ${CANARY}` }
]

function getOrder (orderId) {
  return llmobs.trace({ kind: 'tool', name: 'get_order' }, (span) => {
    const result = { status: 'shipped', note: CANARY }
    llmobs.annotate(span, { inputData: { order_id: orderId }, outputData: result })
    return result
  })
}

function searchHelpCentre (query) {
  return llmobs.trace({ kind: 'retrieval', name: 'help-centre' }, (span) => {
    llmobs.annotate(span, { inputData: query, outputData: DOCUMENTS, metadata: { top_k: 5 } })
    return DOCUMENTS
  })
}

async function main () {
  const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY || 'planted-openai-key' })
  const user = { role: 'user', content: `Where is order 1042? ${CANARY}` }
  await llmobs.trace({ kind: 'agent', name: 'support' }, async (agent) => {
    const first = await client.chat.completions.create({ model: 'gpt-4o', messages: [user], tools: TOOLS })
    const call = first.choices[0].message.tool_calls[0]
    const result = getOrder(JSON.parse(call.function.arguments).order_id)
    searchHelpCentre(`When does an order ship? ${CANARY}`)
    const asked = { role: 'assistant', content: null, tool_calls: [call] }
    const answered = { role: 'tool', tool_call_id: call.id, content: JSON.stringify(result) }
    const second = await client.chat.completions.create({ model: 'gpt-4o', messages: [user, asked, answered], tools: TOOLS })
    llmobs.annotate(agent, { inputData: user.content, outputData: second.choices[0].message.content })
  })
  await llmobs.flush?.()
  await new Promise((resolve) => tracer._tracer._exporter.flush(resolve))
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
