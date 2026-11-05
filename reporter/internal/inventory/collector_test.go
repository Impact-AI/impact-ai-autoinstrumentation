// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/injector"
)

func wl(s *Snapshot) []string { return keys(s) }

func TestWorkloadKindsAndPodCounts(t *testing.T) {
	f := newFake(t)
	app := template(nil, ctr("app"))
	f.list(pathDeployments,
		deployment("shop", "api", 3, 2, app),
		replicated("shop", "idle", nil, 0, app), // replicas unset means 1
		deployment("shop", "off", 0, 0, app),
	)
	f.list(pathStatefulSets, deployment("shop", "db", 2, 2, app))
	f.list(pathDaemonSets, daemonSet("shop", "agent", 4, 3, app))
	f.list(pathCronJobs,
		cronJob("shop", "nightly", false, 1, app),
		cronJob("shop", "paused", true, 0, app),
	)
	f.list(pathJobs,
		job("shop", "migrate", ptr(3), ptr(2), app),
		job("shop", "single", nil, nil, app), // parallelism unset means 1, ready unset means 0
		job("shop", "nightly-29000", nil, nil, app, controller("CronJob", "nightly")),
	)
	f.list(pathPods,
		pod("shop", "debug", app, nil, ready(true)),
		pod("shop", "pending", app, nil, ready(false), withPhase("Pending")),
		pod("shop", "managed-by-rollouts", app, ptr(controller("Rollout", "r"))),
		pod("shop", "static-etcd", app, ptr(controller("Node", "node-1"))),
		pod("shop", "finished", app, nil, withPhase("Succeeded")), // a finished bare pod is not a workload
		pod("shop", "crashed", app, nil, withPhase("Failed")),
	)

	snap := f.snapshot(Options{})

	want := []string{
		"shop/CronJob/nightly", "shop/CronJob/paused",
		"shop/DaemonSet/agent",
		"shop/Deployment/api", "shop/Deployment/idle", "shop/Deployment/off",
		"shop/Job/migrate", "shop/Job/single",
		"shop/Pod/debug", "shop/Pod/pending",
		"shop/StatefulSet/db",
	}
	if got := wl(snap); !reflect.DeepEqual(got, want) {
		t.Fatalf("workloads (sorted by namespace, kind, name) =\n%v\nwant\n%v", got, want)
	}
	counts := map[string][2]int{
		"shop/CronJob/nightly": {1, 1}, "shop/CronJob/paused": {0, 0},
		"shop/DaemonSet/agent": {4, 3},
		"shop/Deployment/api":  {3, 2}, "shop/Deployment/idle": {1, 0}, "shop/Deployment/off": {0, 0},
		"shop/Job/migrate": {3, 2}, "shop/Job/single": {1, 0},
		"shop/Pod/debug": {1, 1}, "shop/Pod/pending": {1, 0},
		"shop/StatefulSet/db": {2, 2},
	}
	for _, w := range snap.Workloads {
		key := w.Namespace + "/" + w.Kind + "/" + w.Name
		if got := [2]int{w.Pods.Desired, w.Pods.Ready}; got != counts[key] {
			t.Errorf("%s pods = %v, want %v", key, got, counts[key])
		}
		if w.ServiceAccount != "sa-1" {
			t.Errorf("%s serviceAccount = %q", key, w.ServiceAccount)
		}
	}
}

func TestServiceAccountDefaultsToDefault(t *testing.T) {
	f := newFake(t)
	tmpl := template(nil, ctr("app"))
	delete(tmpl["spec"].(obj), "serviceAccountName")
	f.list(pathDeployments, deployment("shop", "api", 1, 1, tmpl))
	snap := f.snapshot(Options{})
	if got := findWorkload(t, snap, "shop", "Deployment", "api").ServiceAccount; got != "default" {
		t.Errorf("serviceAccount = %q, want default", got)
	}
}

func TestFinishedJobsAgeOut(t *testing.T) {
	f := newFake(t)
	app := template(nil, ctr("app"))
	f.list(pathJobs,
		finishedJob("shop", "done-long-ago", fixedNow.Add(-3*time.Hour), app),
		finishedJob("shop", "done-just-now", fixedNow.Add(-5*time.Minute), app),
		failedJob("shop", "failed-long-ago", fixedNow.Add(-2*time.Hour), app),
		failedJob("shop", "failed-just-now", fixedNow.Add(-10*time.Minute), app),
		job("shop", "running", nil, nil, app),
	)
	snap := f.snapshot(Options{})
	want := []string{"shop/Job/done-just-now", "shop/Job/failed-just-now", "shop/Job/running"}
	if got := wl(snap); !reflect.DeepEqual(got, want) {
		t.Errorf("jobs = %v, want %v", got, want)
	}
}

