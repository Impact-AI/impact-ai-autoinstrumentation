# SPDX-License-Identifier: Apache-2.0

# After Stop copying: replace a workload's pods now, so that they use their original Agent address.
NAMESPACE="${NAMESPACE:-default}"
DEPLOYMENT="${DEPLOYMENT:-support-assistant}"
kubectl -n "$NAMESPACE" rollout restart deployment/"$DEPLOYMENT"
kubectl -n "$NAMESPACE" rollout status deployment/"$DEPLOYMENT" --timeout=180s
