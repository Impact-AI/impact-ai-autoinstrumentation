# Compatibility

What the kit supports, what CI tests, and the policy that changes both. "Tested" on this page means a test in this
repository runs it: in CI for every change, every day the code changes, or before every release, as each section says.

## Runtimes the kit instruments

The kit supports the versions of Python and Node.js that their own projects still support.

| Runtime | Supported by the kit | End of life upstream |
|---|---|---|
| Python | 3.11 | October 2027 |
| | 3.12 | October 2028 |
| | 3.13 | October 2029 |
| | 3.14 | October 2030 |
| Node.js | 22, from 22.15 | 30 April 2027 |
| | 24 | 30 April 2028 |
| | 26 | 30 April 2029 |

* Node.js 22.15 or later; on an older Node.js the application runs uninstrumented, and the setup report says why.
  Tested on 22, 24 and 26, CommonJS and ES modules.
* Node.js means its LTS lines, which is what OpenTelemetry JavaScript supports.
* Every change runs the container tests that reproduce the Operator's injection on each version in the table
  (`make ci`): Python under uvicorn and gunicorn, Node.js as CommonJS and as ES modules.
* A version leaves in the first kit release after its upstream end of life, and the release before announces it.
* A new version joins once OpenTelemetry supports it and our tests pass. OpenTelemetry Python adds a new Python version
  no later than three months after it is stable; Python 3.15 joins the kit then.
* Java: the repository builds and tests a Java image (the OpenTelemetry Java agent, unchanged, inside a launcher; spans
  tested on Java 17, 21 and 25), but releases do not publish it yet and the chart does not inject it.

## AI libraries

The Python image traces a library only where its qualification proves it: `python/qualification.json`, which the image
carries. Each of its cells is one Python version, one library and one major version of it (the minor, for a library
that versions by minor: `openai-agents` 0.24), and says what happened when a workload with that library ran injected
against mock providers: it started and served; each model call arrived as one span with its provider, model and
tokens; its content arrived on the span; and for an agent framework, its agent and tool steps arrived. Each library has
a cell at the oldest series the image traced before it had a qualification and one at its newest, and a version between
them is traced:

| Library | Versions traced | Python |
|---|---|---|
| `openai` | 1 to 3 | 3.11 to 3.14 |
| `anthropic` | 0.51 to 2 | 3.11 to 3.14 |
| Amazon Bedrock, through `botocore` | 1 | 3.11 to 3.14 |
| `openai-agents` | 0.3 to 0.24 | 3.11 to 3.14 |
| `langchain` | 0.3 to 1 (1 on Python 3.14, where LangChain 0.3 does not import) | 3.11 to 3.14 |
| `langchain-core` | 0.3 to 1 | 3.11 to 3.14 |
| `langgraph` | 0.2 to 1 | 3.11 to 3.14 |
| `google-genai`, the Gemini API and Vertex AI (sync, async and streaming) | 1 to 2 | 3.11 to 3.14 |

A release inside the proven range is traced without waiting for a cell of its own; an instrumentation's own floor still
applies inside it (`openai` from 1.26.0, `anthropic` from 0.51.0, `openai-agents` from 0.3.3, `langchain` from 0.3.22 or
`langchain-core` from 0.3.49, `google-genai` from 1.32.0). Any other version starts as it would without the kit for that
library (nothing of its instrumentation is loaded), the workload's other libraries stay traced, and the setup report
lists the library as untraced from its first report, with a line in `impact.setup.skipped` that says what Impact AI
traces and then what the workload runs, the way it says it of a Node.js below 22.15:

```text
openai: Impact AI traces openai 1 to 3; this workload runs openai 4.0.0
openai_agents: Impact AI traces openai-agents 0.3 to 0.24; this workload runs openai-agents 0.25.0
openai: Impact AI traces openai 1.26.0 to 3; this workload runs openai 1.20.0
```

