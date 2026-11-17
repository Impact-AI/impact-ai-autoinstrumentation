# How injection works

The OpenTelemetry Operator that the chart installs injects Impact AI's instrumentation into a pod when the pod is created
with one annotation. The pod gets an init container that copies the instrumentation into a shared volume, and the
environment that loads it when the process starts. The workload's image, command and code are unchanged. The kit only
ever injects into pods; a running pod is never touched.

| Language | Annotation (on the pod template, or on the pod) |
|---|---|
| Python | `instrumentation.opentelemetry.io/inject-python: impact-ai/impact` |
| Node.js | `instrumentation.opentelemetry.io/inject-nodejs: impact-ai/impact` |

`impact-ai/impact` names the kit's `Instrumentation`: the release namespace and the resource. Two ways put the
annotation on a workload: the per-workload command, and automatic coverage.

## The per-workload command

The command Cover your AI shows for a workload annotates the pod template and so restarts the workload:

```sh
kubectl -n <namespace> patch deployment <name> --type merge \
  -p '{"spec":{"template":{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":"impact-ai/impact"}}}}}'
```

For a Python image based on Alpine (musl) the command also sets
`instrumentation.opentelemetry.io/otel-python-platform: musl`. For Node.js it sets `inject-nodejs` instead.

## Automatic coverage

With an install token the chart also runs the **cover pod** (`impact-ai-cover`; `cover.enabled`: `auto` follows the
token, `"true"` and `"false"` decide). Impact AI keeps the list of workloads to cover, from the inventory the reporter
sends; the cover pod asks for it with the install's key and an ETag (`GET <ingest.endpoint>/v1/inventory/cover`, every
`cover.pollInterval`, with backoff and `Retry-After`).

### Who decides, and what the cluster keeps

* **Impact AI's list decides which workloads are covered.** A workload on the list, in a namespace the kit covers, gets
  the instrumentation at its next rollout, and the instrumentation sends prompts and answers unless the workload turns
  content off ([what leaves the cluster](what-leaves-the-cluster.md)). A change to the list needs nothing from the
  cluster.
