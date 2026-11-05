// SPDX-License-Identifier: Apache-2.0

// Package kube is a minimal, read-only Kubernetes API client for the reporter.
//
// Every type in types.go is a decode target for an API response and holds names
// only (AE-3): there is no field for environment values, container arguments,
// Secret or ConfigMap data, certificates, or annotations outside the allowlist.
// Values that are not declared here are skipped by encoding/json, so they are
// never stored, logged or sent. types_test.go enforces this by reflection.
package kube

import (
	"bytes"
	"encoding/json"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

// MaxAnnotationValueLength caps the value of an allowlisted annotation.
const MaxAnnotationValueLength = 256

// Helm marks every object it creates with these annotations.
const (
	HelmReleaseNameAnnotation      = "meta.helm.sh/release-name"
	HelmReleaseNamespaceAnnotation = "meta.helm.sh/release-namespace"
)

// IsAllowedAnnotation reports whether an annotation key may leave the cluster:
// only injector and impact.ai keys, never anything else (in particular never
// kubectl.kubernetes.io/last-applied-configuration, which holds whole manifests).
// The prefix is the part before the slash; Dynatrace uses subdomains.
func IsAllowedAnnotation(key string) bool {
	slash := strings.IndexByte(key, '/')
	if slash <= 0 || slash == len(key)-1 {
		return false
	}
	switch domain := key[:slash]; {
	case domain == "instrumentation.opentelemetry.io",
		domain == "impact.ai",
		domain == "admission.datadoghq.com",
		domain == "cloudwatch.aws.amazon.com",
		domain == "dynatrace.com",
		strings.HasSuffix(domain, ".dynatrace.com"):
		return true
	}
	return false
}

// CapAnnotationValue cuts an annotation value to MaxAnnotationValueLength bytes
// without splitting a UTF-8 character.
func CapAnnotationValue(value string) string {
	return truncate(value, MaxAnnotationValueLength)
}

// VersionInfo is the answer of GET /version.
type VersionInfo struct {
	GitVersion string `json:"gitVersion"`
}

// OwnerReference names the owner of an object.
type OwnerReference struct {
	Kind       string `json:"kind"`
	Name       string `json:"name"`
	Controller *bool  `json:"controller"`
}

// ControllerOf returns the owner marked as controller, if any.
func ControllerOf(refs []OwnerReference) (OwnerReference, bool) {
	for _, ref := range refs {
		if ref.Controller != nil && *ref.Controller {
			return ref, true
		}
	}
	return OwnerReference{}, false
}

// ObjectMeta is the part of metadata every list item needs. Of the labels only
// those of the impact.ai domain are kept.
type ObjectMeta struct {
	Name            string           `json:"name"`
	Namespace       string           `json:"namespace"`
	UID             string           `json:"uid"`
	OwnerReferences []OwnerReference `json:"ownerReferences"`
	Labels          ImpactLabels     `json:"labels"`
}

// Namespace is one item of GET /api/v1/namespaces.
type Namespace struct {
	Metadata ObjectMeta      `json:"metadata"`
	Status   NamespaceStatus `json:"status"`
}

// NamespaceStatus holds the lifecycle phase (Active or Terminating).
type NamespaceStatus struct {
	Phase string `json:"phase"`
}

// Node is one item of GET /api/v1/nodes. Only provider and region hints are kept.
type Node struct {
	Metadata NodeMeta   `json:"metadata"`
	Status   NodeStatus `json:"status"`
}

// NodeMeta keeps the few labels that identify the managed provider and region.
type NodeMeta struct {
	Labels NodeLabels `json:"labels"`
}

// NodeStatus carries the kubelet version, which also names managed providers.
type NodeStatus struct {
	NodeInfo NodeInfo `json:"nodeInfo"`
}

// NodeInfo is the node's system information.
type NodeInfo struct {
	KubeletVersion string `json:"kubeletVersion"`
}

// PodTemplate is the pod template of a workload.
type PodTemplate struct {
	Metadata TemplateMeta `json:"metadata"`
	Spec     PodSpec      `json:"spec"`
}

// TemplateMeta keeps allowlisted annotations and the impact.ai labels only.
type TemplateMeta struct {
	Annotations AllowedAnnotations `json:"annotations"`
	Labels      ImpactLabels       `json:"labels"`
}

// PodSpec is the part of a pod specification the inventory needs.
type PodSpec struct {
	ServiceAccountName string           `json:"serviceAccountName"`
	InitContainers     []NamedContainer `json:"initContainers"`
	Containers         []Container      `json:"containers"`
}

// NamedContainer is an init container, of which only the name is read.
type NamedContainer struct {
	Name string `json:"name"`
}

// Container holds names: the image, the first command element, environment
// variable names and where their values come from, and mount paths.
type Container struct {
	Name         string          `json:"name"`
	Image        string          `json:"image"`
	Command      FirstArg        `json:"command"`
	Env          []EnvVar        `json:"env"`
	EnvFrom      []EnvFromSource `json:"envFrom"`
	VolumeMounts []VolumeMount   `json:"volumeMounts"`
}

// EnvVar is an environment variable without its value.
type EnvVar struct {
	Name      string        `json:"name"`
	ValueFrom *EnvVarSource `json:"valueFrom"`
}

// EnvVarSource names the Secret behind a variable.
type EnvVarSource struct {
	SecretKeyRef *KeyRef `json:"secretKeyRef"`
}

// KeyRef is a reference to a key of a named Secret.
type KeyRef struct {
	Name string `json:"name"`
	Key  string `json:"key"`
}

// EnvFromSource names a Secret or ConfigMap whose keys become variables.
type EnvFromSource struct {
	SecretRef    *NameRef `json:"secretRef"`
	ConfigMapRef *NameRef `json:"configMapRef"`
}

// NameRef is a reference by name.
type NameRef struct {
	Name string `json:"name"`
}

// VolumeMount is where a volume is mounted in a container.
type VolumeMount struct {
	MountPath string `json:"mountPath"`
}

// ReplicatedSpec is the spec of Deployments and StatefulSets.
type ReplicatedSpec struct {
	Replicas *int32      `json:"replicas"`
	Template PodTemplate `json:"template"`
}

// ReplicatedStatus is the status of Deployments and StatefulSets.
type ReplicatedStatus struct {
	ReadyReplicas int32               `json:"readyReplicas"`
	Conditions    []WorkloadCondition `json:"conditions"`
}

// WorkloadCondition is one condition of a Deployment or a StatefulSet, such as ReplicaFailure. Of it the type, the
// status and the reason are read: the reason is a name the controller chose (FailedCreate). The message is not, and no
// field is declared for it: it can hold what an admission policy or a webhook wrote about a pod.
type WorkloadCondition struct {
	Type   string `json:"type"`
	Status string `json:"status"`
	Reason string `json:"reason"`
}

// Deployment is one item of GET /apis/apps/v1/deployments.
type Deployment struct {
	Metadata ObjectMeta       `json:"metadata"`
	Spec     ReplicatedSpec   `json:"spec"`
	Status   ReplicatedStatus `json:"status"`
}

// StatefulSet is one item of GET /apis/apps/v1/statefulsets.
type StatefulSet struct {
	Metadata ObjectMeta       `json:"metadata"`
	Spec     ReplicatedSpec   `json:"spec"`
	Status   ReplicatedStatus `json:"status"`
}

// TemplateSpec is a spec that only holds a pod template.
type TemplateSpec struct {
	Template PodTemplate `json:"template"`
}

// DaemonSetStatus counts the pods of a DaemonSet.
type DaemonSetStatus struct {
	DesiredNumberScheduled int32 `json:"desiredNumberScheduled"`
	NumberReady            int32 `json:"numberReady"`
}

// DaemonSet is one item of GET /apis/apps/v1/daemonsets.
type DaemonSet struct {
	Metadata ObjectMeta      `json:"metadata"`
	Spec     TemplateSpec    `json:"spec"`
	Status   DaemonSetStatus `json:"status"`
}

// JobTemplate is the template a CronJob creates Jobs from.
type JobTemplate struct {
	Spec TemplateSpec `json:"spec"`
}

// CronJobSpec is the part of a CronJob spec the inventory needs.
type CronJobSpec struct {
	Suspend     *bool       `json:"suspend"`
	JobTemplate JobTemplate `json:"jobTemplate"`
}

// ObjectReference names a referenced object.
type ObjectReference struct {
	Name string `json:"name"`
}

// CronJobStatus lists the Jobs that are running.
type CronJobStatus struct {
	Active []ObjectReference `json:"active"`
}

// CronJob is one item of GET /apis/batch/v1/cronjobs.
type CronJob struct {
	Metadata ObjectMeta    `json:"metadata"`
	Spec     CronJobSpec   `json:"spec"`
	Status   CronJobStatus `json:"status"`
}

// JobSpec is the part of a Job spec the inventory needs.
type JobSpec struct {
	Parallelism *int32      `json:"parallelism"`
	Template    PodTemplate `json:"template"`
}

// JobCondition is one condition of a Job, such as Complete or Failed.
type JobCondition struct {
	Type               string      `json:"type"`
	Status             string      `json:"status"`
	LastTransitionTime LenientTime `json:"lastTransitionTime"`
}

// JobStatus tells whether a Job runs or has finished.
type JobStatus struct {
	Active         int32          `json:"active"`
	Ready          *int32         `json:"ready"`
	CompletionTime LenientTime    `json:"completionTime"`
	Conditions     []JobCondition `json:"conditions"`
}

// Job is one item of GET /apis/batch/v1/jobs.
type Job struct {
	Metadata ObjectMeta `json:"metadata"`
	Spec     JobSpec    `json:"spec"`
	Status   JobStatus  `json:"status"`
}

// PodMeta keeps names, the UID, owners, the pod-template-hash label and allowlisted annotations.
type PodMeta struct {
	Name            string             `json:"name"`
	Namespace       string             `json:"namespace"`
	UID             string             `json:"uid"`
	OwnerReferences []OwnerReference   `json:"ownerReferences"`
	Labels          PodLabels          `json:"labels"`
	Annotations     AllowedAnnotations `json:"annotations"`
}

// PodCondition is one condition of a pod, such as Ready.
type PodCondition struct {
	Type   string `json:"type"`
	Status string `json:"status"`
}

// PodStatus tells whether a pod is live and ready, and why its init containers wait.
type PodStatus struct {
	Phase                 string                `json:"phase"`
	Conditions            []PodCondition        `json:"conditions"`
	InitContainerStatuses []InitContainerStatus `json:"initContainerStatuses"`
}

// InitContainerStatus is the state of one init container: its name and, while it waits, why.
type InitContainerStatus struct {
	Name  string         `json:"name"`
	State ContainerState `json:"state"`
}

// ContainerState is what a container is doing. Only a container that waits is read.
type ContainerState struct {
	Waiting *ContainerWaiting `json:"waiting"`
}

// ContainerWaiting is why a container waits. The reason is a name the kubelet chose (ImagePullBackOff); the message,
// which can hold what a registry or a policy answered, is not read and has no field.
type ContainerWaiting struct {
	Reason string `json:"reason"`
}

// Pod is one item of GET /api/v1/pods.
type Pod struct {
	Metadata PodMeta   `json:"metadata"`
	Spec     PodSpec   `json:"spec"`
	Status   PodStatus `json:"status"`
}

// ServiceReference is the in-cluster service a webhook calls.
type ServiceReference struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	Path      string `json:"path"`
}

