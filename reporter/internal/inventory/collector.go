// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/injector"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kube"
)

const (
	pageLimit = 500
	nodeLimit = 20

	// The Operator names its init container and mounts the payload like this.
	injectionInitContainer = "opentelemetry-auto-instrumentation-python"
	injectionMountPath     = "/otel-auto-instrumentation-python"
	// And the init container it adds for Node.js.
	injectionInitContainerNode = "opentelemetry-auto-instrumentation-nodejs"

	// Finished Jobs older than this are no longer workloads.
	finishedJobGrace = time.Hour

	// The cover pod puts this annotation, with the time, on the pods it annotated.
	autoCoveredAnnotation = "impact.ai/auto-covered"
)

// Cloud is the cloud identity of the cluster as the installer gave it. Every
// field is optional and what is empty stays out of the snapshot.
type Cloud struct {
	Provider  string // aws, azure or gcp
	AccountID string
	// Region replaces the region read from the nodes' topology label.
	Region string
	// ClusterID is the provider's own ID of the cluster, for example the EKS ARN.
	ClusterID string
}

// Options configure a Collector.
type Options struct {
	ClusterName string
	Cloud       Cloud
	// Include limits the inventory to these namespaces; empty means all.
	Include []string
	// Exclude adds to the namespaces that are never listed.
	Exclude []string
	// OwnNamespace is the namespace of the reporter, which is not listed.
	OwnNamespace string
	// Release identifies this Helm release, so its webhooks are not taken for
	// another injector's.
	Release injector.Release
	// InjectionDisabled is set when the chart did not install the Operator.
	InjectionDisabled bool
	// CoverFields adds the auto-cover marker and the impact.ai/* labels (workload and namespace) to the snapshot.
	CoverFields bool
	// Version and Instance describe this reporter in the snapshot.
	Version  string
	Instance string
	Now      func() time.Time
	Log      *slog.Logger
}

// Collector builds snapshots from the Kubernetes API.
type Collector struct {
	client *kube.Client
	opts   Options
	filter NamespaceFilter
}

// NewCollector returns a collector that reads through client.
func NewCollector(client *kube.Client, opts Options) *Collector {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &Collector{client: client, opts: opts, filter: NewNamespaceFilter(opts.Include, opts.Exclude, opts.OwnNamespace)}
}

type workloadKey struct{ namespace, kind, name string }

type nsName struct{ namespace, name string }

type entry struct {
	w        Workload
	injected map[string]bool // container name -> a live pod runs it injected
	// The facts of automatic coverage, reported when Options.CoverFields is set.
	labels    map[string]string // impact.ai/* labels of the object and of its pod template
	covered   string            // the earliest valid auto-covered marker of its live pods, RFC 3339 in UTC
	coveredAt time.Time
	// Why its pods do not start: the Deployment's ReplicaFailure reason, and the reasons the injected init container of
	// its live pods waits for.
	replicaFailure string
	initWaiting    map[string]bool
}

// notePod notes what a live pod of the workload says: which of its containers the Operator injected, when the cover pod
// marked it, and why its injected init container waits.
func (e *entry) notePod(p *kube.Pod) {
	for _, name := range injectedContainers(p) {
		e.injected[name] = true
	}
	if raw, ok := p.Metadata.Annotations[autoCoveredAnnotation]; ok {
		e.noteCovered(raw)
	}
	for _, status := range p.Status.InitContainerStatuses {
		if status.Name != injectionInitContainer && status.Name != injectionInitContainerNode {
			continue
		}
		if waiting := status.State.Waiting; waiting != nil && stallReason(waiting.Reason) != "" && !startingUp[waiting.Reason] {
			if e.initWaiting == nil {
				e.initWaiting = map[string]bool{}
			}
			e.initWaiting[waiting.Reason] = true
		}
	}
}

// startingUp are the reasons a container waits for while nothing is wrong yet.
var startingUp = map[string]bool{"ContainerCreating": true, "PodInitializing": true}

// reasonName is what a reason must look like to leave the cluster: a plain name in CamelCase, as the kubelet and the
// controllers write them. Anything else (free text, a key, a sentence) is not sent, whatever component wrote it.
var reasonName = regexp.MustCompile(`^[A-Z][A-Za-z0-9]{1,62}$`)

// stallReason returns the reason when it is a plain name, and "" when it is not.
func stallReason(reason string) string {
	if reasonName.MatchString(reason) {
		return reason
	}
	return ""
}