func TestPodsAreMappedToTheirWorkloadForInjectionState(t *testing.T) {
	f := newFake(t)
	two := func() obj { return template(nil, ctr("app"), ctr("sidecar")) }
	f.list(pathDeployments,
		deployment("shop", "api", 2, 2, two()),
		deployment("shop", "wrong-hash", 1, 1, two()),
		deployment("shop", "old-pod-only", 1, 1, two()),
		deployment("shop", "no-init", 1, 1, two()),
		deployment("shop", "no-mount", 1, 1, two()),
		deployment("shop", "rolling", 1, 1, two()),
	)
	f.list(pathStatefulSets, deployment("shop", "db", 1, 1, two()))
	f.list(pathDaemonSets, daemonSet("shop", "agent", 1, 1, two()))
	f.list(pathCronJobs, cronJob("shop", "nightly", false, 0, two()))
	f.list(pathJobs,
		job("shop", "nightly-29000", nil, nil, two(), controller("CronJob", "nightly")),
		job("shop", "migrate", nil, nil, two()),
	)
	both := []string{"app", "sidecar"}
	f.list(pathPods,
		// A Deployment's pod: the ReplicaSet name ends with the pod-template-hash.
		injectedPod("shop", "api-1", ptr(controller("ReplicaSet", "api-5d9c7b6f8d")), []string{"app"}, both, withHash("5d9c7b6f8d")),
		// The hash does not match the ReplicaSet name: not mapped.
		injectedPod("shop", "wrong-hash-1", ptr(controller("ReplicaSet", "wrong-hash-5d9c7b6f8d")), both, both, withHash("abcdef")),
		// Only a finished pod was injected: no live pod, so not injected.
		injectedPod("shop", "old-pod-only-1", ptr(controller("ReplicaSet", "old-pod-only-7c")), both, both, withHash("7c"), withPhase("Succeeded")),
		injectedPod("shop", "old-pod-only-2", ptr(controller("ReplicaSet", "old-pod-only-7c")), both, both, withHash("7c"), withPhase("Failed")),
		// Mount without the init container, and init container without the mount.
		pod("shop", "no-init-1", template(nil, ctr("app", mount("/otel-auto-instrumentation-python")), ctr("sidecar")), ptr(controller("ReplicaSet", "no-init-9f")), withHash("9f")),
		pod("shop", "no-mount-1", template(nil, ctr("app"), ctr("sidecar")), ptr(controller("ReplicaSet", "no-mount-9f")), withHash("9f"), withInit("opentelemetry-auto-instrumentation-python")),
		// A rollout: the old pod is not injected, the new one is. Any live injected pod counts.
		pod("shop", "rolling-old", template(nil, ctr("app"), ctr("sidecar")), ptr(controller("ReplicaSet", "rolling-1a")), withHash("1a")),
		injectedPod("shop", "rolling-new", ptr(controller("ReplicaSet", "rolling-2b")), []string{"app", "sidecar"}, both, withHash("2b"), withPhase("Pending")),
		injectedPod("shop", "db-0", ptr(controller("StatefulSet", "db")), []string{"sidecar"}, both),
		injectedPod("shop", "agent-x", ptr(controller("DaemonSet", "agent")), []string{"app"}, both),
		injectedPod("shop", "nightly-29000-abc", ptr(controller("Job", "nightly-29000")), []string{"app"}, both),
		injectedPod("shop", "migrate-xyz", ptr(controller("Job", "migrate")), []string{"app"}, both),
		injectedPod("shop", "standalone", nil, []string{"app"}, []string{"app"}),
	)

	snap := f.snapshot(Options{})

	injected := func(ns, kind, name string) map[string]bool {
		out := map[string]bool{}
		for _, c := range findWorkload(t, snap, ns, kind, name).Containers {
			out[c.Name] = c.Injected.Python
		}
		return out
	}
	cases := []struct {
		kind, name string
		want       map[string]bool
	}{
		{"Deployment", "api", map[string]bool{"app": true, "sidecar": false}},
		{"Deployment", "wrong-hash", map[string]bool{"app": false, "sidecar": false}},
		{"Deployment", "old-pod-only", map[string]bool{"app": false, "sidecar": false}},
		{"Deployment", "no-init", map[string]bool{"app": false, "sidecar": false}},
		{"Deployment", "no-mount", map[string]bool{"app": false, "sidecar": false}},
		{"Deployment", "rolling", map[string]bool{"app": true, "sidecar": true}},
		{"StatefulSet", "db", map[string]bool{"app": false, "sidecar": true}},
		{"DaemonSet", "agent", map[string]bool{"app": true, "sidecar": false}},
		{"CronJob", "nightly", map[string]bool{"app": true, "sidecar": false}},
		{"Job", "migrate", map[string]bool{"app": true, "sidecar": false}},
		{"Pod", "standalone", map[string]bool{"app": true}},
	}
	for _, tc := range cases {
		if got := injected("shop", tc.kind, tc.name); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s/%s injected = %v, want %v", tc.kind, tc.name, got, tc.want)
		}
	}
}

