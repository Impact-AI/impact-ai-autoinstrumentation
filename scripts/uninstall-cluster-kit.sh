#!/bin/sh
# SPDX-License-Identifier: Apache-2.0

# Remove the impact.ai cluster kit and what it and the per-workload commands added to the cluster's pods and workloads.
#
#   1. helm uninstall: the reporter and the cover pod are stopped and the reporter sends one last notice (impact.ai shows
#      the cluster as not reporting at once), then the webhooks, the Operator, the gateway, the reporter and the
#      Instrumentation resource are deleted. The Operator's resource definition (instrumentations.opentelemetry.io) is
#      kept: Helm never deletes definitions, and other tools in the cluster may use it.
#   2. The marks come off the pods the cover pod marked. They carry the label impact.ai/auto-covered, so the API server
#      finds them: the injection annotations, the marker and the label are taken off each one, a metadata patch that
#      restarts nothing. A pod keeps what the Operator put into it until it is replaced, and a pod created from now on is
#      not injected: the cover pod and its webhook are gone.
#   3. The injection annotations (Python and Node.js) on the workloads that were covered are removed. Each of those
#      workloads restarts, without injection (the same applies if you restart them later: nothing injects them any more).
#      Only the kit's own annotations go: the value names this install's namespace and Instrumentation, so another
#      injector's annotation on a workload stays.
#   4. The namespace the install created is deleted.
#   5. The cluster-scoped objects labelled with the release are deleted. Helm removes them with the release; this catches
#      what an uninstall that was cut short leaves behind.
#
# Needs helm and kubectl, pointed at the cluster, with the rights of the person who installed it. RELEASE and NAMESPACE
# default to the install command's values.
set -eu

RELEASE="${RELEASE:-impact-ai}"
NAMESPACE="${NAMESPACE:-impact-ai}"

helm uninstall "$RELEASE" --namespace "$NAMESPACE" --wait

# The marks the cover pod put on the pods it annotated: the five annotations it may add, and the label. A pod that has
# finished is left out, and so is a pod the cover pod did not mark (one whose template you annotated yourself keeps its
# annotation). A pod that is gone by now is not an error.
STRIP='{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/inject-nodejs":null,"instrumentation.opentelemetry.io/container-names":null,"instrumentation.opentelemetry.io/otel-python-platform":null,"impact.ai/auto-covered":null},"labels":{"impact.ai/auto-covered":null}}}'
kubectl get pods --all-namespaces --selector impact.ai/auto-covered=true --field-selector 'status.phase!=Succeeded,status.phase!=Failed' -o jsonpath="{range .items[*]}{.metadata.namespace}{' '}{.metadata.name}{'\n'}{end}" |
  while read -r ns name; do
    kubectl --namespace "$ns" patch pod "$name" --type merge -p "$STRIP" ||
      echo "could not take the marks off pod $ns/$name (it may be gone); going on" >&2
  done

# inject-python (and otel-python-platform, which Alpine images carry), then inject-nodejs.
for language in python nodejs; do
  ANNOTATION="instrumentation\\.opentelemetry\\.io/inject-$language"
  if [ "$language" = python ]; then
    REMOVE='"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/otel-python-platform":null'
  else
    REMOVE='"instrumentation.opentelemetry.io/inject-nodejs":null'
  fi
  for kind in deployments statefulsets daemonsets; do
    kubectl get "$kind" --all-namespaces -o jsonpath="{range .items[?(@.spec.template.metadata.annotations['$ANNOTATION']=='$NAMESPACE/impact')]}{.metadata.namespace}{' '}{.metadata.name}{'\n'}{end}" |
      while read -r ns name; do
        kubectl --namespace "$ns" patch "$kind" "$name" --type merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{$REMOVE}}}}}"
      done
  done
  kubectl get cronjobs --all-namespaces -o jsonpath="{range .items[?(@.spec.jobTemplate.spec.template.metadata.annotations['$ANNOTATION']=='$NAMESPACE/impact')]}{.metadata.namespace}{' '}{.metadata.name}{'\n'}{end}" |
    while read -r ns name; do
      kubectl --namespace "$ns" patch cronjob "$name" --type merge -p "{\"spec\":{\"jobTemplate\":{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{$REMOVE}}}}}}}"
    done
done

kubectl delete namespace "$NAMESPACE" --wait

# The cluster-scoped objects of this release, by label: roles, bindings and webhook configurations. The definition of
# the Instrumentation resource is not among them.
kubectl delete clusterrolebindings,clusterroles,mutatingwebhookconfigurations,validatingwebhookconfigurations --selector "app.kubernetes.io/part-of=impact-ai,app.kubernetes.io/instance=$RELEASE" --ignore-not-found
