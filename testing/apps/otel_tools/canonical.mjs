// SPDX-License-Identifier: Apache-2.0

// The canonical interaction written by one AI tool's own Node.js SDK, for recording what that SDK sends its backend.
//
//     THEIRS_URL=http://127.0.0.1:8080 PROVIDER_URL=http://127.0.0.1:8091 node canonical.mjs langfuse
//
// The same interaction as canonical.py: an agent `support` asks `gpt-4o` (answered as `gpt-4o-2024-08-06` by
// provider.py) where an order is, the model asks for the tool `get_order`, the tool runs, a retrieval over the help
// centre returns three documents of a top-k of five, and a second chat answers. Content carries CONTENT-CANARY. Each
// tool exports to THEIRS_URL through the one setting the kit switches when it copies, with planted credentials.

const CANARY = "CONTENT-CANARY";
const QUESTION = `Where is order 1042? ${CANARY}`;
const QUERY = `When does an order ship? ${CANARY}`;
const RESULT = { status: "shipped", note: CANARY };
const DOCUMENTS = [
  { id: "kb-112", score: 0.91, text: `Orders ship within two days. ${CANARY}` },
  { id: "kb-407", score: 0.73, text: `Tracking numbers arrive by e-mail. ${CANARY}` },
  { id: "kb-019", score: 0.52, text: `Weekend orders ship on Monday. ${CANARY}` },
];
const TOP_K = 5;
const TOOLS = [
  {
    type: "function",
    function: {
      name: "get_order",
      description: "Look up an order by its id.",
      parameters: { type: "object", properties: { order_id: { type: "string" } }, required: ["order_id"] },
    },
  },
];
const SERVICE = { name: "support-assistant", version: "4.2.0" };
const PLANTED = {
  traceloop: "planted-traceloop-key",
  phoenix: "planted-phoenix-key",
  langfuse: ["pk-lf-planted", "sk-lf-planted"],
  logfire: "pylf_v1_us_planted",
  braintrust: "planted-braintrust-key",
};

const theirs = () => process.env.THEIRS_URL.replace(/\/+$/u, "");
const providerBase = () => `${process.env.PROVIDER_URL.replace(/\/+$/u, "")}/v1`;
process.env.OTEL_SERVICE_NAME ??= SERVICE.name;
process.env.OTEL_RESOURCE_ATTRIBUTES ??= `service.version=${SERVICE.version}`;

async function openai() {
  const { default: OpenAI } = await import("openai");
  return new OpenAI({ apiKey: "sk-test", baseURL: providerBase() });
}

async function firstTurn(client) {
  const messages = [{ role: "user", content: QUESTION }];
  const reply = await client.chat.completions.create({ model: "gpt-4o", messages, tools: TOOLS });
  return { messages, message: reply.choices[0].message };
}

async function secondTurn(client, messages, message, result) {
  const call = message.tool_calls[0];
  const reply = await client.chat.completions.create({
    model: "gpt-4o",
    tools: TOOLS,
    messages: [
      ...messages,
      { role: "assistant", content: null, tool_calls: [{ id: call.id, type: "function", function: call.function }] },
      { role: "tool", tool_call_id: call.id, content: result },
    ],
  });
  return reply.choices[0].message.content;
}

function lookup(args) {
  JSON.parse(args);
  return JSON.stringify(RESULT);
}

