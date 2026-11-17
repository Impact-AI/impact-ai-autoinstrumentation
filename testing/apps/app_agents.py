# SPDX-License-Identifier: Apache-2.0

"""Fixture workload (ASGI): agents built with the OpenAI Agents SDK and with LangChain / LangGraph.

Every agent has one tool, `get_weather`; the mock provider (testing/mock/server.py) answers the first model call with a
call of that tool and the second with the usual answer, which is one round of an agent loop. The app has no tracing of
its own beyond what the frameworks do by default, and imports each framework on first use, like the other fixtures.
"""

import os

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
STUB = os.environ["STUB_URL"]
# The Agents SDK builds its model client from the environment.
os.environ.setdefault("OPENAI_BASE_URL", STUB + "/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

PROMPT = "What is the weather in Paris?"


class Ask(BaseModel):
    prompt: str = PROMPT


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/otel/global-tracer-provider")
def global_tracer_provider():
    """The process's global tracer provider, as a library that makes its spans with the global tracer sees it: the
    package never sets it (the cells of the Python qualification check it after the model calls)."""
    try:
        from opentelemetry import trace
    except ImportError:
        return {"provider": None}
    return {"provider": type(trace.get_tracer_provider()).__name__}


def get_weather(city: str) -> str:
    """Weather of a city."""
    return f"Sunny in {city}"


def agents_sdk():
    """The Agents SDK, with its default tracing on and its default exporter pointed at the stub instead of OpenAI."""
    import agents
    from agents.tracing.processors import default_exporter

    default_exporter().endpoint = STUB + "/v1/traces/ingest"
    return agents


@app.post("/agent/openai-agents")
async def openai_agents(ask: Ask):
    """The SDK's default model API, Responses."""
    sdk = agents_sdk()
    agent = sdk.Agent(
        name="Weather agent", instructions="be brief", tools=[sdk.function_tool(get_weather)], model="gpt-4o-mini"
    )
    result = await sdk.Runner.run(agent, ask.prompt)
    return {"answer": result.final_output}


@app.post("/agent/openai-agents-chat")
async def openai_agents_chat(ask: Ask):
    """The same agent on the Chat Completions API."""
    from openai import AsyncOpenAI

    sdk = agents_sdk()
    model = sdk.OpenAIChatCompletionsModel(
        model="gpt-4o-mini", openai_client=AsyncOpenAI(base_url=STUB + "/v1", api_key="sk-test")
    )
    agent = sdk.Agent(
        name="Weather agent", instructions="be brief", tools=[sdk.function_tool(get_weather)], model=model
    )
    result = await sdk.Runner.run(agent, ask.prompt)
    return {"answer": result.final_output}


def chat_model(**options):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o-mini", base_url=STUB + "/v1", api_key="sk-test", **options)


def langchain_weather_agent():
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    return create_agent(chat_model(), tools=[tool(get_weather)], name="weather_agent", system_prompt="be brief")


@app.post("/agent/langchain")
def langchain_agent(ask: Ask):
    """A LangChain v1 agent, which runs on LangGraph."""
    out = langchain_weather_agent().invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


@app.post("/agent/langchain-executor")
def langchain_executor(ask: Ask):
    """A LangChain 0.3 agent: a tool-calling agent run by AgentExecutor, which is how an application written before
    LangChain v1 runs one (the oldest fixture). AgentExecutor streams the model's answer; the stub answers whole."""
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool

    tools = [tool(get_weather)]
    prompt = ChatPromptTemplate.from_messages(
        [("system", "be brief"), ("human", "{input}"), ("placeholder", "{agent_scratchpad}")]
    )
    executor = AgentExecutor(
        agent=create_tool_calling_agent(chat_model(disable_streaming=True), tools, prompt),
        tools=tools,
        name="weather_agent",
    )
    return {"answer": executor.invoke({"input": ask.prompt})["output"]}


@app.post("/chain/langchain-core")
def langchain_core_chain(ask: Ask):
    """langchain-core on its own: a prompt template piped into a chat model, as an application without the langchain
    package calls a model."""
    from langchain_core.prompts import ChatPromptTemplate

    chain = ChatPromptTemplate.from_messages([("system", "be brief"), ("user", "{question}")]) | chat_model()
    return {"answer": chain.invoke({"question": ask.prompt}).content}


@app.post("/agent/langchain-async")
async def langchain_agent_async(ask: Ask):
    """The same agent awaited in an async route, which is how most FastAPI applications run one."""
    out = await langchain_weather_agent().ainvoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


def service_chat_model():
    """An agent service builds its model like this: a ChatOpenAI subclass with an async HTTP client of its own."""
    import httpx
    from langchain_openai import ChatOpenAI

    class ChatLLM(ChatOpenAI):
        def __init__(self, **kwargs):
            kwargs.setdefault("http_async_client", httpx.AsyncClient(verify=True))
            kwargs.setdefault("openai_api_base", os.getenv("LLM_BASE_URL"))
            kwargs.setdefault("model", os.getenv("LLM_MODEL", "gpt-4o-mini"))
            kwargs.setdefault("api_key", os.getenv("API_KEY") or "sk-dummy")
            super().__init__(**kwargs)

    return ChatLLM()


async def get_weather_async(city: str) -> str:
    """Weather of a city."""
    return f"Sunny in {city}"


@app.post("/agent/langchain-unnamed")
async def langchain_unnamed(ask: Ask):
    """An agent service in miniature: `create_agent` without a name, an async tool, `ainvoke` in an async route."""
    from langchain.agents import create_agent
    from langchain.tools import tool

    os.environ.setdefault("LLM_BASE_URL", STUB + "/v1")
    agent = create_agent(
        service_chat_model(),
        tools=[tool(get_weather_async)],
        system_prompt="You are a helpful assistant. Be concise and accurate.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": ask.prompt}]}, config={"recursion_limit": 25}
    )
    return {"answer": result["messages"][-1].content}


@app.post("/agent/langgraph")
def langgraph_agent(ask: Ask):
    """LangGraph's prebuilt ReAct agent: langgraph and langchain-core, no `langchain` package."""
    import warnings

    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # deprecated in favour of langchain's create_agent, still what many apps run
        agent = create_react_agent(chat_model(), tools=[tool(get_weather)], name="react_agent")
    out = agent.invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


@app.post("/agent/langgraph-graph")
def langgraph_graph(ask: Ask):
    """A graph of the application's own: model node, tool node and a loop. A workflow, not an agent."""
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    tools = [tool(get_weather)]
    model = chat_model().bind_tools(tools)

    def call_model(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    def route(state: MessagesState):
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, ["tools", END])
    graph.add_edge("tools", "model")
    out = graph.compile(name="weather_graph").invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}