(The name before the colon is the instrumentation's.) A library proven on some Pythons and not on the workload's reads
`Impact AI traces <library> on Python 3.11 to 3.13; this workload runs 3.14`.

LangChain, `langchain-core` and LangGraph have one instrumentation, so they are traced together, when each of them the
workload has is proven. A release that the qualification lists as excluded is not traced either. A cell that no longer
says what it did fails `make test-python-matrix`, and `make release` runs every cell before it pushes anything;
`make qualify-python` runs them again and rewrites the file. A weekly run on each library's newest release asks for a
cell when a new series passes, and for an exclusion when one fails.

Node.js: OpenAI's SDK 4 to 7 and Anthropic's SDK 0.134 and 1.x, on Node.js 22, 24 and 26, as CommonJS and as ES modules
([`nodejs/qualification.json`](../nodejs/qualification.json)). The one combination that starts and is not traced, on
each of the three, is OpenAI's SDK 4 loaded as an ES module.

Agent frameworks and the versions tested with them: [how injection works](how-injection-works.md#agents).

## Kubernetes

| | Versions |
|---|---|
| The chart installs on | 1.27 and later (`kubeVersion` in `Chart.yaml`), Linux nodes |
| Tested by CI | 1.27 and 1.37 (the end-to-end suite's Kubernetes part) and 1.34 (the whole suite), on kind, one amd64 node |
| Not tested | the versions between them, and every version after 1.37 |

* The chart installs on Kubernetes 1.27 and later. Each release is tested on Kubernetes 1.27, 1.34 and 1.37 before it
  is published, and the code it is made from every day it changes: the end-to-end suite on 1.34 and its Kubernetes
  part on 1.27 and 1.37.
* The Kubernetes project ended support for 1.34 on 27 October 2026. The versions it supports, and the cloud providers'
  extended support versions, are within the range the chart installs on; those not in the "Tested" row are not tested.
* The chart's `kubeVersion` floor is the oldest version CI tests. A version below it is refused at install.
* Helm 3.14 or later for `--reset-then-reuse-values`, and 3.18 or later for the reason of a refused token in the output
  of `helm`. The chart's render checks run with Helm 3.22, and one install of the end-to-end suite is made with Helm 4.3:
  with an install token, the same command again, and a fresh token that is refused.
* `make lint` scans the rendered chart with trivy at Kubernetes 1.27 and 1.34.

## Platforms

* The images are built for `linux/amd64` and `linux/arm64`, and every image of a release is scanned for both.
* **Tested** on kind, one node, amd64.
* **Not run yet** on Amazon EKS, Azure AKS, Google GKE or OpenShift, nor on arm64 nodes (the arm64 images are built and
  scanned). OpenShift's default security context constraint refuses the kit's fixed user IDs, so OpenShift is not
  supported.
* AKS Automatic, and AKS Safeguards in Enforce mode, are expected to deny the install and uninstall hooks (this has not
  been run): [security model](security-model.md#admission-rules-and-user-ids).
* GKE private clusters need a firewall rule for port 9443 (this has not been run on GKE):
  [security model](security-model.md#what-the-cluster-must-allow).

## Overhead

An injected workload costs at most **+1.5 ms of CPU per model call, +40 MiB of memory and +2 s to start** compared with
the same workload without injection. `make overhead` measures every runtime and server the images claim (Python 3.11 to
3.14 under uvicorn and gunicorn, Node.js 22, 24 and 26 under Express), each calling OpenAI, and for a library that
joined the traced set with cells of its own a workload that calls it (`google-genai`, on Python 3.12 under uvicorn):
three paired rounds each, the median of the differences held to the budget, a failing workload measured once more.
`make ci` runs a canary of two workloads, and every release measures all of them before it pushes anything and
publishes the numbers in the chart (`OVERHEAD.md` and the `impact.ai/overhead` annotation of `Chart.yaml`).

A Node.js workload's memory is read after both sides have collected all their garbage, because before a collection the
reading says how far V8's heap has grown and not what the instrumentation holds: V8's young generation grows by
doubling and does not shrink on its own, and two identical plain processes differ by -22 to +23 MiB (with ten times the
calls the two readings are within 3 MiB). The raw reading is kept in the record (`memory_unsettled_mib`) beside the one
judged. Five runs in a row put the median difference at 5.5 to 7.4 MiB on Node.js 22, 6.4 to 9.9 on 24 and 6.3 to 10.8
on 26.

## Versions

Releases use [Semantic Versioning](https://semver.org) version numbers. Before 1.0, a minor release may contain
breaking changes; each is listed first in the [changelog](../CHANGELOG.md) with its upgrade step, so pin `--version`.
Any removal is announced one minor release ahead. Security fixes go into the latest release only
([SUPPORT.md](../SUPPORT.md#security-fixes)).

## How the kit stays current

Dependency updates are automated in the development repository, weekly, one pull request per ecosystem: the base images
of every Dockerfile, pinned by digest (the digest and the patch version move; a new minor or major version of an image is
a decision, not an update), the Go modules of the reporter, the npm packages of the Node.js image, the Python packages
of the development environment, and the GitHub Actions of the workflows. CI decides whether an update merges, and a fix
reaches you in the next patch release. The OpenTelemetry Operator chart is vendored and pinned by
`scripts/vendor_operator_chart.py`; the Operator and Collector images are pinned by digest.