// Langfuse (the OpenTelemetry-based JS SDK): LangfuseSpanProcessor exports OTLP/HTTP JSON to
// LANGFUSE_BASE_URL/api/public/otel/v1/traces with Basic auth and its x-langfuse-* headers. Agents, tools and
// retrievers are observations of those types; the model calls go through observeOpenAI.
async function langfuse() {
  process.env.LANGFUSE_BASE_URL = theirs();
  [process.env.LANGFUSE_PUBLIC_KEY, process.env.LANGFUSE_SECRET_KEY] = PLANTED.langfuse;
  process.env.LANGFUSE_RELEASE ??= SERVICE.version;
  const { NodeSDK } = await import("@opentelemetry/sdk-node");
  const { LangfuseSpanProcessor } = await import("@langfuse/otel");
  const { startActiveObservation } = await import("@langfuse/tracing");
  const { observeOpenAI } = await import("@langfuse/openai");
  const processor = new LangfuseSpanProcessor({ exportMode: "immediate" });
  const sdk = new NodeSDK({ spanProcessors: [processor] });
  sdk.start();
  await startActiveObservation(
    "support",
    async (agent) => {
      agent.update({ input: QUESTION });
      const client = observeOpenAI(await openai());
      const { messages, message } = await firstTurn(client);
      const result = await startActiveObservation(
        "get_order",
        async (tool) => {
          const args = message.tool_calls[0].function.arguments;
          const output = lookup(args);
          tool.update({ input: args, output });
          return output;
        },
        { asType: "tool" },
      );
      await startActiveObservation(
        "help-centre",
        async (retriever) => retriever.update({ input: QUERY, output: DOCUMENTS }),
        { asType: "retriever" },
      );
      agent.update({ output: await secondTurn(client, messages, message, result) });
    },
    { asType: "agent" },
  );
  await processor.forceFlush();
  await sdk.shutdown();
}

// Pydantic Logfire for Node.js: configure() starts the OpenTelemetry Node SDK with its auto-instrumentations (OpenAI's
// among them) and exports OTLP/HTTP protobuf to LOGFIRE_BASE_URL/v1/traces with Authorization: <write token>. The
// agent, the tool and the retrieval are Logfire spans in the OpenTelemetry GenAI terms, their message templates naming
// no content.
async function logfire() {
  process.env.LOGFIRE_BASE_URL = theirs();
  process.env.LOGFIRE_TOKEN = PLANTED.logfire;
  process.env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT ??= "true";
  const sdk = await import("@pydantic/logfire-node");
  sdk.configure({
    serviceName: SERVICE.name,
    serviceVersion: SERVICE.version,
    sendToLogfire: true,
    metrics: false,
  });
  // The auto-instrumentations patch modules loaded with require() after configure().
  const { createRequire } = await import("node:module");
  const { default: OpenAI } = createRequire(import.meta.url)("openai");
  const client = new OpenAI({ apiKey: "sk-test", baseURL: providerBase() });
  await sdk.span("{gen_ai.operation.name} {gen_ai.agent.name}", {
    attributes: { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "support" },
    callback: async () => {
      const { messages, message } = await firstTurn(client);
      const call = message.tool_calls[0];
      const result = sdk.span("{gen_ai.operation.name} {gen_ai.tool.name}", {
        attributes: {
          "gen_ai.operation.name": "execute_tool",
          "gen_ai.tool.name": "get_order",
          "gen_ai.tool.call.id": call.id,
          "gen_ai.tool.call.arguments": call.function.arguments,
        },
        callback: (span) => {
          const output = lookup(call.function.arguments);
          span.setAttribute("gen_ai.tool.call.result", output);
          return output;
        },
      });
      sdk.span("{gen_ai.operation.name} {gen_ai.data_source.id}", {
        attributes: {
          "gen_ai.operation.name": "retrieval",
          "gen_ai.data_source.id": "help-centre",
          "gen_ai.request.top_k": TOP_K,
          "gen_ai.retrieval.query.text": QUERY,
          "gen_ai.retrieval.documents": JSON.stringify(DOCUMENTS.map(({ id, score }) => ({ id, score }))),
        },
        callback: () => DOCUMENTS,
      });
      return secondTurn(client, messages, message, result);
    },
  });
  await sdk.forceFlush();
  await sdk.shutdown();
}

const RUNS = { langfuse, logfire };

const tool = process.argv[2] ?? "";
if (!Object.hasOwn(RUNS, tool)) {
  console.error(`usage: canonical.mjs {${Object.keys(RUNS).join(",")}}`);
  process.exit(2);
}
await RUNS[tool]();
void TOP_K;