// WebhookClientConfig keeps the service only, never the CA bundle.
type WebhookClientConfig struct {
	Service *ServiceReference `json:"service"`
}

// Webhook is one webhook of a configuration.
type Webhook struct {
	Name         string              `json:"name"`
	ClientConfig WebhookClientConfig `json:"clientConfig"`
}

// WebhookConfigMeta keeps the name and Helm's release annotations.
type WebhookConfigMeta struct {
	Name        string             `json:"name"`
	Annotations ReleaseAnnotations `json:"annotations"`
}

// MutatingWebhookConfiguration is one item of the admissionregistration list.
type MutatingWebhookConfiguration struct {
	Metadata WebhookConfigMeta `json:"metadata"`
	Webhooks []Webhook         `json:"webhooks"`
}

// FirstArg holds the first element of a command array and drops the rest, so
// arguments (which can carry keys) are never stored.
type FirstArg struct {
	First string
}

// UnmarshalJSON keeps command[0] only.
func (f *FirstArg) UnmarshalJSON(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	tok, err := dec.Token()
	if err != nil {
		return nil
	}
	if delim, ok := tok.(json.Delim); !ok || delim != '[' || !dec.More() {
		return nil
	}
	var first string
	if err := dec.Decode(&first); err == nil {
		f.First = first
	}
	return nil
}