func TestNamespaceFilters(t *testing.T) {
	f := newFake(t)
	app := template(nil, ctr("app"))
	var deps []any
	for _, ns := range []string{"kube-system", "kube-public", "kube-node-lease", "impact-system", "shop", "batch", "dying", "data"} {
		deps = append(deps, deployment(ns, "web", 1, 1, app))
	}
	f.list(pathDeployments, deps...)
	f.list(pathNamespaces, namespace("kube-system"), namespace("shop"), terminatingNamespace("dying"))
	f.list(pathPods, pod("kube-system", "bare", app, nil), pod("shop", "bare", app, nil), pod("dying", "bare", app, nil))

	cases := []struct {
		name string
		opts Options
		want []string
	}{
		{"defaults skip system namespaces, our own and terminating ones", Options{OwnNamespace: "impact-system"},
			[]string{"batch/Deployment/web", "data/Deployment/web", "shop/Deployment/web", "shop/Pod/bare"}},
		{"without an own namespace only the system ones are skipped", Options{},
			[]string{"batch/Deployment/web", "data/Deployment/web", "impact-system/Deployment/web", "shop/Deployment/web", "shop/Pod/bare"}},
		{"include restricts", Options{OwnNamespace: "impact-system", Include: []string{"shop", "data"}},
			[]string{"data/Deployment/web", "shop/Deployment/web", "shop/Pod/bare"}},
		{"include cannot bring back a system namespace", Options{OwnNamespace: "impact-system", Include: []string{"kube-system", "kube-public", "impact-system", "shop"}},
			[]string{"shop/Deployment/web", "shop/Pod/bare"}},
		{"exclude adds to the defaults", Options{OwnNamespace: "impact-system", Exclude: []string{"batch", "data"}},
			[]string{"shop/Deployment/web", "shop/Pod/bare"}},
		{"exclude wins over include", Options{OwnNamespace: "impact-system", Include: []string{"shop", "data"}, Exclude: []string{"data"}},
			[]string{"shop/Deployment/web", "shop/Pod/bare"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := wl(f.snapshot(tc.opts)); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("workloads =\n%v\nwant\n%v", got, tc.want)
			}
		})
	}
}

func TestNamespaceFilterUnit(t *testing.T) {
	filter := NewNamespaceFilter([]string{"a", " b ", ""}, []string{"c", ""}, "own")
	for ns, want := range map[string]bool{
		"a": true, "b": true, "c": false, "d": false, "own": false, "kube-system": false, "kube-public": false, "kube-node-lease": false,
	} {
		if got := filter.Allows(ns); got != want {
			t.Errorf("Allows(%q) = %v, want %v", ns, got, want)
		}
	}
	open := NewNamespaceFilter(nil, nil, "")
	for ns, want := range map[string]bool{"anything": true, "kube-system": false, "default": true} {
		if got := open.Allows(ns); got != want {
			t.Errorf("open filter Allows(%q) = %v, want %v", ns, got, want)
		}
	}
}

