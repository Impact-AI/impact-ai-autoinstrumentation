// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

var fixedNow = time.Date(2026, 11, 4, 9, 30, 0, 0, time.UTC)

const (
	instrumentation = "impact-ai/impact"
	marker          = "2026-11-04T09:30:00Z" // fixedNow, as the cover pod writes it
)

func discard() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func entry(kind, name string, opts ...func(*Entry)) Entry {
	e := Entry{Namespace: "shop", Kind: kind, Name: name, Lang: "python"}
	for _, o := range opts {
		o(&e)
	}
	return e
}

func lang(l string) func(*Entry)      { return func(e *Entry) { e.Lang = l } }
func container(c string) func(*Entry) { return func(e *Entry) { e.Container = c } }
func musl() func(*Entry)              { return func(e *Entry) { e.Musl = true } }

func storeOf(entries ...Entry) *Store {
	s := &Store{}
	s.Set(List{Schema: SchemaV1, Version: 1, Automatic: true, Workloads: entries})
	return s
}

func decider(s *Store) *Decider {
	return &Decider{Store: s, Instrumentation: instrumentation, Now: func() time.Time { return fixedNow }, Log: discard()}
}

// --- owner derivation (the cover pod works out the workload of a pod from its owner references alone) ----------------

func TestOwnerDerivation(t *testing.T) {
	hash := map[string]string{"pod-template-hash": "5d9c7b6f8d"}
	controller := func(kind, name string) []OwnerRef { return []OwnerRef{{Kind: kind, Name: name, Controller: true}} }
	cases := []struct {
		name   string
		owners []OwnerRef
		labels map[string]string
		want   []Workload
	}{
		// Deployment: its ReplicaSet is named <deployment>-<pod-template-hash>.
		{"Deployment through its ReplicaSet", controller("ReplicaSet", "api-5d9c7b6f8d"), hash, []Workload{{"Deployment", "api"}}},
		{"a Deployment name with dashes", controller("ReplicaSet", "support-assistant-v2-5d9c7b6f8d"), hash, []Workload{{"Deployment", "support-assistant-v2"}}},
		{"a ReplicaSet without the hash label is not a Deployment's", controller("ReplicaSet", "api-5d9c7b6f8d"), nil, nil},
		{"a ReplicaSet whose name does not end with the hash", controller("ReplicaSet", "api-aaaaaaaaaa"), hash, nil},
		{"a ReplicaSet named by the hash alone has no Deployment", controller("ReplicaSet", "-5d9c7b6f8d"), hash, nil},
		{"a name that only ends with the hash characters", controller("ReplicaSet", "api-x5d9c7b6f8d"), hash, nil},
		// StatefulSet and DaemonSet own their pods directly.
		{"StatefulSet", controller("StatefulSet", "db"), nil, []Workload{{"StatefulSet", "db"}}},
		{"DaemonSet", controller("DaemonSet", "node-agent"), nil, []Workload{{"DaemonSet", "node-agent"}}},
		// Job: the Job itself, and the CronJob that made it when the name is <cronjob>-<8 digits>.
		{"Job", controller("Job", "migrate"), nil, []Workload{{"Job", "migrate"}}},
		{"a Job of a CronJob", controller("Job", "nightly-report-29000123"), nil, []Workload{{"Job", "nightly-report-29000123"}, {"CronJob", "nightly-report"}}},
		{"seven digits are not a CronJob's", controller("Job", "report-2900012"), nil, []Workload{{"Job", "report-2900012"}}},
		{"nine digits are not a CronJob's", controller("Job", "report-290001234"), nil, []Workload{{"Job", "report-290001234"}}},
		{"digits alone have no CronJob", controller("Job", "-29000123"), nil, []Workload{{"Job", "-29000123"}}},
		{"letters in the suffix", controller("Job", "report-2900012a"), nil, []Workload{{"Job", "report-2900012a"}}},
		// Everything else is left alone.
		{"no owner (a bare pod)", nil, nil, nil},
		{"an owner that is not the controller", []OwnerRef{{Kind: "StatefulSet", Name: "db"}}, nil, nil},
		{"a Rollout", controller("Rollout", "api"), hash, nil},
		{"a node (a static pod)", controller("Node", "node-1"), nil, nil},
		{"a custom resource", controller("Workflow", "etl"), nil, nil},
		{"the controller is picked among owners", []OwnerRef{{Kind: "Foo", Name: "x"}, {Kind: "StatefulSet", Name: "db", Controller: true}}, nil, []Workload{{"StatefulSet", "db"}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Candidates(tc.owners, tc.labels)
			if len(got) == 0 && len(tc.want) == 0 {
				return
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("Candidates = %v, want %v", got, tc.want)
			}
		})
	}
}

