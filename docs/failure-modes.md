# Failure modes

The kit fails open: your running workloads keep serving when the webhook, the Operator, the gateway or Impact AI is
unavailable, and a new pod starts without the instrumentation rather than wait for it. Two cases hold up the new pods of
covered workloads, an injected image that cannot be pulled and an admission policy that refuses the injected init
container; they are in the table with the others.

## When part of the kit is unavailable

The end-to-end suite runs these cases: the Operator down, the cover pod down, ingest down, a revoked key, an image that
cannot be pulled, and (from Kubernetes 1.30) a policy that refuses the init container. The others follow from the webhook
settings and the init container and are not run: a webhook that is slow, a webhook the API server cannot reach, and a
copy that fails in the init container.

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
| **The image the Operator injects cannot be pulled** (the registry is unreachable, the image was removed, a mirror is closed to the workload) | New pods of covered workloads wait in `Init:ImagePullBackOff` until the image can be pulled, and a rollout or a scale-out of a covered workload stalls until then. Pods that run keep running, and so do new pods on nodes that already hold the image. A rollout whose strategy allows unavailable pods (`maxUnavailable` above 0) stalls after removing that many. With automatic coverage, the next rollout of a listed workload is such a rollout ([below](#when-covering-a-workload-would-stall-its-rollout)). |
| **An admission policy refuses the injected init container** (a registry allowlist, a user-ID range, a LimitRange ratio) | The new pods of covered workloads are refused at admission and the rollout stalls in the same way, until the policy allows the init container or the workload is left out of coverage. Pods that run keep running. |
| The copy in the injected init container fails (for example a full node disk) | The pod waits in Init (`Init:Error`, then `Init:CrashLoopBackOff`) while the copy is retried. |

The injected image is pulled by the kubelet for **the covered workload's** pod: with the node's credentials, or with the
`imagePullSecrets` of the workload's own pod. The kit's `imagePullSecrets` value applies to the kit's own pods. A mirror
(`global.imageRegistry`) therefore has to be open to every covered workload.

## When covering a workload would stall its rollout

* **Covering adds the kit's init container at the next rollout of a listed workload.** That init container is pulled and
  admitted like any other. If the workload's nodes cannot pull the kit's image (a mirror the workload cannot reach, a
  registry that is down), or an admission policy refuses the init container (a registry allowlist, a user-ID range, a
  LimitRange ratio: [security model](security-model.md#admission-rules-and-user-ids)), the new pods wait in
  `Init:ImagePullBackOff` or are refused at admission, and the rollout stalls. Pods that run keep running, and a rollout
  whose strategy allows unavailable pods (`maxUnavailable` above 0) stops after removing that many. Three ways out, each
  of which lets the next pod start without the init container: the label `impact.ai/opt-out` on the namespace (at once)
  or on the workload's pod template (from its next ReplicaSet), `namespaces.include` without the namespace, or
  `cover.enabled=false`. Pods that already wait restart without the instrumentation once the workload is no longer
  covered: delete them. The reporter names the reason in its next snapshot, `ImagePullBackOff` or `FailedCreate` (names
  only, never a message), within one reporter cycle (`reporter.interval`, 5 minutes by default). The end-to-end suite pins
  both causes and that snapshot ([`e2e/test_8_auto_cover.py`](../e2e/test_8_auto_cover.py)).
* **The webhook fails open.** `failurePolicy: Ignore` and a short timeout: with no list, with automatic coverage off for
  the cluster, with the cover pod down or Impact AI out of reach (the last list is kept), pods start as they would. A
  revoked key stops it covering.
* **A network path that drops the API server's calls slows pod creation down.** The API server calls the cover webhook,
  and the Operator's, on port 9443 of the release namespace. With the cover pod down and its Service without endpoints it
  answers at once: pod creation does not wait. If a NetworkPolicy or a firewall keeps the API server out (on a GKE private
  cluster, a firewall rule for 9443 covers both webhooks), or the pod hangs, every pod creation in a covered namespace
  waits for the cover webhook (`cover.timeoutSeconds`, 2 seconds) and again for the Operator's (`webhook.timeoutSeconds`,
  5 seconds) before it starts uninjected. Allow that ingress, or set `cover.enabled` to `"false"`.

## In a covered process

In a covered Python process the package starts with the process, imports nothing until the application imports OpenAI,
Anthropic, boto3 or a web framework, and exports on a thread of its own, dropping spans when the gateway or ingest is
unreachable. If the gateway does not answer, a covered process, in Python or in Node.js, waits at most 5 seconds for it
at exit and holds at most 16 MiB of unsent spans, dropping the oldest first. A DNS lookup for the gateway's name cannot
be interrupted: if the cluster's DNS itself hangs, a process can wait about 20 seconds at exit. Nothing the kit runs is
privileged: every pod runs as non-root with a read-only root file system and no capabilities.

For Node.js, a hung gateway, or one whose packets are dropped, delays the end of a process by at most 5 seconds (2.5
measured). In a test, a pod limited to 512 MiB that made 100 spans a second of 100 kB each ran for two minutes against a
gateway that did not answer and grew by 28 MB.

## In the gateway

The gateway keeps spans, and log events, each in a queue in memory (2,000 batches each) and retries a batch for up to
120 seconds. A restart of the gateway, or an ingest outage longer than 120 seconds, drops what was queued. The memory
limiter and `GOMEMLIMIT` count both queues: when the gateway nears its limit it refuses new data, spans and log events
alike, and the workloads keep serving.

## How this is tested

* The fail-open cases on a kind cluster: [`e2e/test_3_fail_open.py`](../e2e/test_3_fail_open.py) and, for automatic
  coverage, [`e2e/test_8_auto_cover.py`](../e2e/test_8_auto_cover.py).
* Ingest down, refused, overloaded or silent, in containers:
  [`python/tests/image/test_ingest_down.py`](../python/tests/image/test_ingest_down.py),
  [`nodejs/tests/image/test_nodejs_ingest_down.py`](../nodejs/tests/image/test_nodejs_ingest_down.py),
  [`java/tests/image/test_java_never_breaks.py`](../java/tests/image/test_java_never_breaks.py).
* The gateway's queues, read from the pinned Collector's own metrics:
  [`python/tests/image/test_kit_gateway_logs.py`](../python/tests/image/test_kit_gateway_logs.py).