func TestClusterBlock(t *testing.T) {
	cases := []struct {
		name         string
		nodes        []any
		gitVersion   string
		wantProvider string
		wantRegion   *string
		wantVersion  string
	}{
		{"eks by nodegroup label", []any{node(map[string]string{"eks.amazonaws.com/nodegroup": "ng", "topology.kubernetes.io/region": "us-east-1"}, "v1.31.2")}, "v1.31.2-eks-abc", "eks", ptr("us-east-1"), "1.31.2"},
		{"eks by compute type", []any{node(map[string]string{"eks.amazonaws.com/compute-type": "fargate"}, "v1.31.2")}, "v1.31.2", "eks", nil, "1.31.2"},
		{"eks by kubelet version", []any{node(nil, "v1.31.2-eks-abc123")}, "v1.31.2", "eks", nil, "1.31.2"},
		{"gke by nodepool label", []any{node(map[string]string{"cloud.google.com/gke-nodepool": "p", "failure-domain.beta.kubernetes.io/region": "europe-west1"}, "v1.30.5")}, "v1.30.5-gke.1443001", "gke", ptr("europe-west1"), "1.30.5"},
		{"gke by kubelet version", []any{node(nil, "v1.30.5-gke.1443001")}, "v1.30.5", "gke", nil, "1.30.5"},
		{"aks by cluster label", []any{node(map[string]string{"kubernetes.azure.com/cluster": "MC_x", "topology.kubernetes.io/region": "westeurope"}, "v1.29.9")}, "v1.29.9", "aks", ptr("westeurope"), "1.29.9"},
		{"aks by role label", []any{node(map[string]string{"kubernetes.azure.com/role": "agent"}, "v1.29.9")}, "v1.29.9", "aks", nil, "1.29.9"},
		{"kind is other", []any{node(nil, "v1.34.11")}, "v1.34.11", "other", nil, "1.34.11"},
		{"k3s suffix", []any{node(nil, "v1.30.3+k3s1")}, "v1.30.3+k3s1", "other", nil, "1.30.3"},
		{"the region label wins over the legacy one", []any{node(map[string]string{"topology.kubernetes.io/region": "r1", "failure-domain.beta.kubernetes.io/region": "r2"}, "v1.31.2")}, "v1.31.2", "other", ptr("r1"), "1.31.2"},
		{"any of the first nodes can tell", []any{node(nil, "v1.31.2"), node(map[string]string{"eks.amazonaws.com/nodegroup": "ng", "topology.kubernetes.io/region": "eu-west-1"}, "v1.31.2")}, "v1.31.2", "eks", ptr("eu-west-1"), "1.31.2"},
		{"no nodes", nil, "v1.31.2", "other", nil, "1.31.2"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t)
			f.srv.Handle(pathVersion, obj{"gitVersion": tc.gitVersion})
			f.list(pathNodes, tc.nodes...)
			snap := f.snapshot(Options{})
			c := snap.Cluster
			if c.Provider != tc.wantProvider || c.KubernetesVersion != tc.wantVersion || !reflect.DeepEqual(c.Region, tc.wantRegion) {
				t.Errorf("cluster = provider %q region %v version %q, want %q %v %q", c.Provider, deref(c.Region), c.KubernetesVersion, tc.wantProvider, deref(tc.wantRegion), tc.wantVersion)
			}
			if c.UID != "4e5622e7-f420-499c-9662-50af7f8a64b9" {
				t.Errorf("uid = %q, want the UID of the kube-system namespace", c.UID)
			}
		})
	}
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}