// --- the decision -------------------------------------------------------------------------------------------------

func podOf(kind, name string) Pod {
	switch kind {
	case "Deployment":
		return Pod{Namespace: "shop", Labels: map[string]string{"pod-template-hash": "5d9c7b6f8d"}, Owners: []OwnerRef{{Kind: "ReplicaSet", Name: name + "-5d9c7b6f8d", Controller: true}}}
	case "CronJob":
		return Pod{Namespace: "shop", Owners: []OwnerRef{{Kind: "Job", Name: name + "-29000123", Controller: true}}}
	default:
		return Pod{Namespace: "shop", Owners: []OwnerRef{{Kind: kind, Name: name, Controller: true}}}
	}
}

func TestEveryKindOfWorkloadInTheListIsCovered(t *testing.T) {
	for _, kind := range []string{"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"} {
		t.Run(kind, func(t *testing.T) {
			d := decider(storeOf(entry(kind, "app", container("main"))))
			got := d.Decide(podOf(kind, "app"))
			if got.Reason != ReasonCovered || len(got.Ops) == 0 {
				t.Fatalf("decision = %+v, want the workload covered", got)
			}
			if got.Workload != (Workload{Kind: kind, Name: "app"}) {
				t.Errorf("workload = %+v, want %s app", got.Workload, kind)
			}
			// The same pod of a workload that is not listed (another name, another namespace) is left alone.
			other := podOf(kind, "other")
			if res := d.Decide(other); res.Reason != ReasonNotListed || len(res.Ops) != 0 {
				t.Errorf("another workload: %+v", res)
			}
			elsewhere := podOf(kind, "app")
			elsewhere.Namespace = "elsewhere"
			if res := d.Decide(elsewhere); res.Reason != ReasonNotListed || len(res.Ops) != 0 {
				t.Errorf("the same name in another namespace: %+v", res)
			}
		})
	}
}

func TestOnlyWhatTheServerListsIsCovered(t *testing.T) {
	// A Job whose name looks like a CronJob's run: the listed Job wins, then the CronJob.
	d := decider(storeOf(entry("CronJob", "nightly"), entry("Job", "nightly-29000123"), entry("Job", "adhoc-29000124")))
	pod := Pod{Namespace: "shop", Owners: []OwnerRef{{Kind: "Job", Name: "nightly-29000123", Controller: true}}}
	if res := d.Decide(pod); res.Workload != (Workload{Kind: "Job", Name: "nightly-29000123"}) {
		t.Errorf("an exact Job entry comes first: %+v", res)
	}
	pod.Owners[0].Name = "nightly-29000999"
	if res := d.Decide(pod); res.Workload != (Workload{Kind: "CronJob", Name: "nightly"}) {
		t.Errorf("a Job named like a CronJob's run is covered as the CronJob: %+v", res)
	}
	pod.Owners[0].Name = "adhoc-29000124"
	if res := d.Decide(pod); res.Workload != (Workload{Kind: "Job", Name: "adhoc-29000124"}) {
		t.Errorf("a listed Job: %+v", res)
	}
	pod.Owners[0].Name = "adhoc-29000125"
	if res := d.Decide(pod); res.Reason != ReasonNotListed {
		t.Errorf("an unlisted Job with a CronJob-like name and no such CronJob is left alone: %+v", res)
	}
}

func TestNothingIsDoneWithoutAListOrWithAutomaticCoverageOff(t *testing.T) {
	pod := podOf("Deployment", "api")
	var empty Store
	if res := decider(&empty).Decide(pod); res.Reason != ReasonNoList || len(res.Ops) != 0 {
		t.Errorf("no list: %+v", res)
	}
	off := &Store{}
	off.Set(List{Schema: SchemaV1, Version: 3, Automatic: false, Workloads: []Entry{entry("Deployment", "api")}})
	if res := decider(off).Decide(pod); res.Reason != ReasonNoList || len(res.Ops) != 0 {
		t.Errorf("automatic coverage off: %+v", res)
	}
	on := storeOf(entry("Deployment", "api"))
	if res := decider(on).Decide(pod); res.Reason != ReasonCovered {
		t.Errorf("on: %+v", res)
	}
	on.Clear()
	if res := decider(on).Decide(pod); res.Reason != ReasonNoList {
		t.Errorf("a cleared list: %+v", res)
	}
	var none *Decider
	if res := none.Decide(pod); len(res.Ops) != 0 {
		t.Errorf("a missing decider must not act: %+v", res)
	}
}

