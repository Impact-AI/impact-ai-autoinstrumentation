# What leaves the cluster

Everything below goes to the ingest host (`ingest.endpoint`) over HTTPS, with the cluster's own key as the bearer.
Nothing else leaves the cluster, and nothing goes anywhere else.

## Content is captured by default

**By default a span of a model call carries the prompt, the answer, and the arguments and results of tool calls.** This
differs from OpenTelemetry's GenAI instrumentations, which capture no content unless asked. Setting
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` on a workload keeps all of it off, in Python and in
Node.js, for model calls, agents and tools alike. The chart has no cluster-wide setting for it: set the variable in the
workload's pod template.

With [automatic coverage](how-injection-works.md#automatic-coverage), Impact AI's list decides which workloads are
covered, so it decides which workloads send content unless they set the variable. The cluster's controls over that list
(the automatic-coverage setting, `impact.ai/opt-out`, `namespaces.include`, `cover.enabled=false`) are in
[how injection works](how-injection-works.md#who-decides-and-what-the-cluster-keeps).

## The inventory

The reporter sends it every `reporter.interval` (5 minutes by default) to `POST <ingest.endpoint>/v1/inventory`, as
`impact.inventory.v1`: names, never values.

* For each workload: its namespace, kind and name, the pods desired and ready, the service account's name, each
  container's name and image, the base name of each container's command, the names of its environment variables, the
  names of the Secrets and ConfigMaps it refers to (and the key names in them), and the annotations of the allowlist in
  the [security model](security-model.md#reporter) (`instrumentation.opentelemetry.io/*`, `impact.ai/*`,
  `admission.datadoghq.com/*`, `*dynatrace.com/*`, `cloudwatch.aws.amazon.com/*`).
* With automatic coverage: the marker `autoCovered`, the `impact.ai/*` labels of the workload, its pod template and its
  namespace, and the `stall` reasons of a workload whose pods do not start (reasons only, never a message)
  ([how injection works](how-injection-works.md#how-a-pod-is-covered)).
* For the cluster: its ID (the UID of `kube-system`), name, provider, region and Kubernetes version, and the cloud
  identity you gave ([install options](install.md#cloud-identity-optional)).
* When another injector owns the cluster: `mode: report-only` and `clusterOwner`.

Environment values, container arguments, Secret and ConfigMap contents and
`kubectl.kubernetes.io/last-applied-configuration` never leave the cluster. The reporter's role cannot read Secrets or
ConfigMaps, and the types it decodes API responses into have no field for a value.

## Spans

Spans of covered workloads, through the gateway, to `<ingest.endpoint>/v1/traces`, with the cluster's ID, name and cloud
identity added to the resource.

* A model call's span carries model, tokens and, by default, the content above.
* The URL of an HTTP server span includes its query string.
* Where the application puts them in baggage, a span also carries `user.id`, `session.id`,
  `gen_ai.conversation.id` and `impact.use_case`
  ([how injection works](how-injection-works.md#business-context-user-session-and-use-case)).
* In Node.js, OpenAI calls follow the OpenInference conventions (`input.value` and `output.value` hold the whole request
  and the answer as JSON, with the tools, the metadata and the user fields), and Anthropic calls follow the
  OpenTelemetry GenAI conventions.
* Every covered process sends one `impact.setup` span when it starts: its state, the AI libraries and frameworks it
  found with their versions, and those it does not trace
  ([how injection works](how-injection-works.md#the-setup-report)).
* Requests to the usual probe and metrics paths (`/health`, `/healthz`, `/livez`, `/readyz`, `/ping`, `/metrics` and the
  like) make no spans.

## Log events

Log events that a workload's own OpenTelemetry instrumentation sends to the gateway
([how injection works](how-injection-works.md#log-events)), to `<ingest.endpoint>/v1/logs`: each record with its body,
attributes, trace ID and span ID, and the cluster's ID, name and cloud identity added to its resource. A log event of a
model call carries the prompt and the answer where the instrumentation is set to capture message content
(`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`). The kit's packages send no log records, and a workload sends
none until its own instrumentation is pointed at the gateway. The gateway forwards every log record it receives.

## The key exchange and the last notice

* The install hook sends the token, the product, the cluster's ID, name and ARN, and only the SHA-256 hash and first
  characters of the key it made ([install options](install.md#the-install-token)).
* The cover pod asks for the list of workloads to cover with the install's key (`GET <ingest.endpoint>/v1/inventory/cover`)
  and sends nothing else.
* The uninstall hook sends the final `uninstalled` snapshot: the cluster object and no workloads.

## How this is tested

* No planted value reaches the mock ingest from a kind cluster, and the reporter cannot read Secrets or ConfigMaps:
  [`e2e/test_1_install.py`](../e2e/test_1_install.py).
* The reporter decodes into names only, and no secret reaches its output:
  [`reporter/internal/kube/types_test.go`](../reporter/internal/kube/types_test.go),
  [`reporter/internal/inventory/collector_test.go`](../reporter/internal/inventory/collector_test.go).
* A covered workload's spans arrive with provider, model, tokens, placement and content:
  [`e2e/test_2_cover.py`](../e2e/test_2_cover.py).
