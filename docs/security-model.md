# Security model

What each part of the kit may do in the cluster, what the cluster must allow, and where the secrets of an install are.
To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Principles

* **Names, never values.** The reporter sends names only; its role cannot read Secrets or ConfigMaps
  ([what leaves the cluster](what-leaves-the-cluster.md)).
* **Fail open.** Every webhook has `failurePolicy: Ignore` and a short timeout, and a covered process never waits on the
  kit for long ([failure modes](failure-modes.md)).
* **Nothing privileged.** Every pod of the kit runs as non-root with a read-only root file system and no capabilities;
  there is no DaemonSet and no host access.
* **Pinned and signed.** Every image the chart runs is pinned by digest. The kit's own images and the chart are signed
  with cosign and carry SBOMs and build provenance
  ([verify what you install](../chart/impact-ai/README.md#verify-what-you-install)).

## Permissions

Every role of the kit is pinned, install shape by install shape, in
[`chart/tests/test_chart_roles.py`](../chart/tests/test_chart_roles.py): a new right has to change that file, so it is
argued for in review. **No long-lived role of the kit reads a Secret.** Two short-lived rights touch one Secret, the one
that holds the cluster's ingest key: the install hook may `get`, `patch` and `delete` it while `helm install` or
`helm upgrade` with a token runs (it deletes it only when a first install is refused), and the uninstall hook may
`delete` it while `helm uninstall` runs. Both name that Secret in `resourceNames`; no rule of the kit can `create`,
`list`, `watch` or `update` a Secret. The trivy check KSV-0113 ("manage namespace secrets", MEDIUM) reports exactly
those two roles, and this is the reason it stays: they are the least that lets the cluster store its key before
Impact AI is asked (`get`, `patch`) and remove it when an install is refused or the kit is uninstalled (`delete`), they
name the one Secret, and they exist only while a hook runs.

### Reporter

ClusterRole `impact-ai-kit-reporter`: `get`, `list` on namespaces; `list` on nodes, pods, deployments, statefulsets,
daemonsets, cronjobs, jobs and mutating webhook configurations. **Never Secrets or ConfigMaps**, and it decodes only the
fields it sends: environment variable names, `command[0]`'s base name, Secret *references* by name, and annotations from
an allowlist (`instrumentation.opentelemetry.io/*`, `impact.ai/*`, `admission.datadoghq.com/*`, `*dynatrace.com/*`,
`cloudwatch.aws.amazon.com/*`). Environment values, container arguments, Secret and ConfigMap contents and
`kubectl.kubernetes.io/last-applied-configuration` never leave the cluster.

### Operator

ClusterRole `impact-ai-operator-manager` and a Role in the release namespace, both written by this chart; the upstream
chart's role, made for collectors the kit does not install, is not used. The ClusterRole holds `get`, `list` and
`watch` on namespaces, ReplicaSets, Jobs and EndpointSlices (the pod webhook names the workload that owns a pod; at
start the Operator looks up the API server's address); `get`, `list`, `watch`, `update` and `patch` on `Instrumentation`
resources, and `get`, `update`, `patch` on their status; `create` and `patch` on events; `create` on TokenReviews and
SubjectAccessReviews (its metrics endpoint checks its callers). The Role holds `create`, `get`, `list` and `update` on
Leases (leader election). The Operator changes `Instrumentation` resources, events and its own Lease, and nothing else:
it creates, changes and deletes no pod, workload, Service or policy, and it has no rule for Secrets or ConfigMaps. The
role is the same on every Kubernetes version. `make lint` scans the rendered chart with trivy at Kubernetes 1.27 and
1.34 (no HIGH or CRITICAL finding), and the end-to-end suite asks the API server what the Operator may do and reads the
Operator's log for a "forbidden".

### Install hook

Exists only while `helm install` or `helm upgrade` with a token runs: a Role in the release namespace that may `get`,
`patch` and `delete` the one Secret named `<release>-install-key`, which Helm made empty before the hook; it deletes it
only when a first install is refused. No `create` (a role that holds it may make any Secret of the namespace), no
`list`, no `watch`, no other Secret, no cluster-wide permission. Deleted with the hook.

### Cover pod

None. Its service account has no binding and mounts no token; the list of workloads to cover arrives over HTTPS.

### Uninstall hook

Exists only while `helm uninstall` runs: the reporter's read rules, plus `patch` on `deployments/scale` of the reporter
Deployment alone (and of the cover pod's, when there is one), and `delete` on the one key Secret (no `get`, `list` or
`watch`: it can read no Secret). It writes to no pod, and no role of the kit holds a write verb on pods; the uninstall
command takes the marks off the pods with your own rights.

### The installing user

Installing creates a CustomResourceDefinition, ClusterRoles, ClusterRoleBindings and mutating and validating webhook
configurations, and `operator.install: auto` lists mutating webhook configurations: in practice the user needs
`cluster-admin`.

## The install token

The Job, the Secret that carries the token to it (a mounted file, never the pod specification), and the hook's
ServiceAccount and Role are deleted when the hook ends. It deletes the key Secret in one case, a refused first install,
so that a refusal leaves nothing of the kit; it never deletes a Secret that holds a working key. Helm never deletes the
Secret of the key, so it survives an upgrade; `helm uninstall` removes it (its hook may `delete` that one Secret).

**Where the token can be read.** `install.token` is a Helm value: it is in the shell history and the process list of the
machine that runs `helm`, in `helm get values`, and in every release record Helm keeps (ten by default), which whoever can
read Secrets in the release namespace can read. It opens nothing once it has been used, or after 15 minutes. Helm writes
the first release record before the hook uses the token, so for those few seconds a reader of that record could use it
first: install from a machine and a namespace you trust.

**Where the key is.** In the Secret `impact-ai-install-key` (a token install) or `impact-ai-ingest-key` (a key you gave,
which is then also in the release record), or in the Secret `ingest.existingSecret` names. The gateway, the reporter and
the cover pod read it from there. Revoking the key in Impact AI stops this cluster only.

## What the cluster must allow

A cluster without network policies or admission rules of its own needs to allow nothing. Where it has them, these are the
places the kit meets them.

| Flow | From | To | Port |
|---|---|---|---|
| Spans and log events | covered pods, and pods whose own OpenTelemetry instrumentation exports log events, in any namespace | the gateway `impact-ai-gateway`, in the release namespace | 4318 |
| Webhooks | the control plane (the API server) | the Operator's webhook, and the cover pod's with automatic coverage, in the release namespace | 9443 |
| Ingest | the gateway, the reporter, the cover pod and the install and uninstall hooks | the ingest host (`ingest.endpoint`) | 443 |
| Cluster API | the reporter and the Operator | the API server (the `kubernetes` Service) | the API server's own (443 on most managed clusters) |

A default-deny network policy needs an allow rule for each of the four. A policy in the release namespace that keeps the
API server out of port 9443 is the one that slows pod creation down
([failure modes](failure-modes.md#when-part-of-the-kit-is-unavailable)).

**Images.** The kit pulls from `ghcr.io` and its blob host `pkg-containers.githubusercontent.com`, or from a mirror
([install options](install.md#an-image-mirror)).

**GKE private clusters.** Google's firewall lets the control plane open connections to nodes and pods on ports 443 and
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
on GKE). Private nodes also need Cloud NAT, or a mirror, to pull images.

## Admission rules and user IDs

* **The injected init container** has to be admitted into every covered pod. It runs as user 65532 with no privileges,
  asks 10m CPU and 16 Mi and is limited to 200m and 64 Mi (`instrumentation.python.resources` and
  `instrumentation.nodejs.resources`). A registry allowlist that leaves out the kit's images, a user-ID range that leaves
  out 65532, or a LimitRange whose largest limit-to-request ratio for CPU is below 20 (200m against 10m) refuses the
  pod. [Failure modes](failure-modes.md#when-covering-a-workload-would-stall-its-rollout) says what that does to a
  rollout.
* **User IDs.** The kit's pods run as the fixed user IDs 65532 (the reporter, the cover pod, the hooks and the injected
  init container) and 10001 (the gateway). A cluster that assigns user IDs by namespace range, as OpenShift's default
  security context constraint does, refuses them. OpenShift is not supported.
* **AKS Automatic**, and AKS Safeguards in Enforce mode, are expected to deny the install and uninstall hooks (their pods
  have no probes and no CPU limit); this has not been run. There, install with `ingest.key`, which also leaves automatic
  coverage off, and uninstall with `--no-hooks`.

## Findings below the gate

`make lint` fails on a HIGH or CRITICAL finding of trivy in the rendered chart. A scan with every severity also shows
these findings, which are accepted with their reasons ([`scripts/scan-chart.sh`](../scripts/scan-chart.sh)):

* KSV-0113 (MEDIUM, manage namespace secrets): the two hook roles above.
* KSV-0125 (MEDIUM, trusted registries): the images come from `ghcr.io`, by digest; a mirror is `global.imageRegistry`.
* KSV-01010 (MEDIUM, sensitive content in a ConfigMap): the gateway configuration sets an `authorization` header whose
  value is `${env:IMPACT_INGEST_KEY}`, a reference to the Secret and not the key.
* KSV-0011 (LOW, CPU not limited): the kit's pods request CPU and limit memory.
