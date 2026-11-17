#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction under New Relic's Python agent, with its AI monitoring on.

One agent, `support` (LangChain's create_agent), answers "Where is order 1042?": a chat with gpt-4o asks for the tool
`get_order`, the tool looks the answer up in a help centre (a retrieval of a top-k of 5 that returns three documents),
and a second chat answers; 120 + 300 input and 20 + 80 output tokens. Every prompt, reply, tool argument and result,
the retrieval's query and its documents carry the planted text CONTENT-CANARY. Nothing here traces by hand: the spans
and AI monitoring events are what the agent records by itself.

The model is answered in process by an OpenAI-compatible double (an httpx transport) with these fixed answers, so the
OpenAI SDK and New Relic's instrumentation of it run unchanged and no request leaves the machine but the agent's own,
to the collector its environment names (NEW_RELIC_HOST, NEW_RELIC_PORT, NEW_RELIC_CA_BUNDLE_PATH; see
testing/apps/newrelic_record.py). The agent connects before the interaction and sends everything at shutdown.
"""

from __future__ import annotations

import json

import newrelic.agent

newrelic.agent.initialize()
application = newrelic.agent.register_application(timeout=30.0)

import httpx  # noqa: E402 - after the agent, which instruments what is imported later
from langchain.agents import create_agent  # noqa: E402
from langchain_community.vectorstores.inmemory import InMemoryVectorStore  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_core.embeddings import DeterministicFakeEmbedding  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

CANARY = "CONTENT-CANARY"
QUESTION = f"Where is order 1042? {CANARY}"
QUERY = f"When does an order ship? {CANARY}"
ARGUMENTS = {"order_id": f"1042 {CANARY}"}
RESULT = {"status": "shipped", "note": CANARY}
ANSWER = f"Order 1042 has shipped. {CANARY}"
MODEL, RESPONSE_MODEL = "gpt-4o", "gpt-4o-2024-08-06"
DOCUMENTS = [
    ("kb-112", f"Orders ship within two days. {CANARY}"),
    ("kb-407", f"Tracking numbers arrive by e-mail. {CANARY}"),
    ("kb-019", f"Returns are free for thirty days. {CANARY}"),
]


def completion(answer_id: str, message: dict, finish: str, prompt: int, completion_tokens: int) -> dict:
    return {
        "id": answer_id,
        "object": "chat.completion",
        "created": 1762646400,
        "model": RESPONSE_MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": message, "logprobs": None}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens,
        },
    }


def provider(request: httpx.Request) -> httpx.Response:
    """The model's two fixed answers: a call of get_order, then, once its result is in, the answer."""
    messages = json.loads(request.content).get("messages") or []
    if not any(message.get("role") == "tool" for message in messages):
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_order", "arguments": json.dumps(ARGUMENTS)},
        }
        body = completion(
            "chatcmpl-1", {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls", 120, 20
        )
    else:
        body = completion("chatcmpl-2", {"role": "assistant", "content": ANSWER}, "stop", 300, 80)
    headers = {"x-request-id": "req_" + body["id"], "openai-version": "2020-10-01"}
    return httpx.Response(200, json=body, headers=headers)


help_centre = InMemoryVectorStore(DeterministicFakeEmbedding(size=16))
help_centre.add_documents([Document(page_content=text, metadata={"id": doc_id}) for doc_id, text in DOCUMENTS])


@tool
def get_order(order_id: str) -> str:
    """Look up an order and what the help centre says about shipping."""
    help_centre.similarity_search(QUERY, k=5)
    return json.dumps(RESULT)


def main() -> None:
    model = ChatOpenAI(
        model=MODEL,
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(provider)),
        max_retries=0,
    )
    # New Relic instruments an agent that create_agent makes inside a transaction.
    with newrelic.agent.BackgroundTask(application, name="support-assistant"):
        agent = create_agent(model, tools=[get_order], name="support")
        agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})
    newrelic.agent.shutdown_agent(timeout=30.0)


if __name__ == "__main__":
    main()
