# Coexistence and stand-down

The kit is built to run in clusters and processes that already have tracing. It never takes injection over from
another injector, and it never adds a second tracer to a process that has one: it stands down. In both cases the
reporter still lists the cluster's workloads.

## Beside another injector in the cluster

When **another injector already owns the cluster** (another OpenTelemetry Operator, the AWS CloudWatch Observability
add-on, Datadog or Dynatrace), `operator.install: auto` leaves the Operator, the webhooks and the `Instrumentation` out
and installs only the reporter and the gateway. The reporter then reports `mode: report-only` and `clusterOwner`, and
Cover your AI names the injector.

* The check reads the cluster's mutating webhook configurations when the chart is installed or upgraded. The installing
  user must be able to list them (a cluster admin can), or the check sees nothing and the Operator is installed.
* `--set operator.install=false` does the same on purpose; `--set operator.install=true` installs the Operator
  regardless.
* Under `helm template` (no cluster to read) the Operator is installed, and the cluster's ID must be given:
  `--set cluster.uid=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')`.
* **Pin `operator.install` after the first install**, to `true`, or to `false` where another injector should keep the
  cluster. `auto` looks at the webhook configurations on every upgrade, so an upgrade made after another injector
  arrived would take the Operator out.
* A pod that already carries an `instrumentation.opentelemetry.io/inject-*` annotation is never touched by the cover
  pod, nor are the workloads of another tracer or injector. The uninstall removes only annotations whose value names
  this install (`impact-ai/impact`), so another injector's stay.

Every language the Operator can inject other than Python and Node.js (Java, .NET, Go, Ruby, PHP, Apache HTTPD and
nginx) is switched off by name in the kit's Operator, so the kit's Operator answers no other language's annotation.

## Beside another tracer in the process

If a covered workload already has a tracer, the package **stands down** at process start: it takes nothing from the
process, and sends one `impact.setup` span with state `pre-existing` and what it found. The workload's own traces go
where they went before. Detection reads directory listings, the command line and the names of environment variables
only; it imports no candidate package.

What counts as a tracer of the workload's own, in Python:

* a launcher: `opentelemetry-instrument`, `ddtrace-run`, `newrelic-admin`, `splunk-py-trace`;
* an installed tracer distribution: Datadog (`ddtrace`, also when Datadog's injector puts it on `PYTHONPATH`),
  Traceloop and OpenLLMetry's per-provider packages, Langfuse, OpenLIT, Logfire, AgentOps, Arize Phoenix and Arize AX,
  OpenInference instrumentations, New Relic, Elastic APM and EDOT, Splunk, the AWS and Azure Monitor OpenTelemetry
  distributions, Honeycomb, and Impact AI's own SDK (`impact`): an application that uses it sends its traces itself;
* an OpenTelemetry pipeline of the workload's own: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
  `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` set by the workload, or a Zipkin or Jaeger
  exporter setting;
* LangSmith in its OpenTelemetry mode (`LANGSMITH_TRACING_MODE` or `LANGCHAIN_TRACING_MODE` set to `otel` or `hybrid`,
  or `OTEL_ONLY` / `OTEL_ENABLED` under either prefix). LangSmith's default tracing uses callbacks and leaves the
  OpenTelemetry provider alone, so the package starts beside it, and LangSmith receives the same runs as without the
  kit.

The Node.js preload stands down the same way when the workload starts a tracer of its own (Impact AI's SDK, `impact-ai`
or `@impact-ai/sdk`, among them), and so does the Java image's launcher when the JVM already runs an agent (an APM
vendor's or an OpenTelemetry agent) or sets up an exporter of its own.

The Operator's `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER`, `OTEL_EXPORTER_OTLP_PROTOCOL` and
`OTEL_SERVICE_NAME` defaults are suppressed (the `Instrumentation` defines them as optional references to a ConfigMap
key that does not exist, so Kubernetes leaves them out): ddtrace 4, for one, would otherwise export OTLP to
`localhost:4318` on seeing `OTEL_TRACES_EXPORTER=otlp`.

Where GKE's managed OpenTelemetry, Azure Monitor or the CloudWatch add-on already instruments a workload, the package
stands down for it and Impact AI receives no traces from that workload.

The setup report names the tool it found, its version, the kind of exporter its settings show and whether the
package started beside it ([how injection works](how-injection-works.md#the-tool-beside-the-package)). The package
starts beside a tracer only where the coexistence list it ships proves that tool's series; the lists are empty
today, so every tracer of the workload's own keeps the stand-down.

## How this is tested

* The chart leaves the Operator out beside each known injector, and an install beside Datadog names it:
  [`e2e/test_6_report_only.py`](../e2e/test_6_report_only.py).
* Datadog started in code and an `opentelemetry-instrument` entry point keep their own traces on a kind cluster:
  [`e2e/test_2_cover.py`](../e2e/test_2_cover.py).
* Stand-down in containers: [`python/tests/image/test_stand_down.py`](../python/tests/image/test_stand_down.py),
  [`nodejs/tests/image/test_nodejs_stand_down.py`](../nodejs/tests/image/test_nodejs_stand_down.py),
  [`java/tests/image/test_java_stand_down.py`](../java/tests/image/test_java_stand_down.py); the detection rules:
  [`python/tests/unit/test_detect.py`](../python/tests/unit/test_detect.py).
