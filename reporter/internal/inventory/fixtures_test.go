// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"testing"
	"time"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kube"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubetest"
)

// fakeKey is a literal that looks like a provider key. It must never appear in
// a snapshot.
const fakeKey = "sk-live-abcdefghijklmnop"

type obj = map[string]any

var fixedNow = time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)

const (
	pathNamespaces   = "/api/v1/namespaces"
	pathKubeSystem   = "/api/v1/namespaces/kube-system"
	pathNodes        = "/api/v1/nodes"
	pathPods         = "/api/v1/pods"
	pathDeployments  = "/apis/apps/v1/deployments"
	pathStatefulSets = "/apis/apps/v1/statefulsets"
	pathDaemonSets   = "/apis/apps/v1/daemonsets"
	pathCronJobs     = "/apis/batch/v1/cronjobs"
	pathJobs         = "/apis/batch/v1/jobs"
	pathWebhooks     = "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations"
	pathVersion      = "/version"
)

// fake is a fake cluster with sensible empty defaults.
type fake struct {
	t        *testing.T
	srv      *kubetest.Server
	pageSize int
}

func newFake(t *testing.T) *fake {
	t.Helper()
	f := &fake{t: t, srv: kubetest.New(t)}
	f.srv.Handle(pathVersion, obj{"major": "1", "minor": "31", "gitVersion": "v1.31.2"})
	f.srv.Handle(pathKubeSystem, obj{"metadata": obj{"name": "kube-system", "uid": "4e5622e7-f420-499c-9662-50af7f8a64b9"}})
	f.list(pathNamespaces, namespace("kube-system"), namespace("default"), namespace("shop"))
	for _, p := range []string{pathNodes, pathPods, pathDeployments, pathStatefulSets, pathDaemonSets, pathCronJobs, pathJobs, pathWebhooks} {
		f.list(p)
	}
	return f
}

func (f *fake) list(path string, items ...any) {
	f.srv.HandleList(path, items, f.pageSize)
}