// replicaFailureReason is the reason of a ReplicaFailure condition that is true, "Unknown" when the reason is no plain
// name, and "" when there is no such condition.
func replicaFailureReason(conditions []kube.WorkloadCondition) string {
	for _, c := range conditions {
		if c.Type == "ReplicaFailure" && c.Status == "True" {
			if reason := stallReason(c.Reason); reason != "" {
				return reason
			}
			return "Unknown"
		}
	}
	return ""
}

// stall is what the workload reports of why its pods do not start, or nil when nothing is wrong.
func (e *entry) stall() *Stall {
	if e.replicaFailure == "" && len(e.initWaiting) == 0 {
		return nil
	}
	s := &Stall{ReplicaFailure: e.replicaFailure}
	for reason := range e.initWaiting {
		s.InitWaiting = append(s.InitWaiting, reason)
	}
	slices.Sort(s.InitWaiting)
	if len(s.InitWaiting) > MaxStallReasons {
		s.InitWaiting = s.InitWaiting[:MaxStallReasons]
	}
	return s
}

// noteCovered keeps the earliest of the markers it is given. A value that is not a time is ignored.
func (e *entry) noteCovered(raw string) {
	at, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return
	}
	if e.covered == "" || at.Before(e.coveredAt) {
		e.covered, e.coveredAt = at.UTC().Format(time.RFC3339), at
	}
}

type state struct {
	workloads map[workloadKey]*entry
	jobCron   map[nsName]string // Job -> the CronJob that owns it
}

func (s *state) addSpec(ns, kind, name string, annotations map[string]string, spec kube.PodSpec, desired, ready int) {
	sa := spec.ServiceAccountName
	if sa == "" {
		sa = "default"
	}
	w := Workload{
		Namespace:      ns,
		Kind:           kind,
		Name:           name,
		Pods:           Pods{Desired: max(desired, 0), Ready: max(ready, 0)},
		ServiceAccount: sa,
		Annotations:    filterAnnotations(annotations),
		Containers:     make([]Container, 0, len(spec.Containers)),
	}
	for _, c := range spec.Containers {
		w.Containers = append(w.Containers, containerFacts(c))
	}
	s.workloads[workloadKey{ns, kind, name}] = &entry{w: w, injected: map[string]bool{}}
}

// addTemplate adds a workload with a pod template. meta is the workload's own metadata: of it, and of the template,
// only the impact.ai labels are kept, the template's winning where both have one.
func (s *state) addTemplate(ns, kind, name string, meta kube.ObjectMeta, t kube.PodTemplate, desired, ready int) {
	s.addSpec(ns, kind, name, t.Metadata.Annotations, t.Spec, desired, ready)
	labels := meta.Labels.Map()
	for key, text := range t.Metadata.Labels.Map() {
		if labels == nil {
			labels = map[string]string{}
		}
		labels[key] = text
	}
	s.workloads[workloadKey{ns, kind, name}].labels = capLabels(labels)
}