func TestVersionStrings(t *testing.T) {
	for in, want := range map[string]string{
		"v1.31.2": "1.31.2", "v1.31.2-eks-abc": "1.31.2", "v1.30.3+k3s1": "1.30.3", "1.29.0": "1.29.0",
		"v1.29.1-gke.1000+meta": "1.29.1", "": "", "  v1.2.3  ": "1.2.3",
	} {
		if got := cleanVersion(in); got != want {
			t.Errorf("cleanVersion(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestClusterNameIsNullUntilConfigured(t *testing.T) {
	f := newFake(t)
	if snap := f.snapshot(Options{}); snap.Cluster.Name != nil {
		t.Errorf("name = %v, want null", *snap.Cluster.Name)
	}
	if snap := f.snapshot(Options{ClusterName: "prod-eks"}); snap.Cluster.Name == nil || *snap.Cluster.Name != "prod-eks" {
		t.Errorf("name = %v", snap.Cluster.Name)
	}
}

// clusterOnTheWire is the "cluster" object of a snapshot as ingest receives it.
func clusterOnTheWire(t *testing.T, s *Snapshot) map[string]any {
	t.Helper()
	var top map[string]any
	if err := json.Unmarshal([]byte(mustJSON(t, s)), &top); err != nil {
		t.Fatal(err)
	}
	return top["cluster"].(map[string]any)
}

func TestCloudIdentityStaysOutOfTheClusterBlockUntilGiven(t *testing.T) {
	f := newFake(t)
	cluster := clusterOnTheWire(t, f.snapshot(Options{ClusterName: "prod-eks"}))
	for _, key := range []string{"cloudProvider", "accountId", "arn"} {
		if v, ok := cluster[key]; ok {
			t.Errorf("cluster has %q = %v although nothing was given", key, v)
		}
	}
	if len(cluster) != 5 {
		t.Errorf("cluster = %v, want uid, name, provider, region and kubernetesVersion only", cluster)
	}
}

func TestCloudIdentityIsAddedToTheClusterBlock(t *testing.T) {
	f := newFake(t)
	f.list(pathNodes, node(map[string]string{"eks.amazonaws.com/nodegroup": "ng", "topology.kubernetes.io/region": "us-east-1"}, "v1.31.2-eks-abc"))
	snap := f.snapshot(Options{Cloud: Cloud{
		Provider: "aws", AccountID: "012345678901", Region: "eu-west-1",
		ClusterID: "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks",
	}})
	cluster := clusterOnTheWire(t, snap)
	want := map[string]any{
		"cloudProvider": "aws",
		"accountId":     "012345678901",
		"region":        "eu-west-1", // what the installer gave wins over the node label (us-east-1)
		"arn":           "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks",
		"provider":      "eks", // the Kubernetes service still comes from the nodes
		"uid":           "4e5622e7-f420-499c-9662-50af7f8a64b9",
	}
	for key, value := range want {
		if cluster[key] != value {
			t.Errorf("cluster[%q] = %v, want %v", key, cluster[key], value)
		}
	}
}

func TestEachCloudValueAppearsOnlyWhenGiven(t *testing.T) {
	cases := []struct {
		name      string
		cloud     Cloud
		wantKeys  map[string]any
		wantAbsen []string
	}{
		{"provider", Cloud{Provider: "azure"}, map[string]any{"cloudProvider": "azure", "region": "us-east-1"}, []string{"accountId", "arn"}},
		{"account", Cloud{AccountID: "00000000-0000-0000-0000-000000000000"}, map[string]any{"accountId": "00000000-0000-0000-0000-000000000000", "region": "us-east-1"}, []string{"cloudProvider", "arn"}},
		{"region", Cloud{Region: "westeurope"}, map[string]any{"region": "westeurope"}, []string{"cloudProvider", "accountId", "arn"}},
		{"cluster id", Cloud{ClusterID: "arn:aws:eks:x"}, map[string]any{"arn": "arn:aws:eks:x", "region": "us-east-1"}, []string{"cloudProvider", "accountId"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t)
			f.list(pathNodes, node(map[string]string{"topology.kubernetes.io/region": "us-east-1"}, "v1.31.2"))
			cluster := clusterOnTheWire(t, f.snapshot(Options{Cloud: tc.cloud}))
			for key, value := range tc.wantKeys {
				if cluster[key] != value {
					t.Errorf("cluster[%q] = %v, want %v", key, cluster[key], value)
				}
			}
			for _, key := range tc.wantAbsen {
				if v, ok := cluster[key]; ok {
					t.Errorf("cluster has %q = %v, want it left out", key, v)
				}
			}
		})
	}
}

func TestModeAndClusterOwner(t *testing.T) {
	ours := webhookConfig("impact-cluster-kit-opentelemetry-operator-mutation",
		hook("minstrumentation.kb.io", "impact-cluster-kit-opentelemetry-operator-webhook", "impact-system", "/mutate-opentelemetry-io-v1alpha1-instrumentation"),
		hook("mpod.kb.io", "impact-cluster-kit-opentelemetry-operator-webhook", "impact-system", "/mutate-v1-pod"))
	ours["metadata"].(obj)["annotations"] = obj{"meta.helm.sh/release-name": "impact-cluster-kit", "meta.helm.sh/release-namespace": "impact-system"}
	certManager := webhookConfig("cert-manager-webhook", hook("webhook.cert-manager.io", "cert-manager-webhook", "cert-manager", "/mutate"))
	datadog := webhookConfig("datadog-webhook", hook("datadog.webhook.auto.instrumentation", "agent", "datadog", "/injectlib"))
	release := injector.Release{Name: "impact-cluster-kit", Namespace: "impact-system"}

	cases := []struct {
		name      string
		webhooks  []any
		opts      Options
		wantMode  string
		wantOwner *string
	}{
		{"nothing else injects", []any{certManager}, Options{Release: release}, "inject", nil},
		{"our own operator does not count", []any{ours, certManager}, Options{Release: release}, "inject", nil},
		{"datadog owns the cluster", []any{ours, datadog}, Options{Release: release}, "report-only", ptr("datadog")},
		{"injection switched off by the chart", []any{certManager}, Options{Release: release, InjectionDisabled: true}, "report-only", nil},
		{"switched off and another injector present", []any{datadog}, Options{Release: release, InjectionDisabled: true}, "report-only", ptr("datadog")},
		{"without a release identity our operator looks foreign", []any{ours}, Options{}, "report-only", ptr("opentelemetry-operator")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t)
			f.list(pathWebhooks, tc.webhooks...)
			snap := f.snapshot(tc.opts)
			if snap.Reporter.Mode != tc.wantMode || !reflect.DeepEqual(snap.Reporter.ClusterOwner, tc.wantOwner) {
				t.Errorf("mode %q owner %v, want %q %v", snap.Reporter.Mode, deref(snap.Reporter.ClusterOwner), tc.wantMode, deref(tc.wantOwner))
			}
		})
	}
}