func TestAPodThatAlreadyAsksForInjectionIsLeftAlone(t *testing.T) {
	d := decider(storeOf(entry("Deployment", "api")))
	for _, key := range []string{
		"instrumentation.opentelemetry.io/inject-python",
		"instrumentation.opentelemetry.io/inject-nodejs",
		"instrumentation.opentelemetry.io/inject-java",
		"instrumentation.opentelemetry.io/inject-dotnet",
		"instrumentation.opentelemetry.io/inject-go",
		"instrumentation.opentelemetry.io/inject-sdk",
		"instrumentation.opentelemetry.io/inject-apache-httpd",
	} {
		for _, value := range []string{"true", "false", "other/instrumentation", ""} {
			pod := podOf("Deployment", "api")
			pod.Annotations = map[string]string{key: value, "team": "x"}
			if res := d.Decide(pod); res.Reason != ReasonAnnotated || len(res.Ops) != 0 {
				t.Errorf("%s=%q: %+v", key, value, res)
			}
		}
	}
	// Other annotations of the same domain do not count as a request for injection.
	pod := podOf("Deployment", "api")
	pod.Annotations = map[string]string{"instrumentation.opentelemetry.io/container-names": "main"}
	if res := d.Decide(pod); res.Reason != ReasonCovered {
		t.Errorf("container-names alone: %+v", res)
	}
}

func TestOptOutsAreHonoured(t *testing.T) {
	d := decider(storeOf(entry("Deployment", "api")))
	cases := []struct {
		name        string
		annotations map[string]string
		labels      map[string]string
		want        string
	}{
		{"annotation true", map[string]string{OptOut: "true"}, nil, ReasonOptOut},
		{"annotation with any text", map[string]string{OptOut: "because"}, nil, ReasonOptOut},
		{"annotation empty", map[string]string{OptOut: ""}, nil, ReasonOptOut},
		{"annotation false", map[string]string{OptOut: "false"}, nil, ReasonCovered},
		{"annotation False", map[string]string{OptOut: "False"}, nil, ReasonCovered},
		{"annotation FALSE", map[string]string{OptOut: "FALSE"}, nil, ReasonCovered},
		{"label true", nil, map[string]string{OptOut: "true"}, ReasonOptOut},
		{"label empty", nil, map[string]string{OptOut: ""}, ReasonOptOut},
		{"label false still opts out (the webhook's object selector says the same)", nil, map[string]string{OptOut: "false"}, ReasonOptOut},
		{"another label", nil, map[string]string{"impact.ai/team": "x"}, ReasonCovered},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pod := podOf("Deployment", "api")
			pod.Annotations = tc.annotations
			for k, v := range tc.labels {
				pod.Labels[k] = v
			}
			if res := d.Decide(pod); res.Reason != tc.want {
				t.Errorf("decision = %+v, want %s", res, tc.want)
			}
		})
	}
}

func TestPodsWithoutAWorkloadAreLeftAlone(t *testing.T) {
	d := decider(storeOf(entry("Deployment", "api")))
	for name, pod := range map[string]Pod{
		"bare pod":           {Namespace: "shop"},
		"a Rollout":          {Namespace: "shop", Labels: map[string]string{"pod-template-hash": "5d9c7b6f8d"}, Owners: []OwnerRef{{Kind: "Rollout", Name: "api", Controller: true}}},
		"not the controller": {Namespace: "shop", Owners: []OwnerRef{{Kind: "StatefulSet", Name: "api"}}},
	} {
		if res := d.Decide(pod); res.Reason != ReasonNoWorkload || len(res.Ops) != 0 {
			t.Errorf("%s: %+v", name, res)
		}
	}
}

// --- the patch ----------------------------------------------------------------------------------------------------

func keysTheCoverPodMayWrite() []string {
	return []string{
		"instrumentation.opentelemetry.io/inject-python",
		"instrumentation.opentelemetry.io/inject-nodejs",
		"instrumentation.opentelemetry.io/container-names",
		"instrumentation.opentelemetry.io/otel-python-platform",
		"impact.ai/auto-covered",
	}
}