// LenientTime is a timestamp that decodes to the zero time instead of failing.
type LenientTime struct {
	time.Time
}

// UnmarshalJSON accepts RFC 3339 strings and ignores anything else.
func (t *LenientTime) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil || s == "" {
		return nil
	}
	if parsed, err := time.Parse(time.RFC3339, s); err == nil {
		t.Time = parsed
	}
	return nil
}

// AllowedAnnotations keeps the allowlisted annotations and nothing else.
type AllowedAnnotations map[string]string

// UnmarshalJSON drops every key outside the allowlist while decoding.
func (a *AllowedAnnotations) UnmarshalJSON(data []byte) error {
	*a = decodeStringMap(data, IsAllowedAnnotation)
	return nil
}

// ReleaseAnnotations keeps only Helm's release annotations.
type ReleaseAnnotations map[string]string

// UnmarshalJSON drops every key except the Helm release markers.
func (a *ReleaseAnnotations) UnmarshalJSON(data []byte) error {
	*a = decodeStringMap(data, func(key string) bool {
		return key == HelmReleaseNameAnnotation || key == HelmReleaseNamespaceAnnotation
	})
	return nil
}

// ImpactLabel is one label of the impact.ai domain, such as impact.ai/opt-out.
type ImpactLabel struct {
	Key  string `json:"key"`
	Text string `json:"text"`
}

