# Troubleshooting

Each entry names a symptom, the causes the kit knows of, and what to do. The pages linked give the full behaviour.

## What to collect first

```sh
helm list -n impact-ai                                     # the chart version
kubectl -n impact-ai get pods
kubectl -n impact-ai logs deploy/impact-ai-reporter --tail=50
kubectl -n impact-ai logs deploy/impact-ai-operator --tail=50
kubectl -n impact-ai logs deploy/impact-ai-cover --tail=50 # with automatic coverage
```

For a workload that is not traced, also its pod's annotations and init containers:
`kubectl -n <namespace> get pod <pod> -o jsonpath='{.metadata.annotations}{"\n"}{.spec.initContainers[*].name}{"\n"}'`.
Put these into a bug report ([SUPPORT.md](../SUPPORT.md)), after removing anything you consider confidential. The
kit's own logs hold names, not values.

## The install command fails

| Symptom | Cause and fix |
|---|---|
| `helm` prints a sentence from Impact AI and the hook failed | The install token was refused: it was used, has expired (15 minutes), was cancelled, or is for another product or cluster. Get a fresh command from Cover your AI. Nothing was installed beyond the namespace and the resource definition; the next command goes on from there. With Helm older than 3.18 only "hook failed" is printed: upgrade Helm to see the reason. |
| The hook failed and Impact AI could not be reached (`reporter exchange` exit code 3) | Egress from the release namespace to the ingest host on 443 is blocked, or Impact AI was unavailable. Run the same command again while the token is valid: the key was stored before Impact AI was asked, so the retry costs no second token. |
| The hook times out after 4 minutes on GKE Autopilot or with Karpenter | The hook's pod waited for a new node. Run the same command again. |
| `this cluster is already connected to the impact.ai product ...` | A cluster reports to one product. Uninstall the release the message names, then install again ([install options](install.md#one-product-per-cluster)). |
| `helm template`, Argo CD or `--dry-run` refuses `install.token` | A token install needs Helm against the cluster. For GitOps give the key with `ingest.existingSecret` and render with `--no-hooks` ([install options](install.md#giving-the-key-yourself)). |
| AKS Automatic or AKS Safeguards deny the hooks | Install with `ingest.key` and uninstall with `--no-hooks` ([security model](security-model.md#admission-rules-and-user-ids)). |
| The kit's pods are refused on OpenShift | Its default security context constraint assigns user IDs by namespace range and refuses the kit's fixed IDs; OpenShift is not supported. |

## The cluster does not appear in Impact AI, or stops reporting

* The reporter logs `inventory sent` for each accepted snapshot and `inventory not delivered` otherwise.
  `key rejected: ingest refused the ingest key` (a `401` or `403`) means the key was revoked, has expired or is wrong:
  run a fresh install command, which makes a new key and restarts the gateway and the reporter
  ([install options](install.md#a-new-key)).
* Egress from the release namespace to the ingest host on 443 is blocked by a NetworkPolicy or a firewall
  ([security model](security-model.md#what-the-cluster-must-allow)).
* The first snapshot arrives soon after `helm` returns (the end-to-end suite holds a token install to 45 seconds);
  after that one every `reporter.interval` (5 minutes by default).
* `kubectl -n impact-ai get pods` shows `ImagePullBackOff`: the nodes cannot reach `ghcr.io` and
  `pkg-containers.githubusercontent.com`, or the mirror lacks an image ([install options](install.md#an-image-mirror)).

## A workload is not traced

1. **Its pod has no `instrumentation.opentelemetry.io/inject-*` annotation.** Pods created before the annotation are
   not injected: the per-workload command restarts the workload, and automatic coverage covers it at its next rollout.
   With automatic coverage, check that the cluster's setting in Cover your AI is on, the workload is on the list, and no
   `impact.ai/opt-out` label or annotation, `namespaces.include` or `namespaces.exclude` leaves it out. System namespaces
   and the release namespace are never covered.
2. **The annotation is there but the pod has no init container from the Operator.** The Operator was down, slow or
   unreachable when the pod was created, and the pod started uninjected (fail open). Check the Operator's pod and log,
   and that the API server can reach port 9443 in the release namespace. Then restart the workload.
3. **The chart runs in report-only mode** (`operator.install: auto` found another injector, or `operator.install=false`):
   the kit injects nothing ([coexistence](coexistence.md)).
4. **The workload has a tracer of its own**, and the package stood down. Its `impact.setup` span has the state
   `pre-existing` and names what it found ([coexistence](coexistence.md#beside-another-tracer-in-the-process)).
5. **The library version is outside what the qualification proves.** The `impact.setup` span lists the library under
   `impact.setup.untraced`, with a line in `impact.setup.skipped` such as
   `openai: Impact AI traces openai 1 to 3; this workload runs openai 4.0.0` ([compatibility](compatibility.md#ai-libraries)).
6. **Node.js older than 22.15**: the application runs uninstrumented and the setup report says why.
7. **The workload set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`**: its spans arrive without
   content, as intended.

## Pods do not start, or start slowly

| Symptom | Cause and fix |
|---|---|
| New pods of a covered workload wait in `Init:ImagePullBackOff` | The workload's nodes cannot pull the kit's image (registry unreachable, mirror closed to the workload). Running pods keep running. Allow the pull, or take the workload out of coverage ([failure modes](failure-modes.md#when-covering-a-workload-would-stall-its-rollout)). |
| New pods are refused at admission (`FailedCreate`) | A registry allowlist, a user-ID range that leaves out 65532, or a LimitRange whose CPU limit-to-request ratio is below 20 ([security model](security-model.md#admission-rules-and-user-ids)). |
| Every pod creation in a covered namespace takes 2 to 7 seconds more | The API server cannot reach the webhooks on port 9443: a NetworkPolicy, or on GKE private clusters the control-plane firewall ([security model](security-model.md#what-the-cluster-must-allow)). Allow the ingress, or set `cover.enabled` to `"false"`. |
| Pods wait in `Init:Error` or `Init:CrashLoopBackOff` | The copy in the injected init container fails, for example on a full node disk. |
| The Operator restarts with "too many open files" | The node ran out of inotify instances: each pod's runtime shim uses some, and the Linux default is 128 per user (`fs.inotify.max_user_instances`). Raise it on nodes with many pods. |

## Upgrade and uninstall

* **After an upgrade the old images still run**: the upgrade used `--reuse-values`, which keeps the old digests. Upgrade
  again with `--reset-then-reuse-values` ([chart README](../chart/impact-ai/README.md#upgrade)).
* **Argo CD shows a diff on every sync, and pods created just after a sync start uninjected**: a render without the
  cluster makes new webhook certificates each time. Give the render `cluster.uid`.
* **An upgrade fails after `helm rollback`**: the old revision holds a used token. Run a fresh command.
* **`helm uninstall` reports a failed pre-delete hook**: run it again with `--no-hooks`, then the remaining steps of the
  uninstall command ([chart README](../chart/impact-ai/README.md#uninstall)).