// markerLabel is the marker as a label, with the value it has: the uninstall script selects the pods it marked by it.
const (
	markerLabel      = "impact.ai/auto-covered"
	markerLabelValue = "true"
)

// allowedPath is written out again here, not taken from the package, so the test checks the package against a list.
func allowedPath(path string) bool {
	if path == "/metadata/annotations" || path == "/metadata/labels" || path == "/metadata/labels/impact.ai~1auto-covered" {
		return true
	}
	for _, key := range keysTheCoverPodMayWrite() {
		if path == "/metadata/annotations/"+strings.ReplaceAll(key, "/", "~1") {
			return true
		}
	}
	return false
}

// applyPatch applies RFC 6902 `add` operations the way an API server does: the parent of the member must exist.
func applyPatch(t *testing.T, doc map[string]any, wire []byte) {
	t.Helper()
	var ops []struct {
		Op    string          `json:"op"`
		Path  string          `json:"path"`
		Value json.RawMessage `json:"value"`
	}
	if err := json.Unmarshal(wire, &ops); err != nil {
		t.Fatalf("the patch is not a JSON Patch: %v\n%s", err, wire)
	}
	for _, op := range ops {
		if op.Op != "add" {
			t.Fatalf("operation %q in %s: only add is allowed", op.Op, wire)
		}
		if !allowedPath(op.Path) {
			t.Fatalf("path %q is not allowed (patch %s)", op.Path, wire)
		}
		segments := strings.Split(op.Path, "/")[1:]
		for i, s := range segments {
			segments[i] = strings.ReplaceAll(strings.ReplaceAll(s, "~1", "/"), "~0", "~")
		}
		current := doc
		for _, s := range segments[:len(segments)-1] {
			next, ok := current[s].(map[string]any)
			if !ok {
				t.Fatalf("add %s does not apply: %q does not exist (patch %s)", op.Path, s, wire)
			}
			current = next
		}
		var value any
		if err := json.Unmarshal(op.Value, &value); err != nil {
			t.Fatalf("value of %s: %v", op.Path, err)
		}
		current[segments[len(segments)-1]] = value
	}
}

// podDocument is a pod as the API server holds it: with the labels given (nil: the pod has no labels object at all) and
// the annotations given (nil: no annotations object).
func podDocument(annotations map[string]string, labels map[string]string) map[string]any {
	meta := map[string]any{"name": "api-1", "namespace": "shop"}
	if labels != nil {
		l := map[string]any{}
		for k, v := range labels {
			l[k] = v
		}
		meta["labels"] = l
	}
	if annotations != nil {
		a := map[string]any{}
		for k, v := range annotations {
			a[k] = v
		}
		meta["annotations"] = a
	}
	return map[string]any{
		"apiVersion": "v1", "kind": "Pod", "metadata": meta,
		"spec": map[string]any{"containers": []any{map[string]any{"name": "main", "image": "x", "env": []any{map[string]any{"name": "OPENAI_API_KEY", "value": "sk-live-abcdefghijklmnop"}}}}},
	}
}