// capLabels keeps the first kube.MaxImpactLabels labels by key. Each object is capped when it is read; the two together
// (a workload's own and its template's) can still be more than impact.ai reads of one workload.
func capLabels(labels map[string]string) map[string]string {
	if len(labels) <= kube.MaxImpactLabels {
		return labels
	}
	keys := make([]string, 0, len(labels))
	for key := range labels {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	kept := make(map[string]string, kube.MaxImpactLabels)
	for _, key := range keys[:kube.MaxImpactLabels] {
		kept[key] = labels[key]
	}
	return kept
}

// ownerKey finds the top-level workload a pod belongs to.
func (s *state) ownerKey(ns string, owner kube.OwnerReference, pod *kube.Pod) (workloadKey, bool) {
	switch owner.Kind {
	case "StatefulSet", "DaemonSet":
		return workloadKey{ns, owner.Kind, owner.Name}, true
	case "ReplicaSet":
		// A Deployment's ReplicaSet is named <deployment>-<pod-template-hash>.
		hash := pod.Metadata.Labels["pod-template-hash"]
		suffix := "-" + hash
		if hash == "" || !strings.HasSuffix(owner.Name, suffix) || len(owner.Name) == len(suffix) {
			return workloadKey{}, false
		}
		return workloadKey{ns, "Deployment", strings.TrimSuffix(owner.Name, suffix)}, true
	case "Job":
		if cron, ok := s.jobCron[nsName{ns, owner.Name}]; ok {
			return workloadKey{ns, "CronJob", cron}, true
		}
		return workloadKey{ns, "Job", owner.Name}, true
	}
	return workloadKey{}, false
}

func isLive(phase string) bool {
	return phase == "" || phase == "Pending" || phase == "Running"
}

func podReady(p *kube.Pod) bool {
	for _, c := range p.Status.Conditions {
		if c.Type == "Ready" {
			return c.Status == "True"
		}
	}
	return false
}

// injectedContainers lists the containers of a pod that the Operator injected:
// the pod has its init container and the container mounts its volume.
func injectedContainers(p *kube.Pod) []string {
	hasInit := false
	for _, ic := range p.Spec.InitContainers {
		if ic.Name == injectionInitContainer {
			hasInit = true
			break
		}
	}
	if !hasInit {
		return nil
	}
	var out []string
	for _, c := range p.Spec.Containers {
		for _, m := range c.VolumeMounts {
			if m.MountPath == injectionMountPath {
				out = append(out, c.Name)
				break
			}
		}
	}
	return out
}

// jobFinishedAt tells whether a Job has finished and when.
func jobFinishedAt(s kube.JobStatus) (time.Time, bool) {
	for _, cond := range s.Conditions {
		if (cond.Type == "Complete" || cond.Type == "Failed") && cond.Status == "True" {
			if !s.CompletionTime.IsZero() {
				return s.CompletionTime.Time, true
			}
			return cond.LastTransitionTime.Time, true
		}
	}
	return time.Time{}, false
}

func (c *Collector) listErr(what string, err error) error {
	return fmt.Errorf("list %s: %w", what, err)
}

// Snapshot reads the cluster and builds a snapshot with the given sequence. A
// snapshot with missing workloads would make the server think they were
// removed, so any failure to read workloads or pods fails the whole snapshot.
// Nodes, webhook configurations and namespaces are best effort.
func (c *Collector) Snapshot(ctx context.Context, sequence int64) (*Snapshot, error) {
	log := c.opts.Log
	now := c.opts.Now()

	cluster, err := c.cluster(ctx)
	if err != nil {
		return nil, err
	}
	owner := c.clusterOwner(ctx)
	terminating, namespaceLabels := c.namespaces(ctx)
	skip := func(ns string) bool { return !c.filter.Allows(ns) || terminating[ns] }

	st := &state{workloads: map[workloadKey]*entry{}, jobCron: map[nsName]string{}}

	// Jobs first: pods of a Job map to the CronJob that owns the Job.
	err = kube.List(ctx, c.client, "/apis/batch/v1/jobs", pageLimit, func(j *kube.Job) error {
		ns := j.Metadata.Namespace
		if skip(ns) {
			return nil
		}
		if ref, ok := kube.ControllerOf(j.Metadata.OwnerReferences); ok && ref.Kind == "CronJob" {
			st.jobCron[nsName{ns, j.Metadata.Name}] = ref.Name
			return nil
		}
		if at, done := jobFinishedAt(j.Status); done && !at.IsZero() && at.Before(now.Add(-finishedJobGrace)) {
			return nil
		}
		desired, ready := 1, 0
		if j.Spec.Parallelism != nil {
			desired = int(*j.Spec.Parallelism)
		}
		if j.Status.Ready != nil {
			ready = int(*j.Status.Ready)
		}
		st.addTemplate(ns, "Job", j.Metadata.Name, j.Metadata, j.Spec.Template, desired, ready)
		return nil
	})
	if err != nil {
		return nil, c.listErr("jobs", err)
	}

	err = kube.List(ctx, c.client, "/apis/batch/v1/cronjobs", pageLimit, func(cj *kube.CronJob) error {
		ns := cj.Metadata.Namespace
		if skip(ns) {
			return nil
		}
		desired := 1
		if cj.Spec.Suspend != nil && *cj.Spec.Suspend {
			desired = 0
		}
		st.addTemplate(ns, "CronJob", cj.Metadata.Name, cj.Metadata, cj.Spec.JobTemplate.Spec.Template, desired, len(cj.Status.Active))
		return nil
	})
	if err != nil {
		return nil, c.listErr("cronjobs", err)
	}

	err = kube.List(ctx, c.client, "/apis/apps/v1/deployments", pageLimit, func(d *kube.Deployment) error {
		ns := d.Metadata.Namespace
		if skip(ns) {
			return nil
		}
		desired := 1
		if d.Spec.Replicas != nil {
			desired = int(*d.Spec.Replicas)
		}
		st.addTemplate(ns, "Deployment", d.Metadata.Name, d.Metadata, d.Spec.Template, desired, int(d.Status.ReadyReplicas))
		st.workloads[workloadKey{ns, "Deployment", d.Metadata.Name}].replicaFailure = replicaFailureReason(d.Status.Conditions)
		return nil
	})
	if err != nil {
		return nil, c.listErr("deployments", err)
	}

	err = kube.List(ctx, c.client, "/apis/apps/v1/statefulsets", pageLimit, func(s *kube.StatefulSet) error {
		ns := s.Metadata.Namespace
		if skip(ns) {
			return nil
		}
		desired := 1
		if s.Spec.Replicas != nil {
			desired = int(*s.Spec.Replicas)
		}
		st.addTemplate(ns, "StatefulSet", s.Metadata.Name, s.Metadata, s.Spec.Template, desired, int(s.Status.ReadyReplicas))
		return nil
	})
	if err != nil {
		return nil, c.listErr("statefulsets", err)
	}

	err = kube.List(ctx, c.client, "/apis/apps/v1/daemonsets", pageLimit, func(d *kube.DaemonSet) error {
		ns := d.Metadata.Namespace
		if skip(ns) {
			return nil
		}
		st.addTemplate(ns, "DaemonSet", d.Metadata.Name, d.Metadata, d.Spec.Template, int(d.Status.DesiredNumberScheduled), int(d.Status.NumberReady))
		return nil
	})
	if err != nil {
		return nil, c.listErr("daemonsets", err)
	}

	// Pods last. They add bare Pods as workloads and tell which workloads run
	// injected. Nothing else is taken from a pod.
	// Finished pods are of no use here, so the server does not send them.
	livePods := kube.ListQuery{Limit: pageLimit, FieldSelector: "status.phase!=Succeeded,status.phase!=Failed"}
	err = kube.ListWith(ctx, c.client, "/api/v1/pods", livePods, func(p *kube.Pod) error {
		ns := p.Metadata.Namespace
		if skip(ns) || !isLive(p.Status.Phase) {
			return nil
		}
		ref, controlled := kube.ControllerOf(p.Metadata.OwnerReferences)
		if !controlled {
			ready := 0
			if podReady(p) {
				ready = 1
			}
			st.addSpec(ns, "Pod", p.Metadata.Name, p.Metadata.Annotations, p.Spec, 1, ready)
			st.workloads[workloadKey{ns, "Pod", p.Metadata.Name}].notePod(p)
			return nil
		}
		key, ok := st.ownerKey(ns, ref, p)
		if !ok {
			return nil
		}
		if e := st.workloads[key]; e != nil {
			e.notePod(p)
		}
		return nil
	})
	if err != nil {
		return nil, c.listErr("pods", err)
	}

	workloads := make([]Workload, 0, len(st.workloads))
	for _, e := range st.workloads {
		for i := range e.w.Containers {
			e.w.Containers[i].Injected.Python = e.injected[e.w.Containers[i].Name]
		}
		if c.opts.CoverFields {
			e.w.AutoCovered = e.covered
			e.w.Labels = e.labels
			e.w.NamespaceLabels = namespaceLabels[e.w.Namespace]
			e.w.Stall = e.stall()
		}
		workloads = append(workloads, e.w)
	}
	sortWorkloads(workloads)

	mode := ModeReportOnly
	if !c.opts.InjectionDisabled && owner == "" {
		mode = ModeInject
	}
	snap := &Snapshot{
		Schema:     SchemaV1,
		Cluster:    cluster,
		Reporter:   ReporterInfo{Version: c.opts.Version, Instance: c.opts.Instance, Mode: mode},
		ObservedAt: now.UTC().Format(time.RFC3339),
		Sequence:   sequence,
		Workloads:  workloads,
	}
	if owner != "" {
		snap.Reporter.ClusterOwner = &owner
	}
	log.Debug("snapshot collected", "workloads", len(workloads), "provider", cluster.Provider, "mode", mode, "clusterOwner", owner)
	return snap, nil
}

// cluster reads who the cluster is: the UID of kube-system (the cluster ID), the
// Kubernetes version, and best effort the provider and region.
func (c *Collector) cluster(ctx context.Context) (Cluster, error) {
	var version kube.VersionInfo
	if err := c.client.GetJSON(ctx, "/version", nil, &version); err != nil {
		return Cluster{}, fmt.Errorf("read the Kubernetes version: %w", err)
	}
	var kubeSystem kube.Namespace
	if err := c.client.GetJSON(ctx, "/api/v1/namespaces/kube-system", nil, &kubeSystem); err != nil {
		return Cluster{}, fmt.Errorf("read the kube-system namespace, whose UID is the cluster ID: %w", err)
	}
	if kubeSystem.Metadata.UID == "" {
		return Cluster{}, errors.New("the kube-system namespace has no UID")
	}
	provider, region := c.providerAndRegion(ctx)
	cluster := Cluster{
		UID:               kubeSystem.Metadata.UID,
		Provider:          provider,
		Region:            region,
		KubernetesVersion: cleanVersion(version.GitVersion),
	}
	if c.opts.ClusterName != "" {
		name := c.opts.ClusterName
		cluster.Name = &name
	}
	cloud := c.opts.Cloud
	cluster.CloudProvider = cloud.Provider
	cluster.AccountID = cloud.AccountID
	cluster.ARN = cloud.ClusterID
	if cloud.Region != "" {
		region := cloud.Region
		cluster.Region = &region
	}
	return cluster, nil
}

// UninstallNotice builds the final snapshot of a cluster whose reporter is being
// uninstalled: the same cluster identity, mode "uninstalled", no cluster owner
// and no workloads. Nothing but the cluster is read, so it holds no workload
// names and no values. The caller picks the sequence, which must be above every
// sequence the running reporter sent.
func (c *Collector) UninstallNotice(ctx context.Context, sequence int64) (*Snapshot, error) {
	cluster, err := c.cluster(ctx)
	if err != nil {
		return nil, err
	}
	return &Snapshot{
		Schema:     SchemaV1,
		Cluster:    cluster,
		Reporter:   ReporterInfo{Version: c.opts.Version, Instance: c.opts.Instance, Mode: ModeUninstalled},
		ObservedAt: c.opts.Now().UTC().Format(time.RFC3339),
		Sequence:   sequence,
		Workloads:  []Workload{},
	}, nil
}

// providerAndRegion reads the first nodes. A failure is not fatal: the cluster
// is then reported as "other" without a region.
func (c *Collector) providerAndRegion(ctx context.Context) (string, *string) {
	nodes, err := kube.FirstPage[kube.Node](ctx, c.client, "/api/v1/nodes", nodeLimit)
	if err != nil {
		c.opts.Log.Warn("could not read nodes; the provider and region stay unknown", "error", err)
		return "other", nil
	}
	return providerAndRegion(nodes)
}

// clusterOwner finds another injector from the mutating webhook configurations.
// A failure is not fatal: the cluster is then reported as having no other owner.
func (c *Collector) clusterOwner(ctx context.Context) string {
	var configs []kube.MutatingWebhookConfiguration
	err := kube.List(ctx, c.client, "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations", pageLimit,
		func(cfg *kube.MutatingWebhookConfiguration) error {
			configs = append(configs, *cfg)
			return nil
		})
	if err != nil {
		c.opts.Log.Warn("could not read webhook configurations; other injectors cannot be detected", "error", err)
		return ""
	}
	return injector.Detect(configs, c.opts.Release)
}

// namespaces lists the namespaces once and says which are being deleted and what impact.ai labels each has. A
// failure is not fatal: they are then simply not skipped, and no namespace has labels.
func (c *Collector) namespaces(ctx context.Context) (terminating map[string]bool, labels map[string]map[string]string) {
	terminating, labels = map[string]bool{}, map[string]map[string]string{}
	err := kube.List(ctx, c.client, "/api/v1/namespaces", pageLimit, func(ns *kube.Namespace) error {
		if ns.Status.Phase == "Terminating" {
			terminating[ns.Metadata.Name] = true
		}
		if l := ns.Metadata.Labels.Map(); l != nil {
			labels[ns.Metadata.Name] = l
		}
		return nil
	})
	if err != nil {
		c.opts.Log.Warn("could not read namespaces; terminating namespaces are not skipped", "error", err)
		return map[string]bool{}, map[string]map[string]string{}
	}
	return terminating, labels
}
