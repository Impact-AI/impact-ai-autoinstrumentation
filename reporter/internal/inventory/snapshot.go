// SPDX-License-Identifier: Apache-2.0

// Package inventory builds the names-only inventory snapshot (contract C1,
// schema impact.inventory.v1) from what the Kubernetes API says about a cluster.
//
// Names, never values: a snapshot holds workload, container, image,
// environment variable and Secret names, the base name of each executable, and
// annotations from a short allowlist. It never holds environment values,
// container arguments, Secret or ConfigMap contents.
package inventory

import (
	"cmp"
	"slices"
)

// SchemaV1 is the schema name of the snapshot.
const SchemaV1 = "impact.inventory.v1"

// Limits of ingest: more than this and it answers 413.
const (
	MaxWorkloads = 5000
	MaxBodyBytes = 2 << 20 // gzip
)

// Reporter modes.
const (
	ModeInject     = "inject"
	ModeReportOnly = "report-only"
	// ModeUninstalled marks the single final snapshot that the uninstall hook
	// sends after the reporter has stopped: the cluster is no longer reporting.
	ModeUninstalled = "uninstalled"
)

// Snapshot is the request body of POST /v1/inventory.
type Snapshot struct {
	Schema     string       `json:"schema"`
	Cluster    Cluster      `json:"cluster"`
	Reporter   ReporterInfo `json:"reporter"`
	ObservedAt string       `json:"observedAt"`
	Sequence   int64        `json:"sequence"`
	Workloads  []Workload   `json:"workloads"`
}

// Cluster identifies the cluster. UID is the UID of the kube-system namespace.
//
// CloudProvider, AccountID and ARN are the cloud identity the installer gave
// (chart values cloud.provider, cloud.accountId, cluster.arn). They are left out
// when empty. Region is the topology label of the first nodes, or null; the
// installer's cloud.region replaces it when given.
type Cluster struct {
	UID               string  `json:"uid"`
	Name              *string `json:"name"`
	Provider          string  `json:"provider"` // eks, aks, gke or other
	Region            *string `json:"region"`
	KubernetesVersion string  `json:"kubernetesVersion"`
	CloudProvider     string  `json:"cloudProvider,omitempty"` // aws, azure or gcp
	AccountID         string  `json:"accountId,omitempty"`
	ARN               string  `json:"arn,omitempty"` // the provider's own ID of the cluster, such as the EKS ARN
}

// ReporterInfo says who reported and in which mode.
type ReporterInfo struct {
	Version  string `json:"version"`
	Instance string `json:"instance"`
	Mode     string `json:"mode"`
	// ClusterOwner names the other injector found in the cluster, if any.
	ClusterOwner *string `json:"clusterOwner"`
}

// Workload is a top-level owner of pods and the pod template facts about it.
//
// AutoCovered, Labels, NamespaceLabels and Stall are the facts of automatic coverage. They are sent only when the chart
// asks for them (Options.CoverFields: the cover pod is installed, or an install token was used), so a server that does
// not know them never sees them, and are left out when empty.
type Workload struct {
	Namespace      string            `json:"namespace"`
	Kind           string            `json:"kind"`
	Name           string            `json:"name"`
	Pods           Pods              `json:"pods"`
	ServiceAccount string            `json:"serviceAccount"`
	Annotations    map[string]string `json:"annotations"`
	Containers     []Container       `json:"containers"`
	// AutoCovered is when the cover pod annotated the workload's first live pod (RFC 3339, UTC): the earliest value of
	// the pods' impact.ai/auto-covered annotation.
	AutoCovered string `json:"autoCovered,omitempty"`
	// Labels are the impact.ai/* labels of the workload and of its pod template (the template's win), such as
	// impact.ai/opt-out. No other label is read.
	Labels map[string]string `json:"labels,omitempty"`
	// NamespaceLabels are the impact.ai/* labels of the workload's namespace.
	NamespaceLabels map[string]string `json:"namespaceLabels,omitempty"`
	// Stall names why the workload's pods do not start, when they do not. Left out when nothing is wrong.
	Stall *Stall `json:"stall,omitempty"`
}

// Stall names why the pods of a workload do not start, and nothing else: two kinds of reason, each a plain name of the
// shape the kubelet and the controllers give them (FailedCreate, ImagePullBackOff). It is what lets impact.ai see that a
// workload it covers is stuck on the injection, for instance an image the cluster fails to pull or a policy that refuses
// the init container, and stop covering it. The message that goes with a reason can hold what a registry or a policy
// answered about a pod, so it is never read.
type Stall struct {
	// ReplicaFailure is the reason of the Deployment's ReplicaFailure condition while it is true (FailedCreate when the
	// ReplicaSet fails to create a pod: an admission policy refused it, a quota is used up). Deployments only.
	ReplicaFailure string `json:"replicaFailure,omitempty"`
	// InitWaiting are the reasons the init container the Operator injects waits for, across the workload's live pods:
	// sorted, once each, at most MaxStallReasons. A start that is on its way (ContainerCreating, PodInitializing) is not
	// one.
	InitWaiting []string `json:"initWaiting,omitempty"`
}

// MaxStallReasons is how many waiting reasons one workload reports.
const MaxStallReasons = 8

// Pods counts the pods a workload wants and has ready.
type Pods struct {
	Desired int `json:"desired"`
	Ready   int `json:"ready"`
}

// Container holds names only.
type Container struct {
	Name        string       `json:"name"`
	Image       string       `json:"image"`
	Executables []string     `json:"executables"`
	EnvNames    []string     `json:"envNames"`
	SecretRefs  []SecretRef  `json:"secretRefs"`
	EnvFrom     []EnvFromRef `json:"envFrom"`
	Injected    Injected     `json:"injected"`
}

// SecretRef says which Secret key feeds an environment variable.
type SecretRef struct {
	EnvName string `json:"envName"`
	Secret  string `json:"secret"`
	Key     string `json:"key"`
}

// EnvFromRef names a Secret or ConfigMap whose keys become variables.
type EnvFromRef struct {
	Kind string `json:"kind"` // Secret or ConfigMap
	Name string `json:"name"`
}

// Injected tells whether a live pod runs with the instrumentation.
type Injected struct {
	Python bool `json:"python"`
}

func compareWorkloads(a, b Workload) int {
	return cmp.Or(
		cmp.Compare(a.Namespace, b.Namespace),
		cmp.Compare(a.Kind, b.Kind),
		cmp.Compare(a.Name, b.Name),
	)
}

func sortWorkloads(ws []Workload) {
	slices.SortFunc(ws, compareWorkloads)
}

// byPriority orders workloads for truncation: most desired pods first, then by
// identity so the cut is deterministic.
func byPriority(a, b Workload) int {
	return cmp.Or(cmp.Compare(b.Pods.Desired, a.Pods.Desired), compareWorkloads(a, b))
}