func TestReporterAndEnvelopeFields(t *testing.T) {
	f := newFake(t)
	snap := f.snapshot(Options{Version: "0.1.0", Instance: "5b1d0f4c"})
	if snap.Schema != "impact.inventory.v1" || snap.Sequence != 42 || snap.ObservedAt != "2026-11-03T12:00:00Z" {
		t.Errorf("envelope = %q %d %q", snap.Schema, snap.Sequence, snap.ObservedAt)
	}
	if snap.Reporter.Version != "0.1.0" || snap.Reporter.Instance != "5b1d0f4c" {
		t.Errorf("reporter = %+v", snap.Reporter)
	}
}

func TestSnapshotJSONShape(t *testing.T) {
	f := newFake(t)
	f.list(pathDeployments, deployment("shop", "api", 1, 1, template(nil, ctr("app"))))
	raw := mustJSON(t, f.snapshot(Options{}))
	var top map[string]any
	if err := json.Unmarshal([]byte(raw), &top); err != nil {
		t.Fatal(err)
	}
	wantTop := []string{"schema", "cluster", "reporter", "observedAt", "sequence", "workloads"}
	for _, k := range wantTop {
		if _, ok := top[k]; !ok {
			t.Errorf("missing top-level field %q", k)
		}
	}
	if len(top) != len(wantTop) {
		t.Errorf("unexpected top-level fields: %v", top)
	}
	cluster := top["cluster"].(map[string]any)
	for _, k := range []string{"uid", "name", "provider", "region", "kubernetesVersion"} {
		if _, ok := cluster[k]; !ok {
			t.Errorf("cluster is missing %q", k)
		}
	}
	if cluster["name"] != nil || cluster["region"] != nil {
		t.Errorf("name and region must be null when unknown: %v", cluster)
	}
	reporter := top["reporter"].(map[string]any)
	for _, k := range []string{"version", "instance", "mode", "clusterOwner"} {
		if _, ok := reporter[k]; !ok {
			t.Errorf("reporter is missing %q", k)
		}
	}
	w := top["workloads"].([]any)[0].(map[string]any)
	for _, k := range []string{"namespace", "kind", "name", "pods", "serviceAccount", "annotations", "containers"} {
		if _, ok := w[k]; !ok {
			t.Errorf("workload is missing %q", k)
		}
	}
	if _, ok := w["annotations"].(map[string]any); !ok {
		t.Errorf("annotations must be an object even when empty: %v", w["annotations"])
	}
	c := w["containers"].([]any)[0].(map[string]any)
	for _, k := range []string{"name", "image", "executables", "envNames", "secretRefs", "envFrom", "injected"} {
		if _, ok := c[k]; !ok {
			t.Errorf("container is missing %q", k)
		}
	}
	for _, k := range []string{"executables", "envNames", "secretRefs", "envFrom"} {
		if arr, ok := c[k].([]any); !ok || len(arr) != 0 {
			t.Errorf("%s must be an empty array, got %#v", k, c[k])
		}
	}
	if inj := c["injected"].(map[string]any); inj["python"] != false || len(inj) != 1 {
		t.Errorf("injected = %v", inj)
	}
}