* **The list can add the kit's annotations and one label to a pod, and nothing else.** From each entry the cover pod
  takes the workload's namespace, kind and name, a language (`python` or `node`) and, optionally, one container name and
  an Alpine flag. It adds annotations from five keys: `instrumentation.opentelemetry.io/inject-python` or `inject-nodejs`
  (valued `<namespace>/impact`, the kit's own `Instrumentation`), `instrumentation.opentelemetry.io/container-names`,
  `instrumentation.opentelemetry.io/otel-python-platform` and `impact.ai/auto-covered` (the time), and the label
  `impact.ai/auto-covered=true`, which lets the uninstall find the pods it marked. It changes no image, no
  `Instrumentation`, no workload object and no other field of the pod: the code checks every operation of a patch
  against those keys before it answers (`ValidatePatch`), and it refuses a whole list that has one invalid entry, more
  than 2,000 entries or more than 1 MiB. An entry in a language this version does not inject is left out, and the cover
  pod's log counts such entries by language, so a list for a newer version still covers the workloads this one can.
* **The cluster keeps four controls.**
  1. The cluster's automatic-coverage setting in Cover your AI, chosen before the install command is shown. With it
     off, the list covers nothing.
  2. The label `impact.ai/opt-out` (any value) on a namespace or on a pod template, or the annotation `impact.ai/opt-out`
     on a pod template (any value but `false`). The webhook's selectors skip such namespaces and pods, and the list
     leaves such workloads out.
  3. `namespaces.include`, the namespaces the kit covers. Empty means all of them except the excluded ones; the system
     namespaces (`kube-system`, `kube-public`, `kube-node-lease`) and the namespace of this release are never covered,
     and `namespaces.exclude` leaves more out.
  4. `cover.enabled=false`: no cover pod and no webhook of its own. Workloads covered with the per-workload commands
     stay covered.
* **Behind an image mirror, ask for it.** `cover.enabled: auto` installs the cover pod only when no image mirror is set
  ([install options](install.md#an-image-mirror)).

### How a pod is covered

* A **mutating webhook** adds those annotations and that label to the **pods** of the listed workloads **as they are
  created**. The Operator, whose webhook sees the pod next, injects.
* **No workload object changes.** Nothing restarts, a running pod is never touched, and Argo CD or Flux have no drift
  to fight: a workload is covered at its **next rollout**. Restarting at once is a separate choice (the per-workload
  command).
* A pod that already carries an `inject-*` annotation is never touched, nor are workloads of another tracer or injector.
* **The pod has no Kubernetes permission and mounts no service account token**: the list arrives over HTTPS. Its
  certificate is made by the chart (no cert-manager) and reused on `helm upgrade`, so the CA bundle and the pod stay.
* **Order of the two webhooks.** The API server runs mutating webhook configurations in name order. `impact-ai-cover`
  sorts before `impact-ai-operator-mutation`, and the Operator's pod webhook has `reinvocationPolicy: IfNeeded`, so the
  injection happens exactly once whichever runs first (`cover.webhookConfigName` moves the name; the end-to-end tests do).
* **The reporter reports** the marker as `autoCovered` (the earliest among a workload's live pods) and the
  `impact.ai/*` labels of the workload, its pod template and its namespace as `labels` and `namespaceLabels`, so that
  Cover your AI can show what was covered and when, and, while a workload's pods do not start, `stall`: the reason of the
  Deployment's `ReplicaFailure` condition and the reasons the init container the Operator injects waits for
  (`FailedCreate`, `ImagePullBackOff`). Reasons only: a message can hold what a registry or a policy answered, so none is
  read. These fields are sent only when the cover pod is installed or an install token was used (`--cover-fields`), so a
  server that does not know them never sees them: **automatic coverage needs the matching impact.ai release.** Nothing
  else of a label leaves the cluster.

Covering adds the kit's init container at the next rollout of a listed workload. When the workload's nodes cannot pull
the kit's image, or an admission policy refuses the init container, that rollout stalls:
[failure modes](failure-modes.md#when-covering-a-workload-would-stall-its-rollout) says what happens and the three ways
out.

## In the process

If the workload already has a tracer (Datadog, Traceloop, an `opentelemetry-instrument` entry point, Langfuse, OpenLIT,
its own OpenTelemetry pipeline) the package **stands down** at process start: [coexistence](coexistence.md).

Otherwise, in a covered Python process the package starts with the process, imports nothing until the application
imports OpenAI, Anthropic, boto3 or a web framework, and exports on a thread of its own to the gateway, dropping spans
when the gateway or ingest is unreachable. The Node.js package is a preload that starts with the process and never
throws: every failure is caught and reported in the setup span.

### Model calls

OpenAI (chat and Responses), Anthropic (messages) and Amazon Bedrock (Converse, through plain `boto3`) calls arrive as
`chat` spans with prompt and answer, model, tokens and the workload's placement (the Kubernetes and cloud resource
attributes), under the request that made them when there is one. Bedrock is traced for any model ID; it is tested with
Anthropic Claude 3 Haiku and Amazon Nova Micro (`amazon.nova-micro-v1:0`) from a Starlette application on uvicorn. The
Bedrock span's `gen_ai.request.model` is the model ID without its vendor prefix (`nova-micro-v1:0`, as
`claude-3-haiku-20240307-v1:0` for Claude).

Gemini, through Google's SDK `google-genai`, arrives as `generate_content` spans with prompt and answer, model and
tokens, whether the client calls the Gemini API (`gen_ai.provider.name` `gemini`) or Vertex AI (`vertex_ai`), and
whether the call is sync, async or streaming (`opentelemetry-instrumentation-google-genai`, on the same content setting
as OpenAI's and Anthropic's). Tested against a mock of both APIs: the qualification's cells run each of the eight calls.
`google-genai` 3 is not traced yet (the instrumentation supports 1.32.0 to 2), nor is the older `vertexai` module of
`google-cloud-aiplatform`.

A library is traced only in the versions its qualification proves; [compatibility](compatibility.md#ai-libraries)
lists them, and what a workload with another version gets.

### Agents

A workload that runs an agent gets the agent step (`gen_ai.operation.name` `invoke_agent`, with `gen_ai.agent.name`), each
tool step (`execute_tool`, with the tool's name, arguments and result) and each model call (`chat`, one span per call,
with prompt and answer) in **one trace**, under the request that started it. Message and tool content follow
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` like the model calls do: `NO_CONTENT` keeps all of it off.

Tested on Python 3.12, 3.13 and 3.14 under uvicorn, with mock models (3.11 too with
`AGENTS_PYTHONS="3.11 3.12 3.13 3.14"`):

| Framework | Tested | Shows as |
|---|---|---|
| OpenAI Agents SDK | `openai-agents` 0.24.0, Responses and Chat Completions models | `invoke_workflow` > `invoke_agent` > `chat`, `execute_tool` |
| LangChain `create_agent` | `langchain` 1.4.7, `langchain-openai` 1.6.7, sync and async | `invoke_agent` > `chat`, `execute_tool` |
| LangGraph prebuilt agent | `langgraph` 1.2.19 with `langchain-core`, no `langchain` | `invoke_agent` > `chat`, `execute_tool` |
| A LangGraph graph of your own | `StateGraph` with a model node and a `ToolNode` | `invoke_workflow` > `chat`, `execute_tool` (a workflow is not an agent) |
| A LangChain agent service of your own | `create_agent` with no name, a `ChatOpenAI` subclass with its own async HTTP client, an async tool, `ainvoke` | `invoke_agent <workload>` > `chat`, `execute_tool` |

An agent the application did not name (`create_agent(model, tools=...)` without `name=`) gets the workload's name as
`gen_ai.agent.name`, so that every agent span says which agent it is; a name the application gave is never replaced.

Not traced yet, and listed as such by the setup report: LlamaIndex, CrewAI, MCP, Google ADK, Pydantic AI, AutoGen,
Agno and the other frameworks the package knows by name. Not tested: streaming, LangChain with chat models other than
`ChatOpenAI`, and servers other than uvicorn. The OpenAI Agents SDK creates no trace objects when its own tracing is off
(`OPENAI_AGENTS_DISABLE_TRACING`), so a workload that turned it off gets its model calls and no agent spans. The SDK's own
export to OpenAI keeps running next to ours; it is never replaced.

### Business context: user, session and use case

The kit cannot know who made a request; the application can, once it has authenticated it. One line of OpenTelemetry's
API puts that in the baggage of the request, and the Python and Node.js packages copy it onto every span they make in
that request, the model calls included, as these attributes:

| Baggage key | On the span |
|---|---|
| `user.id` | the user the request is for |
| `session.id` | the session |
| `gen_ai.conversation.id` | the conversation, where the application has one |
| `impact.use_case` | the use case, for example `returns assistant` |

In Python (the application depends on `opentelemetry-api`):

```python
from opentelemetry import baggage, context

ctx = baggage.set_baggage("user.id", user_id)
ctx = baggage.set_baggage("session.id", session_id, context=ctx)
ctx = baggage.set_baggage("impact.use_case", "returns assistant", context=ctx)
token = context.attach(ctx)
try:
    ...  # the request's model calls
finally:
    context.detach(token)
```

In Node.js (the application depends on `@opentelemetry/api` 1.x):

```js
const { context, propagation } = require('@opentelemetry/api');

const baggage = propagation.createBaggage({
  'user.id': { value: userId },
  'session.id': { value: sessionId },
  'impact.use_case': { value: 'returns assistant' },
});
await context.with(propagation.setBaggage(context.active(), baggage), () => handle(request));
```

* **Only these four keys.** No other key of the baggage is read. A value is text of at most 256 bytes of UTF-8, cut on
  a character boundary; an empty value and one that is not text are left off. An attribute the instrumentation sets
  itself when the span starts is kept.
* **IDs, not names.** The values leave the cluster on every span ([what leaves the cluster](what-leaves-the-cluster.md#spans)):
  use an opaque ID for the user and the session, not an e-mail address or a name.
* **Set them after authentication.** Take them from what the application authenticated. Baggage can also come from a
  caller, in the `baggage` header of a request, and a caller can name any user there, so by default the kit takes
  these four keys from no caller:
  * Python keeps the propagators the workload has (`OTEL_PROPAGATORS`, OpenTelemetry's `tracecontext,baggage` by
    default) and takes the four keys out of the baggage a caller sends. Every other key of it, and the trace context,
    arrive and pass on as before.
  * Node.js: the package registers W3C trace context only, so nothing of a caller's `baggage` header is read.

  A value the application sets in its process always wins. An internal service behind a gateway that sets these keys,
  and that trusts it, can keep a caller's values: the annotation `impact.ai/trust-caller-baggage: "true"` on the pod
  template (the chart passes it to the package as `IMPACT_TRUST_CALLER_BAGGAGE`; the pod restarts to take it). Python
  then leaves the four keys in a caller's baggage, and Node.js registers W3C baggage beside trace context, as
  OpenTelemetry does by default, so a caller's values reach the spans where the application sets none. Outside the
  chart, set `IMPACT_TRUST_CALLER_BAGGAGE=true` on the container. The annotation counts on the pod (template) only, not on its
  namespace: Kubernetes hands a container its own pod's annotations and nothing of the namespace.

Without the kit the lines change nothing the application does. Proven by `python/tests/unit/test_business_context.py`
and `nodejs/tests/unit/business-context.test.js`, and with the images by `python/tests/image/test_business_context.py`
(Python 3.11 and 3.14 under uvicorn, OpenAI) and `nodejs/tests/image/test_nodejs_business_context.py` (Node.js 24,
OpenAI through the kit's own provider and Anthropic through the provider it registers globally), against the mock ingest.

### The setup report

Every covered process sends one span named `impact.setup` when it starts, also when the package stands down. Its state
is `active`, `pre-existing` (it stood down) or `failed`, and it says why. It carries two string-array attributes:

* `impact.setup.frameworks`: the AI libraries and agent frameworks among the workload's installed distributions, with
  versions, for example `["langgraph 1.2.19", "openai-agents 0.22.3", "openai 3.42.1"]`. Boto3 is not listed: it is
  everywhere and says nothing about AI.
* `impact.setup.untraced`: those of them the image cannot trace (yet), for example `["crewai 1.15.35", "mcp 2.2.0"]`. A
  version the qualification does not prove is on the list from the first report
  ([compatibility](compatibility.md#ai-libraries)); one that is older than its instrumentation supports joins it when
  the workload imports the library (the report is sent again); and so do the Agents SDK with its tracing off and an
  instrumentation switched off with `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`.

The wire shape is fixed, because the Impact AI service drops an entry that does not fit it: OTLP `arrayValue` of
`stringValue`, never one joined string; an entry is `name` or `name version`, one space, each at most 64 characters; at
most 20 entries per list, agent frameworks before provider SDKs; both lists on every report, `[]` when there is nothing
to list. Both are read from the installed distributions by listing directories: nothing of the workload is imported or
run.

### The tool beside the package

When the package finds a tool it knows beside it, the `impact.setup` span (Python and Node.js) also names it:
`impact.setup.tool` (`datadog`, `traceloop`, `langfuse`, `openlit`, `logfire`, `phoenix`, `arize-ax`, `newrelic`,
`splunk-otel`, `honeycomb`, `elastic-edot`, `opentelemetry` or `langsmith`), `impact.setup.tool.version`,
`impact.setup.exporter` (`otlp-http`, `otlp-grpc`, `zipkin` or `console`, when the tool's settings show it) and
`impact.setup.alongside`, a boolean: true when the package started beside the tool, false when it stood down. These are
names, a version and a flag, never a setting's value. A tracer of no known tool (an OpenLLMetry or OpenInference
instrumentation on its own) is named only in the detail.

LangSmith's default tracing never touches OpenTelemetry, so the package starts beside it and reports `langsmith` with
`alongside` true when `LANGSMITH_TRACING` (or `LANGCHAIN_TRACING_V2`) is `true` and no other tracer is found. Beside a
tracer of no known tool it names no tool. Its OpenTelemetry mode (`LANGSMITH_OTEL_ENABLED`, `LANGSMITH_OTEL_ONLY`,
`LANGSMITH_TRACING_MODE`) stands the package down and reports `otlp-http` with `alongside` false; these settings name
LangSmith only when its package is installed. A mode switched on only in code is not visible at start.

Each package ships a coexistence list (`coexistence.json` beside its code, in the form of the qualification): a cell per
tool, Python version or Node.js major and version series of the tool, with what its proof found. The package starts
beside a tracer it found only where a passing `alongside` cell proves that tool's series for the workload: the package
started, traced the workload, and the tool's own backend recorded the same as without it. Any other tracer keeps the
stand-down. The lists are empty today.

 ### Node.js
 
 A Node.js workload that calls OpenAI or Anthropic is covered with the annotation `instrumentation.opentelemetry.io/inject-nodejs:

### Node.js

A Node.js workload that calls OpenAI or Anthropic is covered with the annotation
`instrumentation.opentelemetry.io/inject-nodejs: impact-ai/impact` (the per-workload command, or the cover pod). The
preload starts with the process and sends the same `impact.setup` span.

* **Versions.** Node.js 22.15 or later; on an older Node.js the application runs uninstrumented, and the setup report
  says why. Tested on Node.js 22, 24 and 26, as CommonJS and as ES modules, against OpenAI's SDK 4 to 7 and Anthropic's
  SDK 0.134 and 1.x ([`nodejs/qualification.json`](../nodejs/qualification.json)); the one combination that starts and
  is not traced, on each of the three, is OpenAI's SDK 4 loaded as an ES module. A workload can set
  `IMPACT_NODEJS_ESM=on` to try an ES module the file does not prove, or `off`.
* **Conventions.** OpenAI calls arrive in the OpenInference conventions (`input.value` and `output.value` hold the whole
  request and the answer as JSON); Anthropic calls arrive in the OpenTelemetry GenAI conventions.
* **Cost.** On Node.js 22, on one loaded 16-core host, the preload added 76 ms to `node -e 0` and 166 to 199 ms to an
  Express application's first answer. Memory and the overhead budget are in [compatibility](compatibility.md#overhead).
* **A gateway that does not answer**: [failure modes](failure-modes.md#in-a-covered-process).

## Log events

Some instrumentations write a model call's prompt and answer as OpenTelemetry **log events** next to its span: Java,
.NET and some Node.js instrumentations do, and so does Python's botocore instrumentation. The gateway forwards them to
`<ingest.endpoint>/v1/logs`, with the same key, gzip, retries and cluster attributes as spans, so that content written as
log events is kept with its call: Impact AI joins each event to its span by trace ID and span ID.

The kit's own packages send spans. A workload sends log events when its own OpenTelemetry instrumentation exports them
to the gateway, which takes OTLP over HTTP, protobuf or JSON, on port 4318 (the release namespace is `impact-ai` by
default):

```sh
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://impact-ai-gateway.impact-ai.svc:4318/v1/logs
```

The gateway forwards every log record it receives, so point at it the instrumentation that writes the model events.

## How this is tested

* The Operator's injection is reproduced with containers (`make test-image`, `make test-nodejs`): Python 3.11 to 3.14
  under uvicorn and gunicorn, Node.js 22, 24 and 26, against mock providers
  ([`python/tests/image/test_injected_apps.py`](../python/tests/image/test_injected_apps.py),
  [`nodejs/tests/image/`](../nodejs/tests/image/)).
* The agent frameworks: [`python/tests/image/test_agents.py`](../python/tests/image/test_agents.py) (`make test-agents`,
  on the Pythons of `AGENTS_PYTHONS`, default "3.12 3.13 3.14").
* The setup report's wire shape: [`python/tests/unit/test_setup_wire_shape.py`](../python/tests/unit/test_setup_wire_shape.py).
* Log events: [`python/tests/image/test_kit_gateway_logs.py`](../python/tests/image/test_kit_gateway_logs.py) (the
  pinned Collector with the chart's configuration, against the mock ingest) and
  [`e2e/test_2_logs.py`](../e2e/test_2_logs.py) (a pod of the kind cluster sends a log event to the gateway).
* Automatic coverage on a kind cluster, the stall of both causes and the snapshot that reports it:
  [`e2e/test_8_auto_cover.py`](../e2e/test_8_auto_cover.py).
