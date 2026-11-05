// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// The snapshot fields of automatic coverage: the time a pod was covered (autoCovered) and the impact.ai/* labels of
// the workload and of its namespace. They are only sent when Options.CoverFields is set.

const (
	markerKey  = "impact.ai/auto-covered"
	optOutKey  = "impact.ai/opt-out"
	secretText = "sk-live-abcdefghijklmnop"
)

// labelled returns a workload object with the given labels added to its metadata.
func labelled(o obj, labels map[string]string) obj {
	m := o["metadata"].(obj)
	l, _ := m["labels"].(obj)
	if l == nil {
		l = obj{}
		m["labels"] = l
	}
	for k, v := range labels {
		l[k] = v
	}
	return o
}

// labelledTemplate adds labels to the pod template of a workload built by template().
func labelledTemplate(tmpl obj, labels map[string]string) obj {
	m := tmpl["metadata"].(obj)
	l := obj{}
	for k, v := range labels {
		l[k] = v
	}
	m["labels"] = l
	return tmpl
}

func labelledNamespace(name string, labels map[string]string) obj {
	ns := namespace(name)
	l := obj{}
	for k, v := range labels {
		l[k] = v
	}
	ns["metadata"].(obj)["labels"] = l
	return ns
}

// covered marks a pod the way the cover pod does.
func covered(at string) podOpt {
	return func(p obj) {
		annotations, _ := p["metadata"].(obj)["annotations"].(obj)
		if annotations == nil {
			annotations = obj{}
			p["metadata"].(obj)["annotations"] = annotations
		}
		annotations[markerKey] = at
	}
}

func replicaSetPod(ns, deployment, hash, name string, tmpl obj, opts ...podOpt) obj {
	return pod(ns, name, tmpl, ptr(controller("ReplicaSet", deployment+"-"+hash)), append([]podOpt{withHash(hash)}, opts...)...)
}

func encoded(t *testing.T, s *Snapshot) string {
	t.Helper()
	return mustJSON(t, s)
}

// coverCluster fills the fake with workloads in three namespaces, with or without the impact.ai labels and markers the
// cover fields read. Everything is built before it is listed: the fake serves what it was given at that moment.
func coverCluster(t *testing.T, withExtras bool) *fake {
	t.Helper()
	f := newFake(t)
	app := func() obj { return template(nil, ctr("app", env("OPENAI_API_KEY", secretText))) }
	api := deployment("shop", "api", 2, 2, app())
	db := deployment("shop", "db", 1, 1, app())
	quiet := deployment("edge", "quiet", 1, 1, app())
	shop := namespace("shop")
	var first, second []podOpt
	if withExtras {
		labelled(api, map[string]string{optOutKey: "true", "team": "payments", "token": secretText})
		labelledTemplate(api["spec"].(obj)["template"].(obj), map[string]string{"impact.ai/team": "ai"})
		shop = labelledNamespace("shop", map[string]string{optOutKey: "false", "owner": "someone-private", "kubernetes.io/metadata.name": "shop"})
		first, second = []podOpt{covered("2026-11-03T12:30:00Z")}, []podOpt{covered("2026-11-03T11:45:00Z")}
	}
	f.list(pathDeployments, api, db, quiet)
	f.list(pathNamespaces, namespace("kube-system"), shop, namespace("edge"))
	f.list(pathPods,
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-1", app(), first...),
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-2", app(), second...),
		replicaSetPod("shop", "db", "7c", "db-1", app()),
	)
	return f
}

func TestCoverFieldsAreAbsentUnlessAsked(t *testing.T) {
	plain := encoded(t, coverCluster(t, false).snapshot(Options{}))
	for _, opts := range []Options{{}, {CoverFields: false}} {
		got := encoded(t, coverCluster(t, true).snapshot(opts))
		if got != plain {
			t.Errorf("with the labels and markers present but CoverFields off, the snapshot differs from the one without them:\n%s\n%s", got, plain)
		}
		for _, key := range []string{`"autoCovered"`, `"labels"`, `"namespaceLabels"`} {
			if strings.Contains(got, key) {
				t.Errorf("CoverFields off but the snapshot has %s", key)
			}
		}
	}
}