// AE-3: no secret leaves the cluster.
func TestAE3NoSecretLeavesTheCluster(t *testing.T) {
	f := newFake(t)
	lastApplied := `{"apiVersion":"apps/v1","kind":"Deployment","spec":{"template":{"spec":{"containers":[{"name":"app","env":[{"name":"OPENAI_API_KEY","value":"` + fakeKey + `"}]}]}}}}`
	noisy := func(extra ...func(obj)) obj {
		opts := append([]func(obj){
			command("/bin/sh", "-c", "python app.py --api-key="+fakeKey),
			cliArgs("--api-key="+fakeKey, "--token", fakeKey),
			env("OPENAI_API_KEY", fakeKey),
			env("OTEL_SERVICE_NAME", "support-assistant"),
			secretEnv("ANTHROPIC_API_KEY", "anthropic-credentials", "api-key"),
			fieldEnv("POD_IP", "status.podIP"),
			envFromSecret("llm-config"),
			envFromConfigMap("app-config"),
		}, extra...)
		return ctr("app", opts...)
	}
	annotations := map[string]string{
		"kubectl.kubernetes.io/last-applied-configuration": lastApplied,
		"instrumentation.opentelemetry.io/inject-python":   "impact-system/impact-cluster-kit",
		"example.com/api-token":                            fakeKey,
		"vault.hashicorp.com/agent-inject-template-db":     "password " + fakeKey,
	}
	tmpl := func() obj { return template(annotations, noisy()) }

	f.list(pathDeployments, deployment("shop", "support-assistant", 2, 2, tmpl()))
	f.list(pathStatefulSets, deployment("shop", "memory", 1, 1, tmpl()))
	f.list(pathDaemonSets, daemonSet("shop", "agent", 1, 1, tmpl()))
	f.list(pathCronJobs, cronJob("shop", "nightly", false, 0, tmpl()))
	f.list(pathJobs, job("shop", "one-off", nil, nil, tmpl()))
	// command[0] itself is a secret: it must not be sent either.
	f.list(pathPods,
		pod("shop", "debug", tmpl(), nil),
		pod("shop", "odd", template(nil, ctr("app", command("--api-key="+fakeKey, "x"))), nil),
	)

	snap := f.snapshot(Options{})

	body := mustJSON(t, snap)
	enc, err := Encode(snap)
	if err != nil {
		t.Fatal(err)
	}
	zr, err := gzip.NewReader(bytes.NewReader(enc.Body))
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := io.ReadAll(zr)
	if err != nil {
		t.Fatal(err)
	}
	if string(decoded) != body {
		t.Error("the gzip body does not round-trip to the snapshot JSON")
	}

	for _, forbidden := range []string{
		fakeKey, "sk-live", "--api-key", "python app.py", "last-applied", "kubectl.kubernetes.io",
		"example.com/api-token", "agent-inject-template", "password", `"-c"`, "--token",
	} {
		if strings.Contains(body, forbidden) || strings.Contains(string(decoded), forbidden) {
			t.Errorf("the snapshot contains %q", forbidden)
		}
	}
	// Names and base names do travel.
	for _, wanted := range []string{`"OPENAI_API_KEY"`, `"OTEL_SERVICE_NAME"`, `"ANTHROPIC_API_KEY"`, `"sh"`,
		`"anthropic-credentials"`, `"llm-config"`, `"app-config"`, "instrumentation.opentelemetry.io/inject-python"} {
		if !strings.Contains(string(decoded), wanted) {
			t.Errorf("the snapshot lacks %s", wanted)
		}
	}

	api := findWorkload(t, snap, "shop", "Deployment", "support-assistant")
	if got := api.Annotations; len(got) != 1 || got["instrumentation.opentelemetry.io/inject-python"] != "impact-system/impact-cluster-kit" {
		t.Errorf("annotations = %v, want only the allowlisted one", got)
	}
	c := api.Containers[0]
	if !reflect.DeepEqual(c.Executables, []string{"sh"}) {
		t.Errorf("executables = %v, want [sh]", c.Executables)
	}
	if want := []string{"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OTEL_SERVICE_NAME", "POD_IP"}; !reflect.DeepEqual(c.EnvNames, want) {
		t.Errorf("envNames = %v, want %v", c.EnvNames, want)
	}
	if want := []SecretRef{{EnvName: "ANTHROPIC_API_KEY", Secret: "anthropic-credentials", Key: "api-key"}}; !reflect.DeepEqual(c.SecretRefs, want) {
		t.Errorf("secretRefs = %v", c.SecretRefs)
	}
	if want := []EnvFromRef{{Kind: "ConfigMap", Name: "app-config"}, {Kind: "Secret", Name: "llm-config"}}; !reflect.DeepEqual(c.EnvFrom, want) {
		t.Errorf("envFrom = %v", c.EnvFrom)
	}
	odd := findWorkload(t, snap, "shop", "Pod", "odd")
	if len(odd.Containers[0].Executables) != 0 {
		t.Errorf("executables = %v; a command[0] holding a key must be dropped", odd.Containers[0].Executables)
	}
}

