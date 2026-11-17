// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"strings"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kube"
)

// cleanVersion turns a git version such as v1.31.2-eks-abc or v1.30.3+k3s1
// into 1.31.2.
func cleanVersion(gitVersion string) string {
	v := strings.TrimPrefix(strings.TrimSpace(gitVersion), "v")
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	return v
}

// nodeProvider names the managed Kubernetes service a node belongs to, or "".
func nodeProvider(n kube.Node) string {
	l, kubelet := n.Metadata.Labels, n.Status.NodeInfo.KubeletVersion
	has := func(key string) bool { _, ok := l[key]; return ok }
	switch {
	case has("eks.amazonaws.com/nodegroup") || has("eks.amazonaws.com/compute-type") || strings.Contains(kubelet, "-eks-"):
		return "eks"
	case has("cloud.google.com/gke-nodepool") || strings.Contains(kubelet, "-gke."):
		return "gke"
	case has("kubernetes.azure.com/cluster") || has("kubernetes.azure.com/role"):
		return "aks"
	}
	return ""
}

// nodeRegion reads the region label, preferring the current one over the legacy one.
func nodeRegion(n kube.Node) string {
	if r := n.Metadata.Labels["topology.kubernetes.io/region"]; r != "" {
		return r
	}
	return n.Metadata.Labels["failure-domain.beta.kubernetes.io/region"]
}

// providerAndRegion reads the provider and region from the first nodes. The
// provider is "other" when no node identifies a managed service; the region is
// nil when no node has the label.
func providerAndRegion(nodes []kube.Node) (string, *string) {
	provider := "other"
	for _, n := range nodes {
		if p := nodeProvider(n); p != "" {
			provider = p
			break
		}
	}
	for _, n := range nodes {
		if r := nodeRegion(n); r != "" {
			return provider, &r
		}
	}
	return provider, nil
}
