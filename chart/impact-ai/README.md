# impact.ai cluster kit

One Helm release per cluster. It lists the cluster's workloads to impact.ai **by name, never by value**, and covers a
Python workload that calls OpenAI, Anthropic or Amazon Bedrock (Converse), or runs an agent built with the OpenAI Agents
SDK, LangChain or LangGraph, and a Node.js workload that calls OpenAI or Anthropic, with **one annotation: no code
change, no new image**. The annotation is added **automatically** to the pods of the workloads impact.ai lists, as they
are created, or by a command per workload. Customers get the commands from **Cover your AI** in impact.ai; this page is
for whoever reviews or operates the chart: what it installs, what the cluster must allow, what it may do there, what
leaves the cluster, and its limits.

## Install

```sh
helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai \
  --namespace impact-ai --create-namespace --wait \
  --set-string product.id=<product> \
  --set-string ingest.endpoint=<ingest origin, no path> \
  --set-string cluster.name=<name> \
  --set-string install.token=<one-time install token>
```

The command carries a **one-time install token**, never an ingest key: it is valid for 15 minutes and for one use. Before
anything else is installed, a Helm hook trades the token inside the cluster for **this cluster's own ingest key**
(see [The install token](#the-install-token)). Nobody copies, pastes or types a key, and the key is in no Helm value, no
release record and no pod specification.

Use Helm 3.18 or later: from 3.18 a refused token shows impact.ai's reason in the output of `helm`, where an older Helm
prints only that the hook failed. The kit is tested with Helm 3.22 and Helm 4.3 (see [Platforms](#platforms-and-tools)).

When Cover your AI knows the cluster's cloud (it discovered the cluster), the command adds four optional values, see
[Cloud identity](#cloud-identity-optional).

### Giving the key yourself

For GitOps and for a key you manage, leave `install.token` empty and give the key instead. Passing both is refused.

**GitOps.** A token install is made by Helm, or by a tool that runs Helm against the cluster, as Flux does: the exchange
and the uninstall hook are Helm hooks. A tool that renders the chart without the cluster (`helm template`, Argo CD,
`helm install --dry-run`) would apply them as plain objects, so the chart refuses `install.token` there: the uninstall
hook would run at install time (it stops the reporter and, with a token, deletes the key Secret), and under Argo CD a
failed sync deletes the key Secret. For GitOps give the key: create a Secret in the release namespace with the key
under `ingest-key`, set `ingest.existingSecret` to its name, and render with `helm template --no-hooks` (the hooks
belong to `helm install` and `helm uninstall`). Add `--set cluster.uid=...` when there is no cluster to look in. To
preview a token install, use `helm upgrade --install --dry-run=server`, which asks the cluster.

```sh
helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai \
  --namespace impact-ai --create-namespace \
  --set-string product.id=<product> --set-string ingest.endpoint=<ingest origin, no path> \
  --set-file ingest.key=/dev/stdin
```

The key goes in on standard input, so it stays out of shell history. It is kept in a Kubernetes Secret (and, like every
Helm value, in the release record); revoking the key in impact.ai stops this cluster only. To manage the Secret yourself,
create it with the key under `ingest-key` and set `ingest.existingSecret`. A key given this way belongs to no install, so
the cover pod has nothing to ask impact.ai for: automatic coverage needs the key an install token makes.

To use a new key (the old one was revoked or lost), run the same command again with it. The fingerprint of the key is
in the pod templates of the gateway and the reporter, which read the key when they start, so `helm upgrade --install`
replaces both and the cluster reports again at once, without anyone deleting a pod. With `ingest.existingSecret` the
fingerprint is that of the Secret the cluster holds when the command runs; change the Secret first.

The release is named `impact-ai` and lives in the namespace `impact-ai`; the `Instrumentation` resource is named `impact`
in that namespace. Reinstalling the same product replaces the install. Installing **another product** in a cluster that
already reports to one is refused, and the message names the first:

```text
this cluster is already connected to the impact.ai product "prod_a" (release impact-ai in namespace impact-ai). A cluster
reports to one product. Uninstall that release first (helm uninstall impact-ai -n impact-ai), then install again for
product "prod_b".
```

## Verify what you install

The release workflow of this repository signs every release, with no key to keep (Sigstore keyless signing, the
workflow's own identity): the chart and the three images `autoinstrumentation-python`, `autoinstrumentation-nodejs` and
`kubernetes-reporter` carry a [cosign](https://docs.sigstore.dev/cosign/) signature, each image carries a signed SPDX bill of materials for each platform (`linux/amd64`, `linux/arm64`), and
GitHub records the build provenance (SLSA) of all four. The commands check them against the workflow's identity for the
tag. They need `cosign` 2.6 or later, `helm` and the [GitHub CLI](https://cli.github.com/); `make verify-release
VERSION=v0.3.0` in a clone of this repository runs the same checks
([`scripts/verify-release.sh`](../../scripts/verify-release.sh)).

```sh
version=0.3.0
registry=ghcr.io/impact-ai
chart=oci://$registry/charts/impact-ai
identity="https://github.com/Impact-AI/impact-ai-k8s/.github/workflows/release.yml@refs/tags/v$version"
issuer=https://token.actions.githubusercontent.com

# The chart: its signature and its provenance, by the digest helm pulls.
digest=$(helm pull "$chart" --version "$version" 2>&1 | sed -n 's/^Digest: //p')
cosign verify "$registry/charts/impact-ai@$digest" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
gh attestation verify "oci://$registry/charts/impact-ai@$digest" --repo Impact-AI/impact-ai-k8s \
  --signer-workflow Impact-AI/impact-ai-k8s/.github/workflows/release.yml

# The digests of the images are in the signed chart:
helm show values "$chart" --version "$version" |
  awk '/^images:/ {in_images = 1; next} /^[^ #]/ {in_images = 0} in_images && /^  [a-z]+:$/ {image = $1} in_images && /^    digest: / {print image, $2}'

# For each of the three images, with its digest (the keys above are python, nodejs and reporter: the images
# autoinstrumentation-python, autoinstrumentation-nodejs and kubernetes-reporter): the signature, the signed bill of
# materials, the provenance.
cosign verify "$registry/autoinstrumentation-python@<digest>" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
cosign verify-attestation --type spdxjson "$registry/autoinstrumentation-python@<digest>" --certificate-identity "$identity" --certificate-oidc-issuer "$issuer"
gh attestation verify "oci://$registry/autoinstrumentation-python@<digest>" --repo Impact-AI/impact-ai-k8s \
  --signer-workflow Impact-AI/impact-ai-k8s/.github/workflows/release.yml
```

An admission policy that enforces signatures (Kyverno `verifyImages`, the Sigstore policy-controller) takes the same
issuer and identity. The collector of the gateway and the Operator are the OpenTelemetry project's own images, pinned
by digest in the chart; verify them against that project's signatures.

## What it installs

| Object | Purpose |
|---|---|
| `impact-ai-reporter` Deployment | Every 5 minutes sends the names-only inventory to `POST <ingest.endpoint>/v1/inventory` |
| `impact-ai-gateway` Deployment, Service | OpenTelemetry Collector (core distribution `otelcol`, by digest): receives spans from covered workloads, adds `k8s.cluster.uid` (and the cloud identity when given), forwards to `<ingest.endpoint>/v1/traces` with the key |
| `impact-ai-operator` Deployment, webhooks, certificate | The OpenTelemetry Operator (vendored chart 0.128.0, Operator 0.162.0, by digest), Python and Node.js injection, **no cert-manager**: the chart makes the webhook certificate once and reuses it on every upgrade |
| `Instrumentation` `impact` | Tells the Operator to inject impact.ai's Python image into pods annotated `instrumentation.opentelemetry.io/inject-python: impact-ai/impact` (and the Node.js image for `inject-nodejs`) |
| `impact-ai-cover` Deployment, Service, webhook, certificate (with an install token) | Automatic coverage, see [Automatic coverage](#automatic-coverage). No Kubernetes permission |
| `impact-ai-exchange` hook (only while installing or upgrading with a token) | Trades the token for the cluster's own key and stores it in the Secret `impact-ai-install-key` |
| `impact-ai-uninstall` hook (only while uninstalling) | Stops the reporter and the cover pod, sends the final `uninstalled` snapshot, deletes the key Secret. It writes to no pod |
| CRD `instrumentations.opentelemetry.io` | Installed from `crds/`: Helm installs it first and never deletes it |

**What it asks of the cluster to run.** One Deployment each for the Operator, the gateway and the reporter, and with an
install token the cover pod; no DaemonSet and nothing privileged. Their requests add up to **90m CPU and 224 Mi** (the
Operator 20m and 64 Mi, the gateway 50m and 128 Mi, the reporter 20m and 32 Mi), and to **100m and 248 Mi** with the
cover pod (10m and 24 Mi). These are requests, not use. The memory limits are 256 Mi for the Operator and the gateway,
128 Mi for the reporter and 64 Mi for the cover pod. A covered pod's injected init container asks 10m CPU and 16 Mi while
the pod starts.

When **another injector already owns the cluster** (another OpenTelemetry Operator, the AWS CloudWatch Observability add-on,
Datadog or Dynatrace) `operator.install: auto` leaves the Operator, the webhooks and the `Instrumentation` out and installs
only the reporter and the gateway; the reporter then reports `mode: report-only` and `clusterOwner`. The check reads the
cluster's mutating webhook configurations when the chart is installed or upgraded. `--set operator.install=false` does the
same on purpose, `--set operator.install=true` installs the Operator regardless. The installing user must be able to list
mutating webhook configurations (a cluster admin can), or the check sees nothing and the Operator is installed. Under `helm template` (no cluster to
read) the Operator is installed and the cluster's ID must be given: `--set cluster.uid=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')`.

## What the cluster must allow

A cluster without network policies or admission rules of its own needs to allow nothing. Where it has them, these are the
places the kit meets them.

| Flow | From | To | Port |
|---|---|---|---|
| Spans | covered pods, in any namespace | the gateway `impact-ai-gateway`, in the release namespace | 4318 |
| Webhooks | the control plane (the API server) | the Operator's webhook, and the cover pod's with automatic coverage, in the release namespace | 9443 |
| Ingest | the gateway, the reporter, the cover pod and the install and uninstall hooks | the ingest host (`ingest.endpoint`) | 443 |
| Cluster API | the reporter and the Operator | the API server (the `kubernetes` Service) | the API server's own (443 on most managed clusters) |

A default-deny network policy needs an allow rule for each of the four. A policy in the release namespace that keeps the
API server out of port 9443 is the one that slows pod creation down (see
[When part of the kit is unavailable](#when-part-of-the-kit-is-unavailable)).

* **GKE private clusters.** Google's firewall lets the control plane open connections to nodes and pods on ports 443 and
  10250 only. Both webhooks listen on 9443, so add the firewall rule Google describes for admission webhooks
  ([private clusters, "Add firewall rules for specific use cases"](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/private-clusters)):

  ```sh
  gcloud compute firewall-rules create impact-ai-webhooks \
      --action ALLOW --direction INGRESS \
      --source-ranges <the cluster's masterIpv4CidrBlock> \
      --rules tcp:9443 \
      --target-tags <the target of the cluster's gke-<cluster> firewall rules>
  ```

  Google documents that a webhook behind any other port is blocked without the rule. The kit would then install and
  inject no pod, and every pod creation in a covered namespace would wait for the webhook timeout (this has not been run
  on GKE). Private nodes also need Cloud NAT, or a mirror (below), to pull images.
* **Images.** The kit pulls from `ghcr.io` and its blob host `pkg-containers.githubusercontent.com`, or from a mirror:
  `global.imageRegistry` replaces the registry host of every image the kit runs. The injected image is pulled by **the
  covered workload's own pod**, with its node's credentials or the workload's own `imagePullSecrets`, so a mirror has to
  be open to every covered workload. One reference keeps its upstream address: the Operator's `--collector-image`
  argument, which the kit never uses (it creates no collector).
* **Admission rules.** The injected init container has to be admitted into every covered pod. It runs as user 65532
  with no privileges, asks 10m CPU and 16 Mi and is limited to 200m and 64 Mi (`instrumentation.python.resources` and
  `instrumentation.nodejs.resources`). A registry allowlist that leaves out the kit's images, a user-ID range that leaves
  out 65532, or a LimitRange whose largest limit-to-request ratio for CPU is below 20 (200m against 10m) refuses the
  pod. [Automatic coverage](#automatic-coverage) says what that does to a rollout.
* **User IDs.** The kit's pods run as the fixed user IDs 65532 (the reporter, the cover pod, the hooks and the injected
  init container) and 10001 (the gateway). A cluster that assigns user IDs by namespace range, as OpenShift's default
  security context constraint does, refuses them. OpenShift: planned.
* **Nodes.** Each pod's runtime shim uses inotify instances. On a node with many pods the Linux default of 128 per user
  (`fs.inotify.max_user_instances`) can run out, and the Operator then restarts with "too many open files"; raise the
  limit on such nodes.

## Cloud identity (optional)

Four optional values say which cloud the cluster runs in. They give spans an account and a region on nodes that block the
instance metadata service (where a workload's own detection finds nothing), and give the inventory the same. All default
to empty; a value that is empty adds nothing anywhere.

| Value | Span attribute (resource, added by the gateway) | `cluster` object of the inventory |
|---|---|---|
| `cloud.provider`: `aws`, `azure` or `gcp` | `cloud.provider` | `cloudProvider` |
| `cloud.accountId`: AWS account, Azure subscription or Google Cloud project | `cloud.account.id` | `accountId` |
| `cloud.region`: for example `eu-west-1` | `cloud.region` | `region` |
| `cluster.arn`: the provider's ID of the cluster, such as the EKS ARN | `cloud.resource_id` | `arn` |

```sh
  --set-string cloud.provider=aws --set-string cloud.accountId=012345678901 --set-string cloud.region=eu-west-1 \
  --set-string cluster.arn=arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks
```

Use `--set-string`: an account ID can start with a zero, and a number is refused rather than rewritten.

* **Spans**: the gateway adds each attribute only where the span's resource does not already have it, so a workload that
  detected its own account and region keeps them. `k8s.cluster.uid` is still always set by the gateway.
* **Inventory** (`impact.inventory.v1`): `cloudProvider`, `accountId` and `arn` are optional keys of `cluster`, left out
  when empty. `region` is the key that always existed (the nodes' `topology.kubernetes.io/region` label, or `null`); a
  given `cloud.region` replaces it. The final `uninstalled` snapshot carries the same values. `provider` (`eks`, `aks`,
  `gke` or `other`, read from the nodes) is unchanged.

## The install token

`install.token` is `impactinstall_<environment>_<43 characters>`. `helm upgrade --install` runs a **pre-install and
pre-upgrade hook**, the Job `impact-ai-exchange` (the reporter image, `reporter exchange`), before anything else is
created:

1. It makes the key with `crypto/rand` (`impact_<environment>_<43 characters>`, as impact.ai makes keys) and **stores it
   first**, as `pending-key` in the Secret `impact-ai-install-key`, beside the fingerprint of the token it belongs to.
   Helm made that Secret, empty, as a hook resource of its own just before the Job (only while the cluster has none);
   the Job reads and patches it, and deletes it when a first install is refused.
2. It sends impact.ai the token (as the bearer), the product, the cluster's ID, name and ARN, and **only the key's SHA-256
   hash** and its first characters. impact.ai never sees the key.
3. On a 200 the pending key becomes `ingest-key`. The gateway, the reporter and the cover pod read it from there.

The Job has a ServiceAccount and a Role of its own that may `get`, `patch` and `delete` that one Secret, by name, and
nothing else: no `create` (a role that holds it may make any Secret of the namespace), no `list`, no `watch`. It deletes
the Secret in one case, a refused first install, so that a refusal leaves nothing of the kit; it never deletes a Secret
that holds a working key. They, the Job and the Secret that carries the token to it (a mounted file, never the pod
specification) are deleted when the hook ends. Helm never deletes the Secret of the key, so it survives an upgrade;
`helm uninstall` removes it (its hook may `delete` that one Secret).

* **A lost answer costs one token, not two.** The key was stored before impact.ai was asked, so the retry sends the same
  hash and impact.ai answers `replayed: true`. The hook sends only after the key is stored, so a key that was not stored
  sends nothing and the token is not spent.
* **The same command again changes nothing.** `helm upgrade --reset-then-reuse-values` (see [Upgrading](#upgrading))
  hands the old token to the hook again; the Secret carries that token's fingerprint (`impact.ai/token`), so the hook
  does nothing and contacts nobody, and the pods' `checksum/key` (the same fingerprint) does not change. An upgrade with
  a **fresh** token makes a new key and needs impact.ai to be reachable.
* **A fresh command for a cluster that reports** (for example after the key was revoked) makes a new key. The working
  key stays in `ingest-key` until impact.ai has accepted the new one, so a command that is refused changes nothing.
  A new token rolls the gateway, the reporter and the cover pod, which read the key when they start.
* **A refused command** (the token was used, has expired, was cancelled, or is for another product or cluster) fails the
  hook, and Helm 3.18 or later prints the hook's log (`helm.sh/hook-output-log-policy`): the sentence impact.ai sent
  with the refusal, as it sent it, and one line about what is left. An older Helm prints only that the hook failed.
  Nothing was installed beyond what Helm applies before any hook: the namespace (`--create-namespace`) and the
  Instrumentation resource definition (`crds/`), which the uninstall keeps anyway. `helm uninstall` of the failed
  release works at once; the next command, with a fresh token, goes on from there.
* **After `helm rollback` to a revision that holds an older token**, run a fresh command (a new token) instead of an
  upgrade with the old values: the hook would send that older token again, impact.ai refuses it because it was used, and
  the upgrade fails. The working key is not touched.
* **Moving from a pasted key to a token** works on the same release: the Secret the hook makes has a name of its own
  (`<release>-install-key`), so Helm, which owns the Secret of a pasted key (`<release>-ingest-key`) and deletes it when
  it leaves the manifest, never touches the new key.

**Where the token can be read.** `install.token` is a Helm value: it is in the shell history and the process list of the
machine that runs `helm`, in `helm get values`, and in every release record Helm keeps (ten by default), which whoever can
read Secrets in the release namespace can read. It opens nothing once it has been used, or after 15 minutes. Helm writes
the first release record before the hook uses the token, so for those few seconds a reader of that record could use it
first: install from a machine and a namespace you trust. Exit codes of `reporter exchange`: 0 the key is in place (or
already was), 1 refused, 2 usage, 3 impact.ai could not be reached (run the same command again while the token is valid).

## Automatic coverage

With an install token the chart also runs the **cover pod** (`impact-ai-cover`; `cover.enabled`: `auto` follows the
token, `"true"` and `"false"` decide). impact.ai keeps the list of workloads to cover, from the inventory the reporter
sends; the cover pod asks for it with the install's key and an ETag (`GET <ingest.endpoint>/v1/inventory/cover`, every
`cover.pollInterval`, with backoff and `Retry-After`).

### Who decides, and what the cluster keeps

* **impact.ai's list decides which workloads are covered.** A workload on the list, in a namespace the kit covers, gets
  the instrumentation at its next rollout, and the instrumentation sends prompts and answers unless the workload turns
  content off ([What leaves the cluster](#what-leaves-the-cluster)). A change to the list needs nothing from the cluster.
* **The list can add the kit's annotations and one label to a pod, and nothing else.** From each entry the cover pod
  takes the workload's namespace, kind and name, a language (`python` or `node`) and, optionally, one container name and
  an Alpine flag. It adds annotations from five keys: `instrumentation.opentelemetry.io/inject-python` or `inject-nodejs`
  (valued `<namespace>/impact`, the kit's own `Instrumentation`), `instrumentation.opentelemetry.io/container-names`,
  `instrumentation.opentelemetry.io/otel-python-platform` and `impact.ai/auto-covered` (the time), and the label
  `impact.ai/auto-covered=true`, which lets the uninstall find the pods it marked. It changes no image, no
  `Instrumentation`, no workload object and no other field of the pod: the code checks every operation of a patch
  against those keys before it answers (`ValidatePatch`), and it refuses a whole list that has one invalid entry, more
  than 2,000 entries or more than 1 MiB.
* **The cluster keeps four controls.**
  1. The cluster's automatic-coverage setting in Cover your AI, chosen before the install command is shown. With it
     off, the list covers nothing.
  2. The label `impact.ai/opt-out` (any value) on a namespace or on a pod template, or the annotation `impact.ai/opt-out`
     on a pod template (any value but `false`). The webhook's selectors skip such namespaces and pods, and the list
     leaves such workloads out.
  3. `namespaces.include`, the namespaces the kit covers. Empty means all of them except the excluded ones; the system
     namespaces (`kube-system`, `kube-public`, `kube-node-lease`) and the namespace of this release are never covered,
     and `namespaces.exclude` leaves more out.
  4. `cover.enabled=false`: no cover pod and no webhook of its own. Workloads covered with the row commands stay covered.
* **Behind an image mirror, ask for it.** `cover.enabled: auto` installs the cover pod with an install token when no image
  mirror (`global.imageRegistry`) is set. The init container a covered pod gets is pulled from the mirror, and the
  covered workloads may not pull from it: set `cover.enabled` to `"true"` once they do.

### How a pod is covered

* A **mutating webhook** adds those annotations and that label to the **pods** of the listed workloads **as they are
  created**. The Operator, whose webhook sees the pod next, injects.
* **No workload object changes.** Nothing restarts, a running pod is never touched, and Argo CD or Flux have no drift
  to fight: a workload is covered at its **next rollout**. Restarting at once is a separate choice (the row's command).
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

### When covering a workload would stall its rollout

* **Covering adds the kit's init container at the next rollout of a listed workload.** That init container is pulled and
  admitted like any other. If the workload's nodes cannot pull the kit's image (a mirror the workload cannot reach, a
  registry that is down), or an admission policy refuses the init container (a registry allowlist, a user-ID range, a
  LimitRange ratio: [What the cluster must allow](#what-the-cluster-must-allow)), the new pods wait in
  `Init:ImagePullBackOff` or are refused at admission, and the rollout stalls. Pods that run keep running, and a rollout
  whose strategy allows unavailable pods (`maxUnavailable` above 0) stops after removing that many. Three ways out, each
  of which lets the next pod start without the init container: the label `impact.ai/opt-out` on the namespace (at once)
  or on the workload's pod template (from its next ReplicaSet), `namespaces.include` without the namespace, or
  `cover.enabled=false`. Pods that already wait restart without the instrumentation once the workload is no longer
  covered: delete them. The reporter names the reason in its next snapshot, `ImagePullBackOff` or `FailedCreate` (names
  only, never a message), within one reporter cycle (`reporter.interval`, 5 minutes by default). The end-to-end suite pins
  both causes and that snapshot (`e2e/test_8_auto_cover.py`).
* **The webhook fails open.** `failurePolicy: Ignore` and a short timeout: with no list, with automatic coverage off for
  the cluster, with the cover pod down or impact.ai out of reach (the last list is kept), pods start as they would. A
  revoked key stops it covering.
* **A network path that drops the API server's calls slows pod creation down.** The API server calls the cover webhook,
  and the Operator's, on port 9443 of the release namespace. With the cover pod down and its Service without endpoints it
  answers at once: pod creation does not wait. If a NetworkPolicy or a firewall keeps the API server out (on a GKE private
  cluster, a firewall rule for 9443 covers both webhooks), or the pod hangs, every pod creation in a covered namespace
  waits for the cover webhook (`cover.timeoutSeconds`, 2 seconds) and again for the Operator's (`webhook.timeoutSeconds`,
  5 seconds) before it starts uninjected. Allow that ingress, or set `cover.enabled` to `"false"`.

## Covering a workload

The command on the page annotates the pod template and restarts the workload:

```sh
kubectl -n <namespace> patch deployment <name> --type merge \
  -p '{"spec":{"template":{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":"impact-ai/impact"}}}}}'
```

For a Python image based on Alpine (musl) the row also sets `instrumentation.opentelemetry.io/otel-python-platform: musl`.
The workload's image, command and code are unchanged. If the workload already has a tracer (Datadog, Traceloop, an
`opentelemetry-instrument` entry point, Langfuse, OpenLIT, its own OpenTelemetry pipeline) the package **stands down** at
process start, takes nothing from the process and sends one `impact.setup` span with state `pre-existing` and what it
found; the workload's own traces go where they went before. The Operator's `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`,
`OTEL_LOGS_EXPORTER`, `OTEL_EXPORTER_OTLP_PROTOCOL` and `OTEL_SERVICE_NAME` defaults are suppressed (the `Instrumentation`
defines them as optional references to a ConfigMap key that does not exist, so Kubernetes leaves them out): ddtrace 4, for
one, would otherwise export OTLP to `localhost:4318` on seeing `OTEL_TRACES_EXPORTER=otlp`.

### Model calls

OpenAI (chat and Responses), Anthropic (messages) and Amazon Bedrock (Converse, through plain `boto3`) calls arrive as
`chat` spans with prompt and answer, model, tokens and the workload's placement, under the request that made them when
there is one. Bedrock is traced for any model ID; tested with Anthropic Claude 3 Haiku and Amazon Nova Micro
(`amazon.nova-micro-v1:0`, which a returns assistant calls from a Starlette app on uvicorn, in a thread of its
own, with nothing else in its environment: the injected-app tests in `python/tests/image/`). The Bedrock span's `gen_ai.request.model`
is the model ID without its vendor prefix (`nova-micro-v1:0`, as `claude-3-haiku-20240307-v1:0` for Claude).

### Agents

A workload that runs an agent gets the agent step (`gen_ai.operation.name` `invoke_agent`, with `gen_ai.agent.name`), each
tool step (`execute_tool`, with the tool's name, arguments and result) and each model call (`chat`, one span per call,
with prompt and answer) in **one trace**, under the request that started it. Message and tool content follow
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` like the model calls do: `NO_CONTENT` keeps all of it off.

Proven by `python/tests/image/test_agents.py` (`make test-agents`, and the end-to-end job) on Python 3.12, 3.13 and 3.14
under uvicorn, with mock models (3.11 too with `AGENTS_PYTHONS="3.11 3.12 3.13 3.14"`):

| Framework | Tested | Shows as |
|---|---|---|
| OpenAI Agents SDK | `openai-agents` 0.22.3, Responses and Chat Completions models | `invoke_workflow` > `invoke_agent` > `chat`, `execute_tool` |
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

### What the package found

The `impact.setup` span carries two string-array attributes, on every start and also when the package stands down:

* `impact.setup.frameworks`: the AI libraries and agent frameworks among the workload's installed distributions, with
  versions, for example `["langgraph 1.2.19", "openai-agents 0.22.3", "openai 3.42.1"]`. Boto3 is not listed: it is
  everywhere and says nothing about AI.
* `impact.setup.untraced`: those of them the image cannot trace (yet), for example `["crewai 1.15.35", "mcp 2.2.0"]`. A
  library outside the supported range joins the list when the workload imports it (the report is sent again), and so do
  the Agents SDK with its tracing off and an instrumentation switched off with `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`.

The wire shape is fixed, because the impact.ai service drops an entry that does not fit it (pinned by
`python/tests/unit/test_setup_wire_shape.py`): OTLP `arrayValue` of `stringValue`, never one joined string; an entry is
`name` or `name version`, one space, each at most 64 characters; at most 20 entries per list, agent frameworks before
provider SDKs; both lists on every report, `[]` when there is nothing to list.

Both are read from the installed distributions by listing directories: nothing of the workload is imported or run.

### Node.js

A Node.js workload that calls OpenAI or Anthropic is covered with the annotation `instrumentation.opentelemetry.io/inject-nodejs:
impact-ai/impact` (the row's command, or the cover pod). The preload starts with the process and sends the same
`impact.setup` span.

* **Versions.** Node.js 22.15 or later; an older Node starts the application as without the kit, and the setup report
  says why. Tested on Node.js 22, 24 and 26, as CommonJS and as ES modules, against OpenAI's SDK 4 to 7 and Anthropic's
  SDK 0.134 and 1.x
  ([`nodejs/qualification.json`](../../nodejs/qualification.json)); the one combination that starts and is not traced, on
  each of the three, is OpenAI's SDK 4 loaded as an ES module. A workload can set `IMPACT_NODEJS_ESM=on` to try an ES
  module the file does not prove, or `off`.
* **A gateway that does not answer.** A hung gateway, or one whose packets are dropped, delays the end of a Node.js
  process by at most 5 seconds (2.5 measured), and the process holds at most 16 MiB of unsent spans, dropping the oldest
  first. In a test, a pod limited to 512 MiB that made 100 spans a second of 100 kB each ran for two minutes against a
  gateway that did not answer and grew by 28 MB. A DNS lookup for the gateway's name cannot be interrupted: if the
  cluster's DNS itself hangs, a process can wait about 20 seconds at exit.
* **Conventions.** OpenAI calls arrive in the OpenInference conventions (`input.value` and `output.value` hold the whole
  request and the answer as JSON); Anthropic calls arrive in the OpenTelemetry GenAI conventions.
* **Cost.** On Node.js 22, on one loaded 16-core host, the preload added 76 ms to `node -e 0` and 166 to 199 ms to an
  Express application's first answer. Its memory is measured after a garbage collection in both processes, because
  identical plain processes differ by -22 to +23 MiB before one; five runs in a row put the median difference at 5.5 to
  7.4 MiB on Node.js 22, 6.4 to 9.9 on 24 and 6.3 to 10.8 on 26, against a budget of +40 MiB that is unchanged. Every
  release measures its own overhead against the budget and keeps the raw readings in its records (`OVERHEAD.md` in the
  chart has the method).

## What leaves the cluster

Everything below goes to the ingest host (`ingest.endpoint`) over HTTPS, with the cluster's own key as the bearer.

* **The inventory** (the reporter, every `reporter.interval`): names, never values. For each workload its namespace, kind
  and name, the pods desired and ready, the service account's name, each container's name and image, the base name of
  each container's command, the names of its environment variables, the names of the Secrets and ConfigMaps it refers to
  (and the key names in them), and the annotations of the allowlist in [Permissions](#permissions). For the cluster its ID,
  name, provider, region and Kubernetes version, and the cloud identity you gave. Environment values, container
  arguments, Secret and ConfigMap contents and `kubectl.kubernetes.io/last-applied-configuration` never leave the cluster.
* **Spans** of covered workloads, through the gateway, with the cluster's ID, name and cloud identity added. **By default
  a span of a model call carries the prompt, the answer, and the arguments and results of tool calls.** Setting
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` on a workload keeps all of it off, in Python and in
  Node.js. The URL of an HTTP server span includes its query string. In Node.js, OpenAI calls follow the OpenInference
  conventions (`input.value` and `output.value` hold the whole request and the answer as JSON, with the tools, the
  metadata and the user fields), and Anthropic calls follow the OpenTelemetry GenAI conventions.
* **The key exchange and the last notice.** The install hook sends the token, the product, the cluster's ID, name and ARN,
  and only the SHA-256 hash and first characters of the key it made; the uninstall hook sends the final `uninstalled`
  snapshot.

## When part of the kit is unavailable

Your running workloads keep serving when the webhook, the Operator or ingest is unavailable. Some cases hold up the new
pods of covered workloads; they are in the table with the others. The end-to-end suite runs these cases: the Operator
down, the cover pod down, ingest down, a revoked key, an image that cannot be pulled, and (from Kubernetes 1.30) a
policy that refuses the init container. The others follow from the webhook settings and the init container and are not
run: a webhook that is slow, a webhook the API server cannot reach, and a copy that fails in the init container.

Every pod creation in a covered namespace calls the Operator's pod webhook, and with automatic coverage the cover pod's
before it. The Operator's webhooks have `failurePolicy: Ignore` and `timeoutSeconds: 5` (`webhook.timeoutSeconds`); the
cover pod's has `failurePolicy: Ignore` and `timeoutSeconds: 2` (`cover.timeoutSeconds`); only `Ignore` is accepted. The
system namespaces and the release namespace are never called, `namespaces.exclude` leaves more out, and
`namespaces.include` narrows the calls to the namespaces you list.

| What is unavailable | What happens to your workloads |
|---|---|
| The webhook or the Operator (down, slow) | New pods start without the instrumentation (`failurePolicy: Ignore`, `webhook.timeoutSeconds` 5). Running pods are not touched. |
| The cover pod (automatic coverage) is down | Pod creation does not wait: the API server finds no endpoint and skips its webhook, and new pods start. Workloads are covered again once it is back. |
| The API server cannot reach the cover pod or the Operator (a NetworkPolicy, a GKE private cluster's firewall on 9443), or a webhook hangs | Every pod creation in a covered namespace waits for the timeout of each webhook that cannot be reached (2 seconds for the cover pod's, 5 for the Operator's; usually both, because both listen on 9443 in the release namespace), then starts without the instrumentation. |
| Ingest, or the key is revoked | The reporter and the gateway retry and drop what they cannot send; the workloads keep serving. |
| **The image the Operator injects cannot be pulled** (the registry is unreachable, the image was removed, a mirror is closed to the workload) | New pods of covered workloads wait in `Init:ImagePullBackOff` until the image can be pulled, and a rollout or a scale-out of a covered workload stalls until then. Pods that run keep running, and so do new pods on nodes that already hold the image. A rollout whose strategy allows unavailable pods (`maxUnavailable` above 0) stalls after removing that many. With automatic coverage, the next rollout of a listed workload is such a rollout ([Automatic coverage](#when-covering-a-workload-would-stall-its-rollout)). |
| **An admission policy refuses the injected init container** (a registry allowlist, a user-ID range, a LimitRange ratio) | The new pods of covered workloads are refused at admission and the rollout stalls in the same way, until the policy allows the init container or the workload is left out of coverage. Pods that run keep running. |
| The copy in the injected init container fails (for example a full node disk) | The pod waits in Init (`Init:Error`, then `Init:CrashLoopBackOff`) while the copy is retried. |

The injected image is pulled by the kubelet for **the covered workload's** pod: with the node's credentials, or with the
`imagePullSecrets` of the workload's own pod. The kit's `imagePullSecrets` value applies to the kit's own pods. A mirror
(`global.imageRegistry`) therefore has to be open to every covered workload.

In a covered Python process the package starts with the process, imports nothing until the application imports OpenAI,
Anthropic, boto3 or a web framework, and exports on a thread of its own, dropping spans when the gateway or ingest is
unreachable. If the gateway does not answer, a covered process, in Python or in Node.js, waits at most 5 seconds for it
at exit and holds at most 16 MiB of unsent spans, dropping the oldest first. A DNS lookup for the gateway's name cannot
be interrupted: if the cluster's DNS itself hangs, a process can wait about 20 seconds at exit. Nothing the kit runs is
privileged: every pod runs as non-root with a read-only root file system and no capabilities.

The gateway keeps spans in a queue in memory (2,000 batches) and retries a batch for up to 120 seconds. A restart of the
gateway, or an ingest outage longer than 120 seconds, drops the spans that were queued.

## Permissions

Every role of the kit is pinned, install shape by install shape, in `chart/tests/test_chart_roles.py`: a new right has to
change that file, so it is argued for in review. **No long-lived role of the kit reads a Secret.** Two short-lived rights
touch one Secret, the one that holds the cluster's ingest key: the install hook may `get`, `patch` and `delete` it while
`helm install` or `helm upgrade` with a token runs (it deletes it only when a first install is refused), and the
uninstall hook may `delete` it while `helm uninstall` runs. Both name that Secret in `resourceNames`; no rule of the kit
can `create`, `list`, `watch` or `update` a Secret. The trivy check KSV-0113 ("manage namespace secrets", MEDIUM)
reports exactly those two roles, and this is the reason it stays: they are the least that lets the cluster store its key
before impact.ai is asked (`get`, `patch`) and remove it when an install is refused or the kit is uninstalled
(`delete`), they name the one Secret, and they exist only while a hook runs.

* **Reporter** (ClusterRole `impact-ai-kit-reporter`): `get`, `list` on namespaces; `list` on nodes, pods, deployments,
  statefulsets, daemonsets, cronjobs, jobs and mutating webhook configurations. **Never Secrets or ConfigMaps**, and it
  decodes only the fields it sends: environment variable names, `command[0]`'s base name, Secret *references* by name,
  and annotations from an allowlist (`instrumentation.opentelemetry.io/*`, `impact.ai/*`, `admission.datadoghq.com/*`,
  `*dynatrace.com/*`, `cloudwatch.aws.amazon.com/*`). Environment values, container arguments, Secret and ConfigMap
  contents and `kubectl.kubernetes.io/last-applied-configuration` never leave the cluster.
* **Operator** (ClusterRole `impact-ai-operator-manager` and a Role in the release namespace, both written by this chart;
  the upstream chart's role, made for collectors the kit does not install, is not used). The ClusterRole holds `get`,
  `list` and `watch` on namespaces, ReplicaSets, Jobs and EndpointSlices (the pod webhook names the workload that owns a
  pod; at start the Operator looks up the API server's address); `get`, `list`, `watch`, `update` and `patch` on
  `Instrumentation` resources, and `get`, `update`, `patch` on their status; `create` and `patch` on events; `create` on
  TokenReviews and SubjectAccessReviews (its metrics endpoint checks its callers). The Role holds `create`, `get`,
  `list` and `update` on Leases (leader election). The Operator changes `Instrumentation` resources, events and its own
  Lease, and nothing else: it creates, changes and deletes no pod, workload, Service or policy, and it has no rule for
  Secrets or ConfigMaps. The role is the same on every Kubernetes version.
  `make lint` scans the rendered chart with trivy at Kubernetes 1.27 and 1.34 (no HIGH or CRITICAL finding), and the
  end-to-end suite asks the API server what the Operator may do and reads the Operator's log for a "forbidden".
* **Install hook** (exists only while `helm install` or `helm upgrade` with a token runs): a Role in the release
  namespace that may `get`, `patch` and `delete` the one Secret named `<release>-install-key`, which Helm made empty
  before the hook; it deletes it only when a first install is refused. No `create`, no `list`, no `watch`, no other
  Secret, no cluster-wide permission. Deleted with the hook.
* **Cover pod**: none. Its service account has no binding and mounts no token.
* **Uninstall hook** (exists only while `helm uninstall` runs): the reporter's read rules, plus `patch` on
  `deployments/scale` of the reporter Deployment alone (and of the cover pod's, when there is one), and `delete` on the
  one key Secret (no `get`, `list` or `watch`: it can read no Secret). It writes to no pod, and no role of the kit holds a
  write verb on pods; the uninstall command takes the marks off the pods with your own rights.

## Platforms and tools

* **Kubernetes 1.27 and later**, on Linux nodes (`kubeVersion` in `Chart.yaml`). The images are built for amd64 and arm64.
* **Tested** on kind, one node, amd64: the end-to-end suite runs on Kubernetes 1.34, and its Kubernetes part on 1.27, the
  oldest version the chart supports. The chart's render checks run with Helm 3.22, and one install of the suite is made
  with Helm 4.3: with an install token, the same command again, and a fresh token that is refused.
* **Not run yet** on Amazon EKS, Azure AKS, Google GKE or OpenShift, nor on arm64 nodes (the arm64 images are built and
  scanned). OpenShift: planned.
* **Helm 3.14 or later** for `--reset-then-reuse-values` ([Upgrading](#upgrading)), and **3.18 or later** for the reason of
  a refused token in the output of `helm`.
* **Beside another injector** the kit stands down. With another OpenTelemetry Operator, the AWS CloudWatch Observability
  add-on, Datadog or Dynatrace in the cluster, `operator.install: auto` installs the reporter and the gateway only. Where
  GKE's managed OpenTelemetry, Azure Monitor or the CloudWatch add-on already instruments a workload, the package stands
  down for it and impact.ai receives no traces from that workload.
* **AKS Automatic**, and AKS Safeguards in Enforce mode, are expected to deny the install and uninstall hooks (their pods
  have no probes and no CPU limit); this has not been run. There, install with `ingest.key`, which also leaves automatic
  coverage off, and uninstall with `--no-hooks`.
* **Clusters that add a node for a new pod** (GKE Autopilot, Karpenter): the install hook is the first pod of an install
  and may wait for its node. Its Job allows 4 minutes. If it runs out, run the same command again: the key was stored
  before impact.ai was asked, so the retry costs no second token.
* **Installs that give the key** (`ingest.key` or `ingest.existingSecret`, as GitOps tools do) get no automatic coverage;
  cover workloads with the row commands.

## Upgrading

```sh
helm upgrade impact-ai oci://ghcr.io/impact-ai/charts/impact-ai --version <new version> \
  --namespace impact-ai --reset-then-reuse-values --wait
```

* **Use `--reset-then-reuse-values`, not `--reuse-values`.** A release pins its images by digest in the chart's own
  `values.yaml`. `--reuse-values` keeps the values computed for the last release, digests included, so the upgrade would
  run the old images. `--reset-then-reuse-values` takes the new chart's defaults and puts your own values on top of them.
  Passing all your `--set` flags again does the same.
* **A routine upgrade contacts nobody.** The token of the install stays in the release values and does nothing when the
  hook sees it again; the key stays in its Secret. Only an upgrade with a fresh token needs impact.ai to be reachable.
* **Apply the resource definition yourself when a release changes it.** Helm installs the files in `crds/` once and never
  upgrades them. The release notes say when `instrumentations.opentelemetry.io` changed; then run
  `helm show crds oci://ghcr.io/impact-ai/charts/impact-ai --version <new version> | kubectl apply --server-side --force-conflicts -f -`.
* **Pin `operator.install` after the first install**, to `true`, or to `false` where another injector should keep the
  cluster. `auto` looks at the cluster's webhook configurations on every upgrade, so an upgrade made after another
  injector arrived would take the Operator out.
* **The chart keeps the webhook certificates across upgrades.** It finds the earlier ones in the cluster. A tool that
  renders the chart without the cluster (`helm template`, which GitOps tools such as Argo CD use) cannot, so it makes new
  certificates at every render: the tool shows a diff at every sync, and pods created in the minute after a sync can
  start without the instrumentation (the webhook fails open) until the pods have the new certificate. Give such a
  render `cluster.uid`.
* **After `helm rollback`** to a revision that holds an older token, run a fresh command: see
  [The install token](#the-install-token).

## Values

| Value | Default | |
|---|---|---|
| `product.id` | required | the impact.ai product |
| `ingest.endpoint` | required | origin of the ingest host, no path; `https://` (the chart refuses `http://`) |
| `ingest.allowInsecureHttp` | `false` | for a test against a mock ingest with no certificate only: lets the endpoint be `http://`, which sends the key and the install token in clear |
| `install.token` | | the one-time install token of the Cover your AI command; the alternative to the two below. Helm, or a tool that runs Helm against the cluster, installs with it; a render without a cluster refuses it |
| `install.allowRenderWithoutCluster` | `false` | for the chart's own tests only: lets an install token render with no cluster to look in |
| `ingest.key` / `ingest.existingSecret` | required (one of the three) | the ingest key, when you give it yourself |
| `cluster.name`, `cluster.uid` | empty | `name` is printable text of at most 128 characters; `uid` is looked up (kube-system's UID), give it when rendering without the cluster |
| `cloud.provider`, `cloud.accountId`, `cloud.region`, `cluster.arn` | empty | the cluster's cloud identity, see above |
| `namespaces.include`, `namespaces.exclude` | empty | namespace names; the namespaces covered, and the ones left out. The system namespaces and the release's are never covered |
| `operator.install` | `auto` | `auto`, `"true"`, `"false"` |
| `global.imageRegistry` | empty | replaces the registry host of every image the kit runs (a mirror). The Operator's `--collector-image` argument, which the kit never uses, keeps its upstream address |
| `images.pullPolicy`, `imagePullSecrets`, `nodeSelector`, `tolerations`, `priorityClassName` | | placement and pull settings of the gateway, the reporter, the cover pod and the hooks. The Operator takes its own: `operator.imagePullSecrets`, `operator.nodeSelector`, `operator.tolerations`, `operator.priorityClassName` and `operator.affinity` |
| `reporter.interval`, `reporter.resources`, `gateway.replicas`, `gateway.resources`, `instrumentation.python.resources`, `instrumentation.nodejs.resources` | | |
| `cover.enabled` | `auto` | the cover pod: `auto` (with an install token and no image mirror), `"true"`, `"false"` |
| `cover.replicas`, `cover.pollInterval`, `cover.resources`, `cover.webhookConfigName` | `1`, `1m`, small, `impact-ai-cover` | |
| `cover.timeoutSeconds` | `2` | seconds the API server waits for the cover pod's webhook, between 1 and 10 |
| `images.nodejs.*` | written by the release | the Node.js instrumentation image, by tag and digest |
| `webhook.failurePolicy` / `timeoutSeconds` | `Ignore` / `5` | the Operator's webhooks; only `Ignore` is accepted |

`helm upgrade --reset-then-reuse-values` keeps the values you set ([Upgrading](#upgrading)).

Every value has a description in [`values.schema.json`](values.schema.json): one plain sentence that says what it does,
what its default is and when to change it. A chart test fails on a value without one, and on a value of `values.yaml`
that the schema does not describe.

## Uninstall

`helm uninstall` alone leaves the marks the cover pod put on pods, the injection annotations the row commands added to
workloads, and the namespace. The shown uninstall command is therefore these five steps, in this order
([`scripts/uninstall-cluster-kit.sh`](../../scripts/uninstall-cluster-kit.sh) is the same text and is what `make e2e` runs).
It runs with the rights of the person who installed the kit: the pod patch of step 2 is made with yours, and no role of the
kit holds it.

```sh
# 1. The kit. A pre-delete hook first stops the reporter and the cover pod and waits until their pods are gone, and then
#    sends one final snapshot with mode "uninstalled" and no workloads, so impact.ai shows the cluster as not reporting
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
and the cluster's rows turn to not reporting at once (the final snapshot) or, at the latest, two cycles after the
reporter stops. Pods that were covered keep what the Operator put into them (a running pod keeps its init containers as
they started) until they are replaced; they then send nothing, because their export to the gateway that is gone fails at
once and nothing waits for it. A release that moved from a token to a pasted key leaves the Secret `<release>-install-key`
behind, which step 4 deletes with the namespace.

If `helm uninstall` reports a failed pre-delete hook (for example the cluster cannot pull the reporter image), run it again
with `--no-hooks`: the kit is removed, only the final notice is skipped and impact.ai marks the cluster as not reporting
after two missed cycles. The key Secret stays until step 4 deletes the namespace; the marks come off the pods in step 2
either way. To remove the definition too: `kubectl delete crd instrumentations.opentelemetry.io`.

A release whose first install was refused has no key to wait for: the hook starts without the Secret, sends no notice and
ends at once, so `helm uninstall` of it is quick.

## Updating the vendored Operator chart

`python3 scripts/vendor_operator_chart.py` rebuilds `charts/opentelemetry-operator/` and `crds/` from the pinned upstream
release (SHA-256 verified); `--check` fails when they differ. The only changes to upstream are a guard on every object
template (`operator.install`) and an image helper that honours `global.imageRegistry`.