func (f *fake) collector(opts Options) *Collector {
	f.t.Helper()
	c, err := kube.New(kube.Config{Host: f.srv.URL})
	if err != nil {
		f.t.Fatal(err)
	}
	if opts.Now == nil {
		opts.Now = func() time.Time { return fixedNow }
	}
	if opts.Log == nil {
		opts.Log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if opts.Version == "" {
		opts.Version = "9.9.9"
	}
	if opts.Instance == "" {
		opts.Instance = "pod-uid-1"
	}
	return NewCollector(c, opts)
}

func (f *fake) snapshot(opts Options) *Snapshot {
	f.t.Helper()
	snap, err := f.collector(opts).Snapshot(context.Background(), 42)
	if err != nil {
		f.t.Fatal(err)
	}
	return snap
}

func namespace(name string) obj {
	return obj{"metadata": obj{"name": name}, "status": obj{"phase": "Active"}}
}

func terminatingNamespace(name string) obj {
	return obj{"metadata": obj{"name": name}, "status": obj{"phase": "Terminating"}}
}

func controller(kind, name string) obj {
	return obj{"kind": kind, "name": name, "controller": true, "apiVersion": "x/v1", "uid": "u"}
}

func meta(ns, name string, owners ...obj) obj {
	m := obj{"name": name, "namespace": ns, "uid": ns + "-" + name, "labels": obj{"app": name}, "annotations": obj{"note": "not read"}}
	if len(owners) > 0 {
		refs := make([]any, len(owners))
		for i, o := range owners {
			refs[i] = o
		}
		m["ownerReferences"] = refs
	}
	return m
}

// ctr builds a container. The options add the sensitive parts.
func ctr(name string, opts ...func(obj)) obj {
	c := obj{"name": name, "image": "registry.example.com/shop/" + name + ":1.0", "imagePullPolicy": "IfNotPresent"}
	for _, o := range opts {
		o(c)
	}
	return c
}

func appendTo(c obj, key string, v any) {
	list, _ := c[key].([]any)
	c[key] = append(list, v)
}

func command(args ...string) func(obj) {
	return func(c obj) {
		list := make([]any, len(args))
		for i, a := range args {
			list[i] = a
		}
		c["command"] = list
	}
}

func cliArgs(args ...string) func(obj) {
	return func(c obj) {
		list := make([]any, len(args))
		for i, a := range args {
			list[i] = a
		}
		c["args"] = list
	}
}

func env(name, value string) func(obj) {
	return func(c obj) { appendTo(c, "env", obj{"name": name, "value": value}) }
}

func secretEnv(name, secret, key string) func(obj) {
	return func(c obj) {
		appendTo(c, "env", obj{"name": name, "valueFrom": obj{"secretKeyRef": obj{"name": secret, "key": key}}})
	}
}

func fieldEnv(name, field string) func(obj) {
	return func(c obj) {
		appendTo(c, "env", obj{"name": name, "valueFrom": obj{"fieldRef": obj{"fieldPath": field}}})
	}
}

func envFromSecret(name string) func(obj) {
	return func(c obj) { appendTo(c, "envFrom", obj{"secretRef": obj{"name": name}, "prefix": "P_"}) }
}

func envFromConfigMap(name string) func(obj) {
	return func(c obj) { appendTo(c, "envFrom", obj{"configMapRef": obj{"name": name}}) }
}

func mount(path string) func(obj) {
	return func(c obj) { appendTo(c, "volumeMounts", obj{"name": "v", "mountPath": path}) }
}

func template(annotations map[string]string, containers ...obj) obj {
	cs := make([]any, len(containers))
	for i, c := range containers {
		cs[i] = c
	}
	m := obj{}
	if annotations != nil {
		a := obj{}
		for k, v := range annotations {
			a[k] = v
		}
		m["annotations"] = a
	}
	return obj{"metadata": m, "spec": obj{"serviceAccountName": "sa-1", "containers": cs}}
}

func replicated(ns, name string, replicas *int, ready int, tmpl obj) obj {
	spec := obj{"template": tmpl, "selector": obj{"matchLabels": obj{"app": name}}}
	if replicas != nil {
		spec["replicas"] = *replicas
	}
	return obj{"metadata": meta(ns, name), "spec": spec, "status": obj{"readyReplicas": ready}}
}

func ptr[T any](v T) *T { return &v }

func deployment(ns, name string, replicas, ready int, tmpl obj) obj {
	return replicated(ns, name, ptr(replicas), ready, tmpl)
}

func daemonSet(ns, name string, desired, ready int, tmpl obj) obj {
	return obj{"metadata": meta(ns, name), "spec": obj{"template": tmpl},
		"status": obj{"desiredNumberScheduled": desired, "numberReady": ready}}
}

func cronJob(ns, name string, suspend bool, active int, tmpl obj) obj {
	refs := make([]any, active)
	for i := range refs {
		refs[i] = obj{"kind": "Job", "name": name + "-job"}
	}
	return obj{"metadata": meta(ns, name),
		"spec":   obj{"suspend": suspend, "schedule": "0 2 * * *", "jobTemplate": obj{"spec": obj{"template": tmpl}}},
		"status": obj{"active": refs}}
}

func job(ns, name string, parallelism *int, ready *int, tmpl obj, owners ...obj) obj {
	spec := obj{"template": tmpl}
	if parallelism != nil {
		spec["parallelism"] = *parallelism
	}
	status := obj{"active": 1}
	if ready != nil {
		status["ready"] = *ready
	}
	return obj{"metadata": meta(ns, name, owners...), "spec": spec, "status": status}
}

func finishedJob(ns, name string, finishedAt time.Time, tmpl obj) obj {
	j := job(ns, name, nil, nil, tmpl)
	j["status"] = obj{
		"completionTime": finishedAt.UTC().Format(time.RFC3339),
		"conditions": []any{obj{"type": "Complete", "status": "True",
			"lastTransitionTime": finishedAt.UTC().Format(time.RFC3339)}},
	}
	return j
}

func failedJob(ns, name string, failedAt time.Time, tmpl obj) obj {
	j := job(ns, name, nil, nil, tmpl)
	j["status"] = obj{"conditions": []any{obj{"type": "Failed", "status": "True",
		"lastTransitionTime": failedAt.UTC().Format(time.RFC3339)}}}
	return j
}

type podOpt func(obj)

func withPhase(phase string) podOpt {
	return func(p obj) { p["status"].(obj)["phase"] = phase }
}

func ready(ok bool) podOpt {
	return func(p obj) {
		status := "False"
		if ok {
			status = "True"
		}
		p["status"].(obj)["conditions"] = []any{obj{"type": "Ready", "status": status}}
	}
}

func withInit(name string) podOpt {
	return func(p obj) {
		spec := p["spec"].(obj)
		list, _ := spec["initContainers"].([]any)
		spec["initContainers"] = append(list, obj{"name": name, "image": "x", "command": []any{"cp", "-r", "/a", "/b"}})
	}
}

func withHash(hash string) podOpt {
	return func(p obj) { p["metadata"].(obj)["labels"].(obj)["pod-template-hash"] = hash }
}

func pod(ns, name string, tmpl obj, owner *obj, opts ...podOpt) obj {
	m := meta(ns, name)
	if owner != nil {
		m["ownerReferences"] = []any{*owner}
	}
	if a, ok := tmpl["metadata"].(obj)["annotations"]; ok {
		m["annotations"] = a
	}
	p := obj{"metadata": m, "spec": tmpl["spec"], "status": obj{"phase": "Running"}}
	for _, o := range opts {
		o(p)
	}
	return p
}

// injectedPod is a live pod the Operator has injected: the init container and a
// mount of the instrumentation volume in the named containers.
func injectedPod(ns, name string, owner *obj, injectedContainers []string, all []string, opts ...podOpt) obj {
	cs := make([]obj, len(all))
	for i, n := range all {
		if contains(injectedContainers, n) {
			cs[i] = ctr(n, mount("/otel-auto-instrumentation-python"), mount("/tmp"))
		} else {
			cs[i] = ctr(n, mount("/tmp"))
		}
	}
	return pod(ns, name, template(nil, cs...), owner, append([]podOpt{withInit("opentelemetry-auto-instrumentation-python")}, opts...)...)
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func node(labels map[string]string, kubelet string) obj {
	l := obj{"kubernetes.io/hostname": "node-1"}
	for k, v := range labels {
		l[k] = v
	}
	return obj{"metadata": obj{"name": "node-1", "labels": l}, "status": obj{"nodeInfo": obj{"kubeletVersion": kubelet}}}
}

func webhookConfig(name string, hooks ...obj) obj {
	hs := make([]any, len(hooks))
	for i, h := range hooks {
		hs[i] = h
	}
	return obj{"metadata": obj{"name": name, "annotations": obj{}}, "webhooks": hs}
}

func hook(name, svc, ns, path string) obj {
	return obj{"name": name, "clientConfig": obj{"caBundle": "TOP-SECRET-CA", "service": obj{"name": svc, "namespace": ns, "path": path, "port": 443}}}
}

func findWorkload(t *testing.T, s *Snapshot, ns, kind, name string) *Workload {
	t.Helper()
	for i := range s.Workloads {
		w := &s.Workloads[i]
		if w.Namespace == ns && w.Kind == kind && w.Name == name {
			return w
		}
	}
	t.Fatalf("workload %s/%s/%s not in snapshot; have %v", ns, kind, name, keys(s))
	return nil
}

func keys(s *Snapshot) []string {
	var out []string
	for _, w := range s.Workloads {
		out = append(out, w.Namespace+"/"+w.Kind+"/"+w.Name)
	}
	return out
}

func mustJSON(t testing.TB, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

var _ = http.StatusOK
