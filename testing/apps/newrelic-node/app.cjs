// SPDX-License-Identifier: Apache-2.0

// The canonical interaction under New Relic's Node.js agent, with its AI monitoring on.
//
// One agent, `support` (LangChain's createAgent, a LangGraph graph), answers "Where is order 1042?": a chat with gpt-4o
// asks for the tool `get_order`, the tool looks the answer up in a help centre (a retrieval of a top-k of 5 that returns
// three documents), and a second chat answers; 120 + 300 input and 20 + 80 output tokens. Every prompt, reply, tool
// argument and result, the retrieval's query and its documents carry the planted text CONTENT-CANARY. Nothing here
// traces by hand: the spans and AI monitoring events are what the agent records by itself.
//
// The model is answered in process by an OpenAI-compatible double (the SDK's fetch option) with these fixed answers,
// so the OpenAI SDK and New Relic's instrumentation of it run unchanged and no request leaves the machine but the
// agent's own, to the collector its environment names (NEW_RELIC_HOST, NEW_RELIC_PORT, NEW_RELIC_CERTIFICATES; see
// testing/apps/newrelic_record.py). The agent connects before the interaction and sends everything at shutdown.

'use strict'

const newrelic = require('newrelic')

const CANARY = 'CONTENT-CANARY'
const QUESTION = `Where is order 1042? ${CANARY}`
const QUERY = `When does an order ship? ${CANARY}`
const ARGUMENTS = { order_id: `1042 ${CANARY}` }
const RESULT = { status: 'shipped', note: CANARY }
const ANSWER = `Order 1042 has shipped. ${CANARY}`
const MODEL = 'gpt-4o'
const RESPONSE_MODEL = 'gpt-4o-2024-08-06'
const DOCUMENTS = [
  ['kb-112', `Orders ship within two days. ${CANARY}`],
  ['kb-407', `Tracking numbers arrive by e-mail. ${CANARY}`],
  ['kb-019', `Returns are free for thirty days. ${CANARY}`]
]

function completion(id, message, finish, prompt, completionTokens) {
  return {
    id,
    object: 'chat.completion',
    created: 1762646400,
    model: RESPONSE_MODEL,
    choices: [{ index: 0, finish_reason: finish, message, logprobs: null }],
    usage: { prompt_tokens: prompt, completion_tokens: completionTokens, total_tokens: prompt + completionTokens }
  }
}

// The model's two fixed answers: a call of get_order, then, once its result is in, the answer.
async function provider(url, init) {
  const messages = JSON.parse(init.body).messages || []
  let body
  if (!messages.some((message) => message.role === 'tool')) {
    const call = { id: 'call_1', type: 'function', function: { name: 'get_order', arguments: JSON.stringify(ARGUMENTS) } }
    body = completion('chatcmpl-1', { role: 'assistant', content: null, tool_calls: [call] }, 'tool_calls', 120, 20)
  } else {
    body = completion('chatcmpl-2', { role: 'assistant', content: ANSWER }, 'stop', 300, 80)
  }
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json', 'x-request-id': `req_${body.id}`, 'openai-version': '2020-10-01' }
  })
}

async function main() {
  // Loaded after the agent, which instruments what is required later.
  const { createAgent } = require('langchain')
  const { ChatOpenAI } = require('@langchain/openai')
  const { tool } = require('@langchain/core/tools')
  const { Document } = require('@langchain/core/documents')
  const { VectorStore } = require('@langchain/core/vectorstores')
  const { FakeEmbeddings } = require('@langchain/core/utils/testing')
  const { z } = require('zod')

  // A help centre of three documents: the smallest vector store, on LangChain's own base class.
  class HelpCentre extends VectorStore {
    _vectorstoreType() {
      return 'help-centre'
    }

    async addVectors() {}

    async addDocuments() {}

    async similaritySearchVectorWithScore(_query, k) {
      return DOCUMENTS.slice(0, k).map(([id, text], i) => [new Document({ pageContent: text, metadata: { id } }), 0.9 - i / 5])
    }
  }
  const helpCentre = new HelpCentre(new FakeEmbeddings(), {})

  const getOrder = tool(
    async () => {
      await helpCentre.similaritySearch(QUERY, 5)
      return JSON.stringify(RESULT)
    },
    {
      name: 'get_order',
      description: 'Look up an order and what the help centre says about shipping.',
      schema: z.object({ order_id: z.string() })
    }
  )
  const model = new ChatOpenAI({ model: MODEL, apiKey: 'sk-test', maxRetries: 0, configuration: { fetch: provider } })

  await new Promise((resolve, reject) => {
    newrelic.startBackgroundTransaction('support-assistant', async () => {
      try {
        const agent = createAgent({ model, tools: [getOrder], name: 'support' })
        await agent.invoke({ messages: [{ role: 'user', content: QUESTION }] })
        resolve()
      } catch (error) {
        reject(error)
      } finally {
        newrelic.getTransaction().end()
      }
    })
  })
  await new Promise((resolve) => newrelic.shutdown({ collectPendingData: true, timeout: 30000 }, resolve))
}

newrelic.agent.on('started', () => {
  main().catch((error) => {
    console.error(error)
    process.exitCode = 1
  })
})
