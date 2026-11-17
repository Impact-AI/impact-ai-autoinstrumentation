# Install options

The [chart README](../chart/impact-ai/README.md#install) has the install command that Cover your AI shows. This page
says what that command does, and how to install without an install token.

## The install token

`install.token` is `impactinstall_<environment>_<43 characters>`, valid for 15 minutes and for one use.
`helm upgrade --install` runs a **pre-install and pre-upgrade hook**, the Job `impact-ai-exchange` (the reporter image,
`reporter exchange`), before anything else is created:

1. It makes the key with `crypto/rand` (`impact_<environment>_<43 characters>`, as Impact AI makes keys) and **stores it
   first**, as `pending-key` in the Secret `impact-ai-install-key`, beside the fingerprint of the token it belongs to.
   Helm made that Secret, empty, as a hook resource of its own just before the Job (only while the cluster has none);
   the Job reads and patches it, and deletes it when a first install is refused.
2. It sends Impact AI the token (as the bearer), the product, the cluster's ID, name and ARN, and **only the key's SHA-256
   hash** and its first characters. Impact AI never sees the key.
3. On a 200 the pending key becomes `ingest-key`. The gateway, the reporter and the cover pod read it from there.

Nobody copies, pastes or types a key, and the key is in no Helm value, no release record and no pod specification. The
Job's rights, and where the token itself can be read, are in the [security model](security-model.md#the-install-token).

* **A lost answer costs one token, not two.** The key was stored before Impact AI was asked, so the retry sends the same
  hash and Impact AI answers `replayed: true`. The hook sends only after the key is stored, so a key that was not stored
  sends nothing and the token is not spent.
* **The same command again changes nothing.** `helm upgrade --reset-then-reuse-values` (see
  [Upgrade](../chart/impact-ai/README.md#upgrade)) hands the old token to the hook again; the Secret carries that token's
  fingerprint (`impact.ai/token`), so the hook does nothing and contacts nobody, and the pods' `checksum/key` (the same
  fingerprint) does not change. An upgrade with a **fresh** token makes a new key and needs Impact AI to be reachable.
* **A fresh command for a cluster that reports** (for example after the key was revoked) makes a new key. The working
  key stays in `ingest-key` until Impact AI has accepted the new one, so a command that is refused changes nothing.
  A new token rolls the gateway, the reporter and the cover pod, which read the key when they start.
* **A refused command** (the token was used, has expired, was cancelled, or is for another product or cluster) fails the
  hook, and Helm 3.18 or later prints the hook's log (`helm.sh/hook-output-log-policy`): the sentence Impact AI sent
  with the refusal, as it sent it, and one line about what is left. An older Helm prints only that the hook failed.
  Nothing was installed beyond what Helm applies before any hook: the namespace (`--create-namespace`) and the
  Instrumentation resource definition (`crds/`), which the uninstall keeps anyway. `helm uninstall` of the failed
  release works at once; the next command, with a fresh token, goes on from there.
* **After `helm rollback`** to a revision that holds an older token, run a fresh command (a new token) instead of an
  upgrade with the old values: the hook would send that older token again, Impact AI refuses it because it was used, and
  the upgrade fails. The working key is not touched.
* **Moving from a pasted key to a token** works on the same release: the Secret the hook makes has a name of its own
  (`<release>-install-key`), so Helm, which owns the Secret of a pasted key (`<release>-ingest-key`) and deletes it when
  it leaves the manifest, never touches the new key.
* **Clusters that add a node for a new pod** (GKE Autopilot, Karpenter): the install hook is the first pod of an install
  and may wait for its node. Its Job allows 4 minutes. If it runs out, run the same command again: the key was stored
  before Impact AI was asked, so the retry costs no second token.

Exit codes of `reporter exchange`: 0 the key is in place (or already was), 1 refused, 2 usage, 3 Impact AI could not be
reached (run the same command again while the token is valid).

## One product per cluster

Reinstalling the same product replaces the install. Installing **another product** in a cluster that already reports to
one is refused, and the message names the first:

```text
this cluster is already connected to the impact.ai product "prod_a" (release impact-ai in namespace impact-ai). A cluster
reports to one product. Uninstall that release first (helm uninstall impact-ai -n impact-ai), then install again for
product "prod_b".
```

## Giving the key yourself

The install token is the way to install: it is valid for 15 minutes and for one use, and the key is made inside the
cluster. For GitOps and for a key you manage, leave `install.token` empty and give the key instead. Passing both is
refused. Installs that give the key (`ingest.key` or `ingest.existingSecret`) get no automatic coverage: a key given
this way belongs to no install, so the cover pod has nothing to ask Impact AI for. Cover workloads with the per-workload
commands.

**GitOps.** A token install is made by Helm, or by a tool that runs Helm against the cluster, as Flux does: the exchange
and the uninstall hook are Helm hooks. A tool that renders the chart without the cluster (`helm template`, Argo CD,
`helm install --dry-run`) would apply them as plain objects, so the chart refuses `install.token` there: the uninstall
hook would run at install time (it stops the reporter and, with a token, deletes the key Secret), and under Argo CD a
failed sync deletes the key Secret. For GitOps give the key: create a Secret in the release namespace with the key
under `ingest-key`, set `ingest.existingSecret` to its name, and render with `helm template --no-hooks` (the hooks
belong to `helm install` and `helm uninstall`). Add `--set cluster.uid=...` when there is no cluster to look in. To
preview a token install, use `helm upgrade --install --dry-run=server`, which asks the cluster.

**At a prompt.** Never paste the key where the terminal shows it: an install's output gets copied into chats and
tickets. These steps ask for the key at a prompt that shows nothing while you paste it (press Enter after it) and hand
it to `helm` on standard input, so it never shows on the screen, stays out of the shell's history, and is in no
command's arguments or environment. The parentheses make them one command: the shell reads all of it before it asks for
the key, also when the lines are pasted one by one, and the key is gone when the command ends.

```sh
(
  printf 'impact.ai ingest key: ' >&2
  IFS= read -rs key
  printf '\n' >&2
  printf '%s\n' "$key" | helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai \
    --namespace impact-ai --create-namespace \
    --set-string product.id=<product> --set-string ingest.endpoint=<ingest origin, no path> \
    --set-file ingest.key=/dev/stdin
)
```

The steps are for bash; PowerShell and cmd have no `/dev/stdin`. A test pastes them into bash in a terminal, line by
line, against a stand-in for `helm`, and checks each of the above
([`chart/tests/test_key_prompt.py`](../chart/tests/test_key_prompt.py)).

The key is kept in a Kubernetes Secret (and, like every Helm value, in the release record); revoking the key in
Impact AI stops this cluster only. To manage the Secret yourself, create it with the key under `ingest-key` and set
`ingest.existingSecret`.

## A new key

To use a new key (the old one was revoked or lost), run the same steps again with it, or run a fresh command from Cover
your AI for a token install. The fingerprint of the key is in the pod templates of the gateway and the reporter, which
read the key when they start, so `helm upgrade --install` replaces both and the cluster reports again at once, without
anyone deleting a pod. With `ingest.existingSecret` the fingerprint is that of the Secret the cluster holds when the
command runs; change the Secret first.

## Cloud identity (optional)

Four optional values say which cloud the cluster runs in. When Cover your AI knows the cluster's cloud (it discovered
the cluster), its command adds them. They give spans and log events an account and a region on nodes that block the
instance metadata service (where a workload's own detection finds nothing), and give the inventory the same. All
default to empty; a value that is empty adds nothing anywhere.

| Value | Span and log event attribute (resource, added by the gateway) | `cluster` object of the inventory |
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

* **Spans and log events**: the gateway adds each attribute only where the resource of the span or log event does not
  already have it, so a workload that detected its own account and region keeps them. `k8s.cluster.uid` is still always
  set by the gateway.
* **Inventory** (`impact.inventory.v1`): `cloudProvider`, `accountId` and `arn` are optional keys of `cluster`, left out
  when empty. `region` is the key that always existed (the nodes' `topology.kubernetes.io/region` label, or `null`); a
  given `cloud.region` replaces it. The final `uninstalled` snapshot carries the same values. `provider` (`eks`, `aks`,
  `gke` or `other`, read from the nodes) is unchanged.

## An image mirror

The kit pulls from `ghcr.io` and its blob host `pkg-containers.githubusercontent.com`, or from a mirror:
`global.imageRegistry` replaces the registry host of every image the chart names, so
`ghcr.io/impact-ai/autoinstrumentation-python` becomes `<mirror>/impact-ai/autoinstrumentation-python`. That includes
the images in the Operator's arguments: `--collector-image`, the default of collectors the kit never creates, is the
gateway's own image. Copy each image to the same path under the mirror with `oras cp -r <src> <dst>`, which copies an
image with its referrers: for the kit's images the cosign signature, the signed SBOM and the build provenance, so the
commands of [Verify what you install](../chart/impact-ai/README.md#verify-what-you-install) find them in the mirror.
The chart names `autoinstrumentation-java` but does not inject it yet: a mirror needs it once a release does.

The injected image is pulled by **the covered workload's own pod**, with its node's credentials or the workload's own
`imagePullSecrets`, so a mirror has to be open to every covered workload. The kit's `imagePullSecrets` value applies to
the kit's own pods only.

**Behind an image mirror, ask for it.** `cover.enabled: auto` installs the cover pod with an install token only when no
image mirror (`global.imageRegistry`) is set. The init container a covered pod gets is pulled from the mirror, and the
covered workloads may not be able to pull from it: set `cover.enabled` to `"true"` once they can.