func TestOnlyLivePodsAreRequested(t *testing.T) {
	f := newFake(t)
	f.snapshot(Options{})
	reqs := f.srv.RequestsFor(pathPods)
	if len(reqs) == 0 || !strings.Contains(reqs[0].Query, "fieldSelector=status.phase%21%3DSucceeded%2Cstatus.phase%21%3DFailed") {
		t.Errorf("pods request = %+v, want a field selector that leaves out finished pods", reqs)
	}
}

func TestListsAreFollowedAcrossPages(t *testing.T) {
	f := newFake(t)
	f.pageSize = 2
	var deps, pods []any
	for _, n := range []string{"a", "b", "c", "d", "e"} {
		deps = append(deps, deployment("shop", n, 1, 1, template(nil, ctr("app"))))
		pods = append(pods, pod("shop", "bare-"+n, template(nil, ctr("app")), nil))
	}
	f.list(pathDeployments, deps...)
	f.list(pathPods, pods...)
	snap := f.snapshot(Options{})
	if len(snap.Workloads) != 10 {
		t.Errorf("collected %d workloads, want 10: %v", len(snap.Workloads), wl(snap))
	}
}

func TestOutputIsDeterministic(t *testing.T) {
	build := func(reverse bool) string {
		f := newFake(t)
		deps := []any{
			deployment("b", "z", 1, 1, template(nil, ctr("c2"), ctr("c1", env("Z", "1"), env("A", "2")))),
			deployment("a", "y", 1, 1, template(nil, ctr("c"))),
			deployment("a", "x", 1, 1, template(nil, ctr("c"))),
		}
		if reverse {
			deps[0], deps[2] = deps[2], deps[0]
		}
		f.list(pathDeployments, deps...)
		return mustJSON(t, f.snapshot(Options{}))
	}
	if a, b := build(false), build(true); a != b {
		t.Errorf("the output depends on the order the API server answered in:\n%s\n%s", a, b)
	}
	f := newFake(t)
	f.list(pathDeployments,
		deployment("b", "z", 1, 1, template(nil, ctr("c2"), ctr("c1", env("Z", "1"), env("A", "2")))))
	w := f.snapshot(Options{}).Workloads[0]
	if w.Containers[0].Name != "c2" || w.Containers[1].Name != "c1" {
		t.Errorf("containers must keep spec order, got %s, %s", w.Containers[0].Name, w.Containers[1].Name)
	}
	if !reflect.DeepEqual(w.Containers[1].EnvNames, []string{"A", "Z"}) {
		t.Errorf("env names must be sorted, got %v", w.Containers[1].EnvNames)
	}
}

func TestPartialFailures(t *testing.T) {
	t.Run("nodes, webhooks and namespaces are best effort", func(t *testing.T) {
		f := newFake(t)
		f.srv.HandleStatus(pathNodes, http.StatusForbidden, `{}`)
		f.srv.HandleStatus(pathWebhooks, http.StatusForbidden, `{}`)
		f.srv.HandleStatus(pathNamespaces, http.StatusForbidden, `{}`)
		f.list(pathDeployments, deployment("shop", "api", 1, 1, template(nil, ctr("app"))))
		snap := f.snapshot(Options{})
		if snap.Cluster.Provider != "other" || snap.Cluster.Region != nil || snap.Reporter.ClusterOwner != nil || len(snap.Workloads) != 1 {
			t.Errorf("snapshot = %+v", snap)
		}
	})
	for _, path := range []string{pathKubeSystem, pathVersion, pathDeployments, pathStatefulSets, pathDaemonSets, pathCronJobs, pathJobs, pathPods} {
		t.Run("failure of "+path+" fails the snapshot", func(t *testing.T) {
			f := newFake(t)
			f.srv.HandleStatus(path, http.StatusInternalServerError, `{}`)
			if _, err := f.collector(Options{}).Snapshot(t.Context(), 1); err == nil {
				t.Error("a snapshot built from partial data would make workloads look removed; it must fail")
			}
		})
	}
}
