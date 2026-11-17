# Impact AI auto-instrumentation for Kubernetes

One Helm release per cluster. It instruments the Python and Node.js workloads that call AI models, with no code change
and no new image, and sends their traces and a names-only inventory of the cluster's workloads to Impact AI.

- **Inventory.** Every 5 minutes the reporter sends each workload's namespace, kind, name and images, and the *names* of
  its environment variables and of the Secrets it refers to. Values never leave the cluster.
- **Traces, content on by default.** A covered workload's model-call spans carry the prompt, the answer, and the
  arguments and results of tool calls. Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` on a workload
  to send none of it. [What leaves the cluster][leaves] lists everything that is sent.
- **Coverage.** A workload is covered by one pod-template annotation, which a per-workload command adds, or which the
  kit's cover pod adds to new pods of the workloads Impact AI lists ([how injection works][injection]).

Python 3.11 to 3.14 (OpenAI, Anthropic, Amazon Bedrock Converse, the OpenAI Agents SDK, LangChain and LangGraph) and
Node.js 22.15 or later (OpenAI and Anthropic). On an older Node.js the application runs uninstrumented, and the setup
report says why. Supported versions and what CI tests: [compatibility][compatibility].

## Requirements

| Requirement | Value |
|---|---|
| Kubernetes | 1.27 or later (`kubeVersion`), Linux nodes. CI tests 1.27, 1.34 and 1.37 on kind, one amd64 node |
| Helm | 3.18 or later: 3.14 has `--reset-then-reuse-values`, 3.18 prints the reason for a refused install token. Tested with Helm 3.22 and 4.3 |
| Rights of the installing user | Create a CustomResourceDefinition, ClusterRoles, ClusterRoleBindings and mutating and validating webhook configurations, and list mutating webhook configurations: in practice `cluster-admin` |
| Egress | The kit's pods to the ingest host on 443; nodes to `ghcr.io` and `pkg-containers.githubusercontent.com`, or a mirror |
| In-cluster traffic | The API server to port 9443 in the release namespace (webhooks); covered pods to the gateway on 4318 |
| cert-manager | Not needed: the chart makes its webhook certificates and keeps them across upgrades |

[Security model][security] has the network flows in full, and the admission rules and user IDs a cluster must allow.

## Install

```sh
helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai --version <version> \
  --namespace impact-ai --create-namespace --wait \
  --set-string product.id=<product> \
  --set-string ingest.endpoint=<ingest origin, no path> \
  --set-string cluster.name=<name> \
  --set-string install.token=<one-time install token>