func TestCoverFieldsOnAddsTheMarkerAndTheLabels(t *testing.T) {
	snap := coverCluster(t, true).snapshot(Options{CoverFields: true})

	api := findWorkload(t, snap, "shop", "Deployment", "api")
	if api.AutoCovered != "2026-11-03T11:45:00Z" {
		t.Errorf("api autoCovered = %q, want the earliest marker of its pods", api.AutoCovered)
	}
	wantLabels := map[string]string{optOutKey: "true", "impact.ai/team": "ai"}
	if !reflect.DeepEqual(api.Labels, wantLabels) {
		t.Errorf("api labels = %v, want %v (the workload's and the pod template's impact.ai labels)", api.Labels, wantLabels)
	}
	if want := map[string]string{optOutKey: "false"}; !reflect.DeepEqual(api.NamespaceLabels, want) {
		t.Errorf("api namespaceLabels = %v, want %v", api.NamespaceLabels, want)
	}

	db := findWorkload(t, snap, "shop", "Deployment", "db")
	if db.AutoCovered != "" || db.Labels != nil {
		t.Errorf("db has no marker and no impact.ai label of its own: %+v", db)
	}
	if want := map[string]string{optOutKey: "false"}; !reflect.DeepEqual(db.NamespaceLabels, want) {
		t.Errorf("db namespaceLabels = %v: every workload of the namespace carries them", db.NamespaceLabels)
	}

	quiet := findWorkload(t, snap, "edge", "Deployment", "quiet")
	if quiet.AutoCovered != "" || quiet.Labels != nil || quiet.NamespaceLabels != nil {
		t.Errorf("quiet has nothing to report: %+v", quiet)
	}

	// On the wire the three fields are left out when empty, and named exactly so.
	var wire struct {
		Workloads []map[string]any `json:"workloads"`
	}
	if err := json.Unmarshal([]byte(encoded(t, snap)), &wire); err != nil {
		t.Fatal(err)
	}
	for _, w := range wire.Workloads {
		if w["name"] == "quiet" {
			for _, key := range []string{"autoCovered", "labels", "namespaceLabels"} {
				if _, ok := w[key]; ok {
					t.Errorf("quiet has the empty field %q on the wire", key)
				}
			}
		}
		if w["name"] == "api" {
			for _, key := range []string{"autoCovered", "labels", "namespaceLabels"} {
				if _, ok := w[key]; !ok {
					t.Errorf("api lacks %q on the wire: %v", key, w)
				}
			}
		}
	}
}

func TestOnlyImpactLabelsAndNothingElseLeavesWithTheCoverFields(t *testing.T) {
	text := encoded(t, coverCluster(t, true).snapshot(Options{CoverFields: true}))
	for _, leak := range []string{secretText, "payments", "someone-private", "kubernetes.io/metadata.name", `"team"`, `"token"`, `"owner"`, "not read"} {
		if strings.Contains(text, leak) {
			t.Errorf("the snapshot contains %q: %s", leak, text)
		}
	}
}

func TestAutoCoveredIsTheEarliestValidMarkerOfTheLivePods(t *testing.T) {
	f := newFake(t)
	app := func() obj { return template(nil, ctr("app")) }
	f.list(pathDeployments, deployment("shop", "api", 4, 4, app()), deployment("shop", "late", 1, 1, app()), deployment("shop", "broken", 1, 1, app()))
	f.list(pathPods,
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-1", app(), covered("2026-11-03T12:30:00Z")),
		// The same instant in another zone is earlier than 12:30Z, and is written as UTC.
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-2", app(), covered("2026-11-03T13:15:00+02:00")),
		// A pod that has finished does not count, however early its marker is.
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-3", app(), covered("2026-11-01T00:00:00Z"), withPhase("Succeeded")),
		// Not a time: ignored.
		replicaSetPod("shop", "api", "5d9c7b6f8d", "api-4", app(), covered("yesterday-ish")),
		replicaSetPod("shop", "late", "9f", "late-1", app(), covered("2026-11-03T11:59:59.5Z")),
		replicaSetPod("shop", "broken", "1a", "broken-1", app(), covered("not a time")),
	)
	snap := f.snapshot(Options{CoverFields: true})
	cases := map[string]string{
		"api":    "2026-11-03T11:15:00Z",
		"late":   "2026-11-03T11:59:59Z",
		"broken": "",
	}
	for name, want := range cases {
		if got := findWorkload(t, snap, "shop", "Deployment", name).AutoCovered; got != want {
			t.Errorf("%s autoCovered = %q, want %q", name, got, want)
		}
	}
}

func TestMarkersOfOtherOwnersOfPodsAreFound(t *testing.T) {
	f := newFake(t)
	app := func() obj { return template(nil, ctr("app")) }
	f.list(pathStatefulSets, deployment("shop", "db", 1, 1, app()))
	f.list(pathDaemonSets, daemonSet("shop", "agent", 1, 1, app()))
	f.list(pathCronJobs, cronJob("shop", "nightly", false, 0, app()))
	f.list(pathJobs,
		job("shop", "nightly-29000123", nil, nil, app(), controller("CronJob", "nightly")),
		job("shop", "migrate", nil, nil, app()),
	)
	f.list(pathPods,
		pod("shop", "db-0", app(), ptr(controller("StatefulSet", "db")), covered("2026-11-03T10:00:00Z")),
		pod("shop", "agent-x", app(), ptr(controller("DaemonSet", "agent")), covered("2026-11-03T10:01:00Z")),
		pod("shop", "nightly-29000123-abc", app(), ptr(controller("Job", "nightly-29000123")), covered("2026-11-03T10:02:00Z")),
		pod("shop", "migrate-xyz", app(), ptr(controller("Job", "migrate")), covered("2026-11-03T10:03:00Z")),
		pod("shop", "standalone", app(), nil, covered("2026-11-03T10:04:00Z")),
	)
	snap := f.snapshot(Options{CoverFields: true})
	want := map[[2]string]string{
		{"StatefulSet", "db"}:  "2026-11-03T10:00:00Z",
		{"DaemonSet", "agent"}: "2026-11-03T10:01:00Z",
		{"CronJob", "nightly"}: "2026-11-03T10:02:00Z",
		{"Job", "migrate"}:     "2026-11-03T10:03:00Z",
		{"Pod", "standalone"}:  "2026-11-03T10:04:00Z",
	}
	for key, at := range want {
		if got := findWorkload(t, snap, "shop", key[0], key[1]).AutoCovered; got != at {
			t.Errorf("%s/%s autoCovered = %q, want %q", key[0], key[1], got, at)
		}
	}
}

