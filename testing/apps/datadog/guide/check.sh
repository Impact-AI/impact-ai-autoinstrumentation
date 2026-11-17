# SPDX-License-Identifier: Apache-2.0

# The Agent address each pod of a workload uses. Set the namespace and the label of the workload's pods.
NAMESPACE="${NAMESPACE:-default}"
SELECTOR="${SELECTOR:-app=support-assistant}"
kubectl -n "$NAMESPACE" get pods -l "$SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"  DD_TRACE_AGENT_URL="}{.spec.containers[0].env[?(@.name=="DD_TRACE_AGENT_URL")].value}{"  DD_AGENT_HOST="}{.spec.containers[0].env[?(@.name=="DD_AGENT_HOST")].value}{.spec.containers[0].env[?(@.name=="DD_AGENT_HOST")].valueFrom.fieldRef.fieldPath}{"\n"}{end}'