```

Cover your AI in Impact AI shows this command with the values filled in. It carries a one-time install token, valid for
15 minutes and for one use, never an ingest key: before anything else is installed, a Helm hook trades the token inside
the cluster for this cluster's own ingest key. The key is in no Helm value, no release record and no pod specification.
Cover your AI adds four optional values when it knows the cluster's cloud (it discovered the cluster).

The release is named `impact-ai` and lives in the namespace `impact-ai`; the `Instrumentation` resource is named `impact`
in that namespace. A cluster reports to one product: reinstalling the same product replaces the install, and installing
another product is refused with a message that names the first.

[Install options][install] covers the token exchange step by step, installing with a key you give (GitOps,
`ingest.existingSecret`), the cloud identity values, an image mirror and a new key after a revocation.

## What it installs

| Object | Purpose |
|---|---|
| `impact-ai-reporter` Deployment | Every 5 minutes sends the names-only inventory to `POST <ingest.endpoint>/v1/inventory` |
| `impact-ai-gateway` Deployment, Service | OpenTelemetry Collector (core distribution `otelcol`, by digest): receives spans from covered workloads and log events from workloads whose own OpenTelemetry instrumentation sends them, adds `k8s.cluster.uid` (and the cloud identity when given) to both, forwards spans to `<ingest.endpoint>/v1/traces` and log events to `<ingest.endpoint>/v1/logs` with the key |
| `impact-ai-operator` Deployment, webhooks, certificate | The OpenTelemetry Operator (vendored chart 0.128.0, Operator 0.162.0, by digest), Python and Node.js injection (every other language the Operator can inject, Java, .NET, Go, Ruby, PHP, Apache HTTPD and nginx, is switched off by name), **no cert-manager**: the chart makes the webhook certificate once and reuses it on every upgrade |
| `Instrumentation` `impact` | Tells the Operator to inject Impact AI's Python image into pods annotated `instrumentation.opentelemetry.io/inject-python: impact-ai/impact` (and the Node.js image for `inject-nodejs`) |
| `impact-ai-cover` Deployment, Service, webhook, certificate (with an install token) | Automatic coverage, see [how injection works][injection]. No Kubernetes permission |
| `impact-ai-exchange` hook (only while installing or upgrading with a token) | Trades the token for the cluster's own key and stores it in the Secret `impact-ai-install-key` |
| `impact-ai-uninstall` hook (only while uninstalling) | Stops the reporter and the cover pod, sends the final `uninstalled` snapshot, deletes the key Secret. It writes to no pod |
| CRD `instrumentations.opentelemetry.io` | Installed from `crds/`: Helm installs it first and never deletes it |

**What it asks of the cluster to run.** One Deployment each for the Operator, the gateway and the reporter, and with an
install token the cover pod; no DaemonSet and nothing privileged. Their requests add up to **90m CPU and 224 Mi** (the
Operator 20m and 64 Mi, the gateway 50m and 128 Mi, the reporter 20m and 32 Mi), and to **100m and 248 Mi** with the
cover pod (10m and 24 Mi). These are requests, not use. The memory limits are 256 Mi for the Operator and the gateway,
128 Mi for the reporter and 64 Mi for the cover pod. A covered pod's injected init container asks 10m CPU and 16 Mi while
the pod starts. The gateway's request and limit serve its spans and its log events together: one memory limiter and one
`GOMEMLIMIT` (80% of the limit) cover the whole process, and each of the two has a queue of its own
([failure modes][failures]).

When another injector already owns the cluster (another OpenTelemetry Operator, the AWS CloudWatch Observability
add-on, Datadog or Dynatrace), `operator.install: auto` installs only the reporter and the gateway
([coexistence][coexistence]).

## Values

Every value is described in [`values.schema.json`](values.schema.json): what it does, its default and when to change
it. `helm show values oci://ghcr.io/impact-ai/charts/impact-ai --version <version>` prints the defaults. The ones an
install usually sets or reviews:

| Value | Default | |
|---|---|---|
| `product.id` | required | the Impact AI product |
| `ingest.endpoint` | required | origin of the ingest host, no path; `https://` (the chart refuses `http://`) |
| `ingest.allowInsecureHttp` | `false` | for a test against a mock ingest with no certificate only: lets the endpoint be `http://`, which sends the key and the install token in clear |
| `install.token` | empty | the one-time install token of the Cover your AI command; the alternative to the two below. Helm, or a tool that runs Helm against the cluster, installs with it; a render without a cluster refuses it |
| `install.allowRenderWithoutCluster` | `false` | for the chart's own tests only: lets an install token render with no cluster to look in |
| `ingest.key` / `ingest.existingSecret` | empty (one of the three is required) | the ingest key, when you give it yourself ([install options][install]) |
| `cluster.name`, `cluster.uid` | empty | `name` is printable text of at most 128 characters; `uid` is looked up (kube-system's UID), give it when rendering without the cluster |
| `cloud.provider`, `cloud.accountId`, `cloud.region`, `cluster.arn` | empty | the cluster's cloud identity ([install options][install]) |
| `namespaces.include`, `namespaces.exclude` | empty | namespace names; the namespaces covered, and the ones left out. The system namespaces and the release's are never covered |
| `operator.install` | `auto` | `auto`, `"true"`, `"false"` ([coexistence][coexistence]) |
| `cover.enabled` | `auto` | the cover pod: `auto` (with an install token and no image mirror), `"true"`, `"false"` |
| `cover.timeoutSeconds` | `2` | seconds the API server waits for the cover pod's webhook, between 1 and 10 |
| `cover.replicas`, `cover.pollInterval`, `cover.webhookConfigName` | `1`, `1m`, `impact-ai-cover` | |
| `webhook.failurePolicy` / `webhook.timeoutSeconds` | `Ignore` / `5` | the Operator's webhooks; only `Ignore` is accepted |
| `global.imageRegistry` | empty | replaces the registry host of every image the chart names (a mirror), the Operator's `--collector-image` argument included |
| `images.pullPolicy`, `imagePullSecrets`, `nodeSelector`, `tolerations`, `priorityClassName` | `IfNotPresent`, empty | placement and pull settings of the gateway, the reporter, the cover pod and the hooks. The Operator takes its own: `operator.imagePullSecrets`, `operator.nodeSelector`, `operator.tolerations`, `operator.priorityClassName` and `operator.affinity` |
| `reporter.interval` | `5m` | how often the inventory is sent; at least `5s` |
| `reporter.resources` | requests 20m, 32Mi; limit 128Mi | |
| `gateway.replicas`, `gateway.resources` | `1`; requests 50m, 128Mi; limit 256Mi | |
| `cover.resources` | requests 10m, 24Mi; limit 64Mi | |
| `instrumentation.python.resources`, `instrumentation.nodejs.resources` | requests 10m, 16Mi; limits 200m, 64Mi | the init container injected into a covered pod |
| `images.python.*`, `images.nodejs.*`, `images.reporter.*` | written by the release | the kit's images, by tag and digest |
| `images.java.*` | empty | the Java instrumentation image; not injected yet, and written only by a release that publishes it |

A chart test fails on a value without a description, and on a value of `values.yaml` that the schema does not describe.

## Verify what you install

The release workflow signs every release with no key to keep (Sigstore keyless signing with the workflow's own
identity). The chart and the three images a release publishes, `autoinstrumentation-python`,
`autoinstrumentation-nodejs` and `kubernetes-reporter`, carry a [cosign](https://docs.sigstore.dev/cosign/)
signature; each image carries a signed SPDX bill of materials for each platform (`linux/amd64`, `linux/arm64`); and
GitHub records the build provenance (SLSA) of the chart and the three images. `autoinstrumentation-java` is built and
tested in this repository but not published: it ships, signed and attested the same way, from the first release whose
workflow sets `RELEASE_JAVA=1`. Until then the chart carries no digest for it, the chart does not inject it, and the
checks below skip it.

The commands check each artifact against the workflow's identity for the tag. They need `cosign` 2.6 or later, `helm`
and the [GitHub CLI](https://cli.github.com/). `make verify-release VERSION=v<version>` in a clone of this repository
runs the same checks ([`scripts/verify-release.sh`][verify-script]).

```sh
version=<version>   # the chart version, for example the one helm list shows
registry=ghcr.io/impact-ai
chart=oci://$registry/charts/impact-ai
identity="https://github.com/Impact-AI/impact-autoinstrumentation/.github/workflows/release.yml@refs/tags/v$version"
issuer=https://token.actions.githubusercontent.com

# The chart: its signature and its provenance, by the digest helm pulls.
digest=$(helm pull "$chart" --version "$version" 2>&1 | sed -n 's/^Digest: //p')
cosign verify "$registry/charts/impact-ai@$digest" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
gh attestation verify "oci://$registry/charts/impact-ai@$digest" --repo Impact-AI/impact-autoinstrumentation \
  --signer-workflow Impact-AI/impact-autoinstrumentation/.github/workflows/release.yml

# The digests of the images are in the signed chart:
helm show values "$chart" --version "$version" |
  awk '/^images:/ {in_images = 1; next} /^[^ #]/ {in_images = 0} in_images && /^  [a-z]+:$/ {image = $1} in_images && /^    digest: / {print image, $2}'

# For each image with a digest (the keys above are python, nodejs and reporter: the images autoinstrumentation-python,
# autoinstrumentation-nodejs and kubernetes-reporter): the signature, the signed bill of materials, the provenance.
cosign verify "$registry/autoinstrumentation-python@<digest>" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
cosign verify-attestation --type spdxjson "$registry/autoinstrumentation-python@<digest>" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
gh attestation verify "oci://$registry/autoinstrumentation-python@<digest>" --repo Impact-AI/impact-autoinstrumentation \
  --signer-workflow Impact-AI/impact-autoinstrumentation/.github/workflows/release.yml
```

An admission policy that enforces signatures (Kyverno `verifyImages`, the Sigstore policy-controller) takes the same
issuer and identity. Releases up to 0.3.0-rc1 were signed while this repository was named `Impact-AI/impact-ai-k8s`:
verify those with that name in the identity and in `--repo`. The gateway's collector and the Operator are the OpenTelemetry project's own images, pinned by
digest in the chart; verify them against that project's signatures.

## Upgrade

```sh
helm upgrade impact-ai oci://ghcr.io/impact-ai/charts/impact-ai --version <new version> \
  --namespace impact-ai --reset-then-reuse-values --wait
```

Read the [changelog][changelog] first. Before 1.0 a minor release may hold a breaking change; each is listed first, with
its upgrade step. Pin `--version`.

* **Use `--reset-then-reuse-values`, not `--reuse-values`.** A release pins its images by digest in the chart's own
  `values.yaml`. `--reuse-values` keeps the values computed for the last release, digests included, so the upgrade would
  run the old images. `--reset-then-reuse-values` takes the new chart's defaults and puts your own values on top of them.
  Passing all your `--set` flags again does the same.
* **A routine upgrade contacts nobody.** The token of the install stays in the release values and does nothing when the
  hook sees it again; the key stays in its Secret. Only an upgrade with a fresh token needs Impact AI to be reachable.
* **Apply the resource definition yourself when a release changes it.** Helm installs the files in `crds/` once and never
  upgrades them. The changelog says when `instrumentations.opentelemetry.io` changed; then run
  `helm show crds oci://ghcr.io/impact-ai/charts/impact-ai --version <new version> | kubectl apply --server-side --force-conflicts -f -`.
* **Pin `operator.install` after the first install**, to `true`, or to `false` where another injector should keep the
  cluster. `auto` looks at the cluster's webhook configurations on every upgrade, so an upgrade made after another
  injector arrived would take the Operator out.
* **The chart keeps the webhook certificates across upgrades.** It finds the earlier ones in the cluster. A tool that
  renders the chart without the cluster (`helm template`, which GitOps tools such as Argo CD use) cannot, so it makes new
  certificates at every render: the tool shows a diff at every sync, and pods created in the minute after a sync can
  start without the instrumentation (the webhook fails open) until the pods have the new certificate. Give such a
  render `cluster.uid`.
* **After `helm rollback`** to a revision that holds an older token, run a fresh command (a new token) instead of an
  upgrade with the old values: the hook would send that older token again, Impact AI refuses it because it was used, and
  the upgrade fails. The working key is not touched.

## Uninstall

`helm uninstall` alone leaves the marks the cover pod put on pods, the injection annotations the per-workload commands
added to workloads, and the namespace. The uninstall command Cover your AI shows is therefore these five steps, in this
order ([`scripts/uninstall-cluster-kit.sh`][uninstall-script] is the same text, and the end-to-end suite runs it). It
runs with the rights of the person who installed the kit: the pod patch of step 2 is made with yours, and no role of
the kit holds it.

```sh
# 1. The kit. A pre-delete hook first stops the reporter and the cover pod and waits until their pods are gone, and then
#    sends one final snapshot with mode "uninstalled" and no workloads, so Impact AI shows the cluster as not reporting
#    at once and no late snapshot of the old reporter flips it back. Last, it deletes the Secret that holds the ingest
#    key, which Helm does not own. The hook patches no pod.
helm uninstall impact-ai --namespace impact-ai --wait

# 2. The marks the cover pod put on the pods it annotated. They carry the label impact.ai/auto-covered, so the API server
#    finds them: the injection annotations, the marker and the label come off each pod, a metadata patch that restarts
#    nothing. Pods that have finished, and pods whose template you annotated yourself, are left alone.
STRIP='{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/inject-nodejs":null,"instrumentation.opentelemetry.io/container-names":null,"instrumentation.opentelemetry.io/otel-python-platform":null,"impact.ai/auto-covered":null},"labels":{"impact.ai/auto-covered":null}}}'
kubectl get pods --all-namespaces --selector impact.ai/auto-covered=true --field-selector 'status.phase!=Succeeded,status.phase!=Failed' -o jsonpath="{range .items[*]}{.metadata.namespace}{' '}{.metadata.name}{'\n'}{end}" |
  while read -r ns name; do
    kubectl --namespace "$ns" patch pod "$name" --type merge -p "$STRIP" ||
      echo "could not take the marks off pod $ns/$name (it may be gone); going on" >&2
  done

# 3. The annotations the per-workload commands added, for Python and for Node.js. Each covered workload restarts,
#    without injection. Only the kit's own annotations go: their value names this install, so another injector's stays.
for language in python nodejs; do
  if [ "$language" = python ]; then
    REMOVE='"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/otel-python-platform":null'
  else
    REMOVE='"instrumentation.opentelemetry.io/inject-nodejs":null'
  fi
  for kind in deployments statefulsets daemonsets; do
    kubectl get "$kind" --all-namespaces -o jsonpath="{range .items[?(@.spec.template.metadata.annotations['instrumentation\.opentelemetry\.io/inject-$language']=='impact-ai/impact')]}{.metadata.namespace}{' '}{.metadata.name}{'\n'}{end}" |
      while read -r ns name; do
        kubectl --namespace "$ns" patch "$kind" "$name" --type merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{$REMOVE}}}}}"
      done
  done
done
# (CronJobs the same way, under spec.jobTemplate.spec.template; the script has the loop.)

# 4. The namespace the install created.
kubectl delete namespace impact-ai --wait

# 5. The cluster-scoped objects of this release, by label. Helm removes them with the release; this catches what an
#    uninstall that was cut short leaves behind. The Operator's resource definition is not among them.
kubectl delete clusterrolebindings,clusterroles,mutatingwebhookconfigurations,validatingwebhookconfigurations --selector "app.kubernetes.io/part-of=impact-ai,app.kubernetes.io/instance=impact-ai" --ignore-not-found
```

What remains afterwards: **the Operator's resource definition `instrumentations.opentelemetry.io`** (Helm never deletes
definitions placed in `crds/`, and other tools in the cluster may use it). Nothing else: no webhook, collector, reporter,
cover pod, `Instrumentation` resource, ClusterRole, key Secret or injection annotation. Restarted pods are not injected,
and Impact AI shows the cluster as not reporting at once (the final snapshot) or, at the latest, two cycles after the
reporter stops. Pods that were covered keep what the Operator put into them (a running pod keeps its init containers as
they started) until they are replaced; they then send nothing, because their export to the gateway that is gone fails at
once and nothing waits for it. A release that moved from a token to a pasted key leaves the Secret `<release>-install-key`
behind, which step 4 deletes with the namespace.

If `helm uninstall` reports a failed pre-delete hook (for example the cluster cannot pull the reporter image), run it again
with `--no-hooks`: the kit is removed, only the final notice is skipped and Impact AI marks the cluster as not reporting
after two missed cycles. The key Secret stays until step 4 deletes the namespace; the marks come off the pods in step 2
either way. To remove the definition too: `kubectl delete crd instrumentations.opentelemetry.io`.

A release whose first install was refused has no key to wait for: the hook starts without the Secret, sends no notice and
ends at once, so `helm uninstall` of it is quick.

## Documentation

| Page | Covers |
|---|---|
| [Install options][install] | The install token step by step, giving the key yourself (GitOps), cloud identity, image mirrors, a new key |
| [How injection works][injection] | The annotation, automatic coverage, what is traced in Python and Node.js, agents, the setup report, log events |
| [Coexistence and stand-down][coexistence] | Beside another injector, beside another tracer in the process |
| [What leaves the cluster][leaves] | Every field sent, the content default and how to turn content off |
| [Failure modes][failures] | What happens to your workloads when a part of the kit or Impact AI is unavailable |
| [Security model][security] | RBAC of every component, network flows, admission rules, the install token's exposure |
| [Troubleshooting][troubleshooting] | Symptoms, causes and fixes, and what to put in a bug report |
| [Compatibility][compatibility] | Runtimes, libraries and Kubernetes versions supported and tested, and the support policy |

In a released chart these links point at the tag of that release; in this repository they point at `main`.

[install]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/install.md
[injection]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/how-injection-works.md
[coexistence]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/coexistence.md
[leaves]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/what-leaves-the-cluster.md
[failures]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/failure-modes.md
[security]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/security-model.md
[troubleshooting]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/troubleshooting.md
[compatibility]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/docs/compatibility.md
[changelog]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/CHANGELOG.md
[verify-script]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/scripts/verify-release.sh
[uninstall-script]: https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/scripts/uninstall-cluster-kit.sh