func TestTheTemplateLabelWinsOverTheWorkloadsOwn(t *testing.T) {
	f := newFake(t)
	api := deployment("shop", "api", 1, 1, labelledTemplate(template(nil, ctr("app")), map[string]string{optOutKey: "false", "impact.ai/from-template": "t"}))
	labelled(api, map[string]string{optOutKey: "true", "impact.ai/from-object": "o"})
	f.list(pathDeployments, api)
	snap := f.snapshot(Options{CoverFields: true})
	want := map[string]string{optOutKey: "false", "impact.ai/from-template": "t", "impact.ai/from-object": "o"}
	if got := findWorkload(t, snap, "shop", "Deployment", "api").Labels; !reflect.DeepEqual(got, want) {
		t.Errorf("labels = %v, want %v", got, want)
	}
}

func TestCronJobLabelsComeFromTheCronJobAndItsPodTemplate(t *testing.T) {
	f := newFake(t)
	cj := cronJob("shop", "nightly", false, 0, labelledTemplate(template(nil, ctr("app")), map[string]string{"impact.ai/tier": "batch"}))
	labelled(cj, map[string]string{optOutKey: "true"})
	f.list(pathCronJobs, cj)
	snap := f.snapshot(Options{CoverFields: true})
	want := map[string]string{optOutKey: "true", "impact.ai/tier": "batch"}
	if got := findWorkload(t, snap, "shop", "CronJob", "nightly").Labels; !reflect.DeepEqual(got, want) {
		t.Errorf("labels = %v, want %v", got, want)
	}
}

func TestNamespaceLabelsAreBestEffort(t *testing.T) {
	f := coverCluster(t, true)
	f.srv.HandleStatus(pathNamespaces, 500, `{"kind":"Status","code":500}`)
	snap := f.snapshot(Options{CoverFields: true})
	api := findWorkload(t, snap, "shop", "Deployment", "api")
	if api.NamespaceLabels != nil {
		t.Errorf("namespaceLabels = %v, want none when the namespaces cannot be listed", api.NamespaceLabels)
	}
	if api.AutoCovered == "" || api.Labels == nil {
		t.Errorf("the rest of the snapshot must not suffer: %+v", api)
	}
}

func TestNamespaceLabelsNeedNoExtraRequest(t *testing.T) {
	f := coverCluster(t, true)
	f.snapshot(Options{CoverFields: true})
	withFields := 0
	for _, r := range f.srv.Requests() {
		if r.Path == pathNamespaces {
			withFields++
		}
	}
	g := coverCluster(t, true)
	g.snapshot(Options{})
	without := 0
	for _, r := range g.srv.Requests() {
		if r.Path == pathNamespaces {
			without++
		}
	}
	if withFields != without {
		t.Errorf("the namespaces were listed %d times with the cover fields and %d without", withFields, without)
	}
}

func TestTheUninstallNoticeNeverCarriesTheCoverFields(t *testing.T) {
	notice, err := coverCluster(t, true).collector(Options{CoverFields: true}).UninstallNotice(t.Context(), 1)
	if err != nil {
		t.Fatal(err)
	}
	text := mustJSON(t, notice)
	for _, key := range []string{`"autoCovered"`, `"labels"`, `"namespaceLabels"`} {
		if strings.Contains(text, key) {
			t.Errorf("the notice has %s", key)
		}
	}
}

func TestTheMergedLabelsOfAWorkloadAndItsTemplateStayWithinWhatImpactAiReads(t *testing.T) {
	// 12 labels on the object and 12 on its template, all different: 24 together, and the snapshot would be refused.
	own, tmpl := map[string]string{}, map[string]string{}
	for i := 0; i < 12; i++ {
		own[fmt.Sprintf("impact.ai/own%02d", i)] = "o"
		tmpl[fmt.Sprintf("impact.ai/tmpl%02d", i)] = "t"
	}
	f := newFake(t)
	api := deployment("shop", "api", 1, 1, labelledTemplate(template(nil, ctr("app")), tmpl))
	labelled(api, own)
	f.list(pathDeployments, api)
	got := findWorkload(t, f.snapshot(Options{CoverFields: true}), "shop", "Deployment", "api").Labels
	if len(got) != 16 {
		t.Fatalf("%d labels, want the 16 impact.ai reads: %v", len(got), got)
	}
	keys := make([]string, 0, len(got))
	for key := range got {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	if keys[0] != "impact.ai/own00" || keys[15] != "impact.ai/tmpl03" {
		t.Errorf("the first 16 by key are kept: %v", keys)
	}
}