// ImpactLabels keeps the labels of the impact.ai domain and nothing else: opt-outs
// are said with them, and no other label of a workload or namespace leaves the
// cluster. It is a slice of pairs, not a map, so that the guard that walks every
// decode target (TestDecodeStructsAreNamesOnly) can tell it holds names only.
type ImpactLabels []ImpactLabel

// isImpactLabel reports whether key is a label of the impact.ai domain. The
// domain must match exactly, as for annotations: sub.impact.ai/x does not.
func isImpactLabel(key string) bool {
	slash := strings.IndexByte(key, '/')
	return slash > 0 && slash < len(key)-1 && key[:slash] == "impact.ai"
}

// MaxImpactLabels is how many impact.ai labels one object reports. impact.ai refuses a whole snapshot whose workload
// holds more, and a label whose name or value is not a Kubernetes label's: the API server only lets valid labels exist,
// but one odd object must never cost a cluster its inventory, so the reporter keeps to the grammar itself.
const MaxImpactLabels = 16

var (
	labelName  = regexp.MustCompile(`^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$`)
	labelValue = regexp.MustCompile(`^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$`)
)

// UnmarshalJSON reads a labels object and keeps the impact.ai ones whose name and value are a Kubernetes label's, sorted
// by key and at most MaxImpactLabels of them (the first by key). A value that is not a string is dropped without
// failing the object.
func (l *ImpactLabels) UnmarshalJSON(data []byte) error {
	kept := decodeStringMap(data, isImpactLabel)
	out := make(ImpactLabels, 0, len(kept))
	for key, text := range kept {
		if labelName.MatchString(key[strings.IndexByte(key, '/')+1:]) && labelValue.MatchString(text) {
			out = append(out, ImpactLabel{Key: key, Text: text})
		}
	}
	slices.SortFunc(out, func(a, b ImpactLabel) int { return strings.Compare(a.Key, b.Key) })
	if len(out) > MaxImpactLabels {
		out = out[:MaxImpactLabels]
	}
	*l = out
	return nil
}

// Map returns the labels as a map, or nil when there are none.
func (l ImpactLabels) Map() map[string]string {
	if len(l) == 0 {
		return nil
	}
	out := make(map[string]string, len(l))
	for _, label := range l {
		out[label.Key] = label.Text
	}
	return out
}

// PodLabels keeps the pod-template-hash label, which links pods to Deployments.
type PodLabels map[string]string

// UnmarshalJSON drops every label except pod-template-hash.
func (l *PodLabels) UnmarshalJSON(data []byte) error {
	*l = decodeStringMap(data, func(key string) bool { return key == "pod-template-hash" })
	return nil
}

var nodeLabelKeys = map[string]bool{
	"eks.amazonaws.com/nodegroup":              true,
	"eks.amazonaws.com/compute-type":           true,
	"cloud.google.com/gke-nodepool":            true,
	"kubernetes.azure.com/cluster":             true,
	"kubernetes.azure.com/role":                true,
	"topology.kubernetes.io/region":            true,
	"failure-domain.beta.kubernetes.io/region": true,
}

// NodeLabels keeps the labels that identify the provider and region.
type NodeLabels map[string]string

// UnmarshalJSON drops every label that is not a provider or region hint.
func (l *NodeLabels) UnmarshalJSON(data []byte) error {
	*l = decodeStringMap(data, func(key string) bool { return nodeLabelKeys[key] })
	return nil
}

// truncate cuts s to at most max bytes without splitting a UTF-8 character.
func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	s = s[:max]
	for len(s) > 0 && !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

// skipped swallows a JSON value without keeping it.
type skipped struct{}

func (*skipped) UnmarshalJSON([]byte) error { return nil }

// decodeStringMap reads a JSON object and keeps the string entries whose key
// passes keep. Other values are skipped without being stored. Values are capped
// at MaxAnnotationValueLength.
func decodeStringMap(data []byte, keep func(string) bool) map[string]string {
	out := map[string]string{}
	dec := json.NewDecoder(bytes.NewReader(data))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return out
	}
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return out
		}
		key, _ := keyTok.(string)
		if keep(key) {
			var value string
			if err := dec.Decode(&value); err == nil {
				out[key] = truncate(value, MaxAnnotationValueLength)
			}
			continue
		}
		var skip skipped
		if err := dec.Decode(&skip); err != nil {
			return out
		}
	}
	return out
}
