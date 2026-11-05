// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestUninstallNoticeSaysOnlyThatTheReporterIsGone(t *testing.T) {
	f := newFake(t)
	// A cluster full of things that must not appear in the notice.
	app := template(map[string]string{"instrumentation.opentelemetry.io/inject-python": "impact-system/impact-cluster-kit"},
		ctr("app", env("OPENAI_API_KEY", fakeKey), command("/bin/sh", "-c", "python app.py --api-key="+fakeKey), secretEnv("ANTHROPIC_API_KEY", "anthropic-credentials", "api-key")))
	f.list(pathDeployments, deployment("shop", "support-assistant", 2, 2, app))
	f.list(pathPods, pod("shop", "debug", app, nil))
	f.list(pathWebhooks, webhookConfig("datadog-webhook", hook("datadog.webhook.config", "dd", "datadog", "/injectconfig")))
	f.list(pathNodes, node(map[string]string{"eks.amazonaws.com/nodegroup": "ng", "topology.kubernetes.io/region": "us-east-1"}, "v1.31.2-eks-abc"))
	f.srv.Handle(pathVersion, obj{"gitVersion": "v1.31.2-eks-abc"})

	snap, err := f.collector(Options{ClusterName: "prod-eks", Version: "0.1.0", Instance: "hook-pod-uid"}).UninstallNotice(t.Context(), 1793707300)
	if err != nil {
		t.Fatal(err)
	}

	if snap.Schema != SchemaV1 || snap.Sequence != 1793707300 || snap.ObservedAt != "2026-11-03T12:00:00Z" {
		t.Errorf("envelope = %q %d %q", snap.Schema, snap.Sequence, snap.ObservedAt)
	}
	if snap.Reporter.Mode != "uninstalled" || snap.Reporter.ClusterOwner != nil || snap.Reporter.Version != "0.1.0" || snap.Reporter.Instance != "hook-pod-uid" {
		t.Errorf("reporter = %+v, want mode uninstalled, no cluster owner", snap.Reporter)
	}
	c := snap.Cluster
	if c.UID != "4e5622e7-f420-499c-9662-50af7f8a64b9" || c.Provider != "eks" || c.KubernetesVersion != "1.31.2" ||
		c.Name == nil || *c.Name != "prod-eks" || c.Region == nil || *c.Region != "us-east-1" {
		t.Errorf("cluster = %+v", c)
	}
	if snap.Workloads == nil || len(snap.Workloads) != 0 {
		t.Errorf("workloads = %#v, want an empty list", snap.Workloads)
	}

	// The wire form: "workloads": [] and "clusterOwner": null.
	raw := mustJSON(t, snap)
	var wire map[string]any
	if err := json.Unmarshal([]byte(raw), &wire); err != nil {
		t.Fatal(err)
	}
	if w, ok := wire["workloads"].([]any); !ok || len(w) != 0 {
		t.Errorf("workloads on the wire = %#v", wire["workloads"])
	}
	if owner, ok := wire["reporter"].(map[string]any)["clusterOwner"]; !ok || owner != nil {
		t.Errorf("clusterOwner on the wire = %#v, want null", owner)
	}
	for _, leak := range []string{"support-assistant", "shop", "debug", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", fakeKey, "sk-live", "datadog", "inject-python", "anthropic-credentials"} {
		if strings.Contains(raw, leak) {
			t.Errorf("the notice contains %q: %s", leak, raw)
		}
	}

	// Cluster discovery only: no workload, pod or webhook is read for a notice.
	for _, path := range []string{pathDeployments, pathStatefulSets, pathDaemonSets, pathCronJobs, pathJobs, pathPods, pathWebhooks, pathNamespaces} {
		if n := len(f.srv.RequestsFor(path)); n != 0 {
			t.Errorf("%s was requested %d times", path, n)
		}
	}
}

func TestUninstallNoticeCarriesTheSameCloudIdentity(t *testing.T) {
	f := newFake(t)
	cloud := Cloud{Provider: "aws", AccountID: "012345678901", Region: "eu-west-1", ClusterID: "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks"}
	snap, err := f.collector(Options{Cloud: cloud}).UninstallNotice(t.Context(), 1793707300)
	if err != nil {
		t.Fatal(err)
	}
	cluster := clusterOnTheWire(t, snap)
	if cluster["cloudProvider"] != "aws" || cluster["accountId"] != "012345678901" || cluster["region"] != "eu-west-1" ||
		cluster["arn"] != "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks" {
		t.Errorf("cluster in the notice = %v", cluster)
	}
	if _, ok := clusterOnTheWire(t, mustNotice(t, f.collector(Options{})))["arn"]; ok {
		t.Error("a notice without a cloud identity must not carry one")
	}
}

func mustNotice(t *testing.T, c *Collector) *Snapshot {
	t.Helper()
	snap, err := c.UninstallNotice(t.Context(), 1)
	if err != nil {
		t.Fatal(err)
	}
	return snap
}

func TestUninstallNoticeNeedsTheClusterIDAndTheVersion(t *testing.T) {
	for _, path := range []string{pathKubeSystem, pathVersion} {
		f := newFake(t)
		f.srv.HandleStatus(path, http.StatusInternalServerError, `{}`)
		if _, err := f.collector(Options{}).UninstallNotice(t.Context(), 1); err == nil {
			t.Errorf("a notice without %s cannot identify the cluster and must fail", path)
		}
	}
}

func TestUninstallNoticeDoesNotNeedNodes(t *testing.T) {
	f := newFake(t)
	f.srv.HandleStatus(pathNodes, http.StatusForbidden, `{}`)
	snap, err := f.collector(Options{}).UninstallNotice(t.Context(), 1)
	if err != nil {
		t.Fatal(err)
	}
	if snap.Cluster.Provider != "other" || snap.Cluster.Region != nil {
		t.Errorf("cluster = %+v", snap.Cluster)
	}
}

func TestSnapshotsKeepTheirModes(t *testing.T) {
	f := newFake(t)
	if got := f.snapshot(Options{}).Reporter.Mode; got != ModeInject {
		t.Errorf("mode = %q", got)
	}
	if got := f.snapshot(Options{InjectionDisabled: true}).Reporter.Mode; got != ModeReportOnly {
		t.Errorf("mode = %q", got)
	}
	if !reflect.DeepEqual([]string{ModeInject, ModeReportOnly, ModeUninstalled}, []string{"inject", "report-only", "uninstalled"}) {
		t.Error("mode names changed")
	}
}