// clone copies a document by way of JSON.
func clone(t *testing.T, doc map[string]any) map[string]any {
	t.Helper()
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestThePatchHoldsOnlyAllowedPathsAndAddsExactlyTheAnnotationsAndTheMarkerLabel(t *testing.T) {
	type pre struct {
		name        string
		annotations map[string]string // nil: the pod has no annotations at all
	}
	existing := []pre{
		{"none", nil},
		{"empty object", map[string]string{}},
		{"other annotations", map[string]string{"team": "payments", "kubectl.kubernetes.io/last-applied-configuration": "{\"x\":1}"}},
		{"container-names set by the customer", map[string]string{"instrumentation.opentelemetry.io/container-names": "sidecar"}},
		{"platform set by the customer", map[string]string{"instrumentation.opentelemetry.io/otel-python-platform": "glibc"}},
		{"marker from an earlier pod template", map[string]string{"impact.ai/auto-covered": "2026-01-01T00:00:00Z"}},
		{"opt-out false", map[string]string{"impact.ai/opt-out": "false"}},
	}
	// The labels a pod arrives with. A pod of a Deployment always has its hash label, so it never has none.
	type lab struct {
		name     string
		labels   func(kind string) map[string]string // nil: the pod has no labels object at all
		deployed bool                                // whether a Deployment's pod can be like this
	}
	hash := func(kind string, l map[string]string) map[string]string {
		if kind == "Deployment" {
			l["pod-template-hash"] = "5d9c7b6f8d"
		}
		return l
	}
	labelled := []lab{
		{"labels of the controller", func(kind string) map[string]string { return hash(kind, map[string]string{"app": "api"}) }, true},
		{"no labels", func(string) map[string]string { return nil }, false},
		{"an empty labels object", func(string) map[string]string { return map[string]string{} }, false},
		{"the marker label from an earlier pod with another value", func(kind string) map[string]string {
			return hash(kind, map[string]string{"app": "api", markerLabel: "false"})
		}, true},
	}
	checked, wantChecked := 0, 0
	for _, kind := range []string{"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"} {
		for _, language := range []string{"python", "node"} {
			for _, withContainer := range []bool{false, true} {
				for _, withMusl := range []bool{false, true} {
					for _, p := range existing {
						for _, l := range labelled {
							if kind == "Deployment" && !l.deployed {
								continue
							}
							wantChecked++
							name := fmt.Sprintf("%s %s container=%v musl=%v existing=%s labels=%s", kind, language, withContainer, withMusl, p.name, l.name)
							t.Run(name, func(t *testing.T) {
								e := entry(kind, "app", lang(language))
								if withContainer {
									e.Container = "main"
								}
								e.Musl = withMusl
								pod := podOf(kind, "app")
								pod.Annotations = p.annotations
								pod.Labels = l.labels(kind)
								res := decider(storeOf(e)).Decide(pod)
								if res.Reason != ReasonCovered {
									t.Fatalf("decision = %+v", res)
								}
								for _, op := range res.Ops {
									if op.Op != "add" || !allowedPath(op.Path) {
										t.Errorf("operation %+v is outside what the cover pod may do", op)
									}
								}
								if len(p.annotations) == 0 && res.Ops[0].Path != "/metadata/annotations" {
									t.Errorf("a pod without annotations needs its annotations object added first: %+v", res.Ops)
								}
								if len(pod.Labels) == 0 && res.Ops[len(res.Ops)-1].Path != "/metadata/labels" {
									t.Errorf("a pod without labels needs its labels object added: %+v", res.Ops)
								}
								wire, err := MarshalPatch(res.Ops)
								if err != nil {
									t.Fatal(err)
								}

								// What the annotations must be afterwards: what the pod had, and the cover pod's own.
								want := map[string]any{}
								for k, v := range p.annotations {
									want[k] = v
								}
								inject := "instrumentation.opentelemetry.io/inject-python"
								if language == "node" {
									inject = "instrumentation.opentelemetry.io/inject-nodejs"
								}
								want[inject] = instrumentation
								if _, had := p.annotations["instrumentation.opentelemetry.io/container-names"]; withContainer && !had {
									want["instrumentation.opentelemetry.io/container-names"] = "main"
								}
								if _, had := p.annotations["instrumentation.opentelemetry.io/otel-python-platform"]; withMusl && language == "python" && !had {
									want["instrumentation.opentelemetry.io/otel-python-platform"] = "musl"
								}
								want["impact.ai/auto-covered"] = marker
								// And the labels: what the pod had, and the marker label, which the uninstall finds the pod by.
								wantLabels := map[string]any{}
								for k, v := range pod.Labels {
									wantLabels[k] = v
								}
								wantLabels[markerLabel] = markerLabelValue

								before := podDocument(p.annotations, pod.Labels)
								after := clone(t, before)
								applyPatch(t, after, wire)

								got, _ := after["metadata"].(map[string]any)["annotations"].(map[string]any)
								if !reflect.DeepEqual(got, want) {
									t.Errorf("annotations after the patch =\n%v\nwant\n%v", got, want)
								}
								gotLabels, _ := after["metadata"].(map[string]any)["labels"].(map[string]any)
								if !reflect.DeepEqual(gotLabels, wantLabels) {
									t.Errorf("labels after the patch =\n%v\nwant\n%v", gotLabels, wantLabels)
								}
								// And nothing else of the pod changed: not its spec, not its env, not its other metadata.
								for _, doc := range []map[string]any{before, after} {
									delete(doc["metadata"].(map[string]any), "annotations")
									delete(doc["metadata"].(map[string]any), "labels")
								}
								if !reflect.DeepEqual(before, after) {
									t.Errorf("the patch changed more than the annotations and the marker label:\n%v\n%v", before, after)
								}
								checked++
							})
						}
					}
				}
			}
		}
	}
	// 2 labelled variants for a Deployment's pods and 4 for each of the other four kinds.
	if want := 2 * 2 * 2 * len(existing) * (2 + 4*4); checked != want || wantChecked != want {
		t.Errorf("checked %d combinations (%d planned), want %d", checked, wantChecked, want)
	}
}

func TestThePatchOnTheWire(t *testing.T) {
	d := decider(storeOf(entry("Deployment", "api", container("main"), musl())))
	pod := podOf("Deployment", "api")
	pod.Annotations = map[string]string{"team": "x"}
	res := d.Decide(pod)
	wire, err := MarshalPatch(res.Ops)
	if err != nil {
		t.Fatal(err)
	}
	want := `[` +
		`{"op":"add","path":"/metadata/annotations/instrumentation.opentelemetry.io~1inject-python","value":"impact-ai/impact"},` +
		`{"op":"add","path":"/metadata/annotations/instrumentation.opentelemetry.io~1container-names","value":"main"},` +
		`{"op":"add","path":"/metadata/annotations/instrumentation.opentelemetry.io~1otel-python-platform","value":"musl"},` +
		`{"op":"add","path":"/metadata/annotations/impact.ai~1auto-covered","value":"2026-11-04T09:30:00Z"},` +
		`{"op":"add","path":"/metadata/labels/impact.ai~1auto-covered","value":"true"}]`
	if string(wire) != want {
		t.Errorf("patch =\n%s\nwant\n%s", wire, want)
	}

	pod.Annotations = nil
	res = d.Decide(pod)
	wire, _ = MarshalPatch(res.Ops)
	want = `[{"op":"add","path":"/metadata/annotations","value":{` +
		`"impact.ai/auto-covered":"2026-11-04T09:30:00Z",` +
		`"instrumentation.opentelemetry.io/container-names":"main",` +
		`"instrumentation.opentelemetry.io/inject-python":"impact-ai/impact",` +
		`"instrumentation.opentelemetry.io/otel-python-platform":"musl"}},` +
		`{"op":"add","path":"/metadata/labels/impact.ai~1auto-covered","value":"true"}]`
	if string(wire) != want {
		t.Errorf("patch for a pod without annotations =\n%s\nwant\n%s", wire, want)
	}

	// A pod with no labels at all (a pod of a StatefulSet or a Job has some, a controller of another kind may not).
	stateful := decider(storeOf(entry("StatefulSet", "db")))
	bare := podOf("StatefulSet", "db")
	bare.Annotations = map[string]string{"team": "x"}
	wire, _ = MarshalPatch(stateful.Decide(bare).Ops)
	want = `[{"op":"add","path":"/metadata/annotations/instrumentation.opentelemetry.io~1inject-python","value":"impact-ai/impact"},` +
		`{"op":"add","path":"/metadata/annotations/impact.ai~1auto-covered","value":"2026-11-04T09:30:00Z"},` +
		`{"op":"add","path":"/metadata/labels","value":{"impact.ai/auto-covered":"true"}}]`
	if string(wire) != want {
		t.Errorf("patch for a pod without labels =\n%s\nwant\n%s", wire, want)
	}
}

func TestAPatchOutsideTheAllowedPathsIsNeverSent(t *testing.T) {
	// The guard that backs the table above: even a bug in the decision cannot make the cover pod change anything else.
	for _, ops := range [][]Op{
		{{Op: "add", Path: "/spec/containers/0/env/-", Value: map[string]string{"name": "X"}}},
		{{Op: "replace", Path: "/metadata/annotations/impact.ai~1auto-covered", Value: "x"}},
		{{Op: "add", Path: "/metadata/annotations/team", Value: "x"}},
		{{Op: "remove", Path: "/metadata/annotations"}},
		{{Op: "add", Path: "/metadata/annotations/instrumentation.opentelemetry.io/inject-python", Value: "x"}},
		// The marker is the one label the cover pod may add, and only as "true".
		{{Op: "add", Path: "/metadata/labels/impact.ai~1opt-out", Value: "x"}},
		{{Op: "add", Path: "/metadata/labels/app", Value: "x"}},
		{{Op: "add", Path: "/metadata/labels/impact.ai~1auto-covered", Value: "false"}},
		{{Op: "add", Path: "/metadata/labels/impact.ai~1auto-covered", Value: marker}},
		{{Op: "add", Path: "/metadata/labels/impact.ai~1auto-covered", Value: 1}},
		{{Op: "replace", Path: "/metadata/labels/impact.ai~1auto-covered", Value: markerLabelValue}},
		{{Op: "remove", Path: "/metadata/labels"}},
		{{Op: "add", Path: "/metadata/labels", Value: map[string]string{"impact.ai/auto-covered": "true", "app": "x"}}},
		{{Op: "add", Path: "/metadata/labels", Value: map[string]string{"app": "x"}}},
		{{Op: "add", Path: "/metadata/labels", Value: map[string]string{"impact.ai/auto-covered": marker}}},
		{{Op: "add", Path: "/metadata/labels", Value: "true"}},
		{{Op: "add", Path: "/metadata/labels/impact.ai/auto-covered", Value: markerLabelValue}},
	} {
		if err := ValidatePatch(ops); err == nil {
			t.Errorf("ValidatePatch(%+v) accepted it", ops)
		}
	}
	ok := []Op{
		{Op: "add", Path: "/metadata/annotations", Value: map[string]string{"impact.ai/auto-covered": marker}},
		{Op: "add", Path: "/metadata/annotations/impact.ai~1auto-covered", Value: marker},
		{Op: "add", Path: "/metadata/labels", Value: map[string]string{"impact.ai/auto-covered": "true"}},
		{Op: "add", Path: "/metadata/labels/impact.ai~1auto-covered", Value: "true"},
	}
	if err := ValidatePatch(ok); err != nil {
		t.Errorf("ValidatePatch(valid) = %v", err)
	}
	// The decision itself never produces an op the guard would refuse.
	d := decider(storeOf(entry("Deployment", "api", container("main"), musl())))
	for _, annotations := range []map[string]string{nil, {}, {"a": "b"}} {
		pod := podOf("Deployment", "api")
		pod.Annotations = annotations
		if err := ValidatePatch(d.Decide(pod).Ops); err != nil {
			t.Errorf("%v", err)
		}
	}
	stateful := decider(storeOf(entry("StatefulSet", "db")))
	for _, labels := range []map[string]string{nil, {}, {"app": "db"}} {
		pod := podOf("StatefulSet", "db")
		pod.Labels = labels
		if err := ValidatePatch(stateful.Decide(pod).Ops); err != nil {
			t.Errorf("%v", err)
		}
	}
}

func TestEscapingOfPatchPaths(t *testing.T) {
	for in, want := range map[string]string{"a/b": "a~1b", "a~b": "a~0b", "a~/b": "a~0~1b", "plain": "plain"} {
		if got := escapePointer(in); got != want {
			t.Errorf("escapePointer(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTheDecisionNeverLogsAnnotationValues(t *testing.T) {
	var logs bytes.Buffer
	d := decider(storeOf(entry("Deployment", "api")))
	d.Log = slog.New(slog.NewJSONHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	pod := podOf("Deployment", "api")
	pod.Annotations = map[string]string{"kubectl.kubernetes.io/last-applied-configuration": "sk-live-abcdefghijklmnop", OptOut: "private-reason"}
	d.Decide(pod)
	covered := podOf("Deployment", "api")
	covered.Annotations = map[string]string{"team": "sk-live-abcdefghijklmnop"}
	d.Decide(covered)
	for _, leak := range []string{"sk-live", "private-reason", "last-applied"} {
		if strings.Contains(logs.String(), leak) {
			t.Errorf("the log contains %q: %s", leak, logs.String())
		}
	}
	if !strings.Contains(logs.String(), "shop/Deployment/api") {
		t.Errorf("the covered workload is not named in the log: %s", logs.String())
	}
}

func TestReasonsAreStable(t *testing.T) {
	// The reasons are logged and shown in tests; keep them readable and distinct.
	reasons := []string{ReasonCovered, ReasonNoList, ReasonAnnotated, ReasonOptOut, ReasonNotListed, ReasonNoWorkload}
	sort.Strings(reasons)
	for i := 1; i < len(reasons); i++ {
		if reasons[i] == reasons[i-1] || reasons[i] == "" {
			t.Errorf("reasons are not distinct: %v", reasons)
		}
	}
}
