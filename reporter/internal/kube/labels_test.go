// SPDX-License-Identifier: Apache-2.0

package kube

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func TestImpactLabelsKeepOnlyTheImpactDomain(t *testing.T) {
	in := `{
		"app": "support",
		"team": "payments",
		"secret-ish": "sk-live-abcdefghijklmnop",
		"impact.ai/opt-out": "true",
		"impact.ai/team": "ai",
		"sub.impact.ai/opt-out": "true",
		"notimpact.ai/opt-out": "true",
		"impact.ai": "bare",
		"impact.ai/": "empty name",
		"kubernetes.io/metadata.name": "shop"
	}`
	var got ImpactLabels
	if err := json.Unmarshal([]byte(in), &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"impact.ai/opt-out": "true", "impact.ai/team": "ai"}
	if !reflect.DeepEqual(got.Map(), want) {
		t.Fatalf("labels = %v, want %v", got.Map(), want)
	}
	blob, _ := json.Marshal(got)
	for _, leak := range []string{"sk-live", "secret-ish", "payments", "support", "metadata.name", "sub.impact.ai"} {
		if strings.Contains(string(blob), leak) {
			t.Errorf("decoded labels contain %q: %s", leak, blob)
		}
	}
}

func TestImpactLabelsAreSortedByKey(t *testing.T) {
	var got ImpactLabels
	if err := json.Unmarshal([]byte(`{"impact.ai/z":"1","impact.ai/a":"2","impact.ai/m":"3"}`), &got); err != nil {
		t.Fatal(err)
	}
	var keys []string
	for _, l := range got {
		keys = append(keys, l.Key)
	}
	if !reflect.DeepEqual(keys, []string{"impact.ai/a", "impact.ai/m", "impact.ai/z"}) {
		t.Errorf("keys = %v, want them sorted", keys)
	}
}

func TestImpactLabelsDropLongValuesAndNonStrings(t *testing.T) {
	// A label value longer than 63 characters is no Kubernetes label (and impact.ai refuses a snapshot that holds one),
	// so it is left out rather than cut.
	long := strings.Repeat("v", 600)
	in := `{"impact.ai/long": "` + long + `", "impact.ai/number": 5, "impact.ai/object": {"a": "b"}, "impact.ai/list": ["x"], "impact.ai/ok": "yes"}`
	var got ImpactLabels
	if err := json.Unmarshal([]byte(in), &got); err != nil {
		t.Fatalf("an odd value must not fail the whole page: %v", err)
	}
	m := got.Map()
	if !reflect.DeepEqual(m, map[string]string{"impact.ai/ok": "yes"}) {
		t.Errorf("labels = %v, want the one that is a label", m)
	}
}

func TestImpactLabelsToleratesOddInput(t *testing.T) {
	for _, in := range []string{`null`, `{}`, `[]`, `"text"`, `5`, `true`} {
		var got ImpactLabels
		if err := json.Unmarshal([]byte(in), &got); err != nil {
			t.Errorf("Unmarshal(%s) failed: %v", in, err)
		}
		if got.Map() != nil {
			t.Errorf("Unmarshal(%s) = %v, want no labels", in, got.Map())
		}
	}
}

func TestImpactLabelsEmptyValueIsKept(t *testing.T) {
	// A label may have an empty value, and `impact.ai/opt-out=` still says "opted out".
	var got ImpactLabels
	if err := json.Unmarshal([]byte(`{"impact.ai/opt-out": ""}`), &got); err != nil {
		t.Fatal(err)
	}
	if v, ok := got.Map()["impact.ai/opt-out"]; !ok || v != "" {
		t.Errorf("labels = %v, want the key with an empty value", got.Map())
	}
}

func TestMetadataOfWorkloadsNamespacesAndTemplatesCarriesTheImpactLabels(t *testing.T) {
	in := `{
		"metadata": {"name": "api", "namespace": "shop", "labels": {"app": "api", "impact.ai/opt-out": "true", "token": "sk-live-abcdefghijklmnop"}},
		"spec": {"template": {
			"metadata": {"labels": {"app": "api", "impact.ai/team": "ai"}, "annotations": {"impact.ai/x": "y"}},
			"spec": {"containers": [{"name": "app", "image": "x"}]}
		}}
	}`
	var d Deployment
	if err := json.Unmarshal([]byte(in), &d); err != nil {
		t.Fatal(err)
	}
	if got := d.Metadata.Labels.Map(); !reflect.DeepEqual(got, map[string]string{"impact.ai/opt-out": "true"}) {
		t.Errorf("deployment labels = %v", got)
	}
	if got := d.Spec.Template.Metadata.Labels.Map(); !reflect.DeepEqual(got, map[string]string{"impact.ai/team": "ai"}) {
		t.Errorf("template labels = %v", got)
	}
	if d.Spec.Template.Metadata.Annotations["impact.ai/x"] != "y" {
		t.Errorf("template annotations lost: %v", d.Spec.Template.Metadata.Annotations)
	}

	var ns Namespace
	if err := json.Unmarshal([]byte(`{"metadata":{"name":"shop","uid":"u","labels":{"kubernetes.io/metadata.name":"shop","impact.ai/opt-out":"true","owner-email":"someone@example.com"}},"status":{"phase":"Active"}}`), &ns); err != nil {
		t.Fatal(err)
	}
	if got := ns.Metadata.Labels.Map(); !reflect.DeepEqual(got, map[string]string{"impact.ai/opt-out": "true"}) {
		t.Errorf("namespace labels = %v", got)
	}
	blob, _ := json.Marshal([]any{d, ns})
	for _, leak := range []string{"sk-live", "owner-email", "someone@example.com", "kubernetes.io/metadata.name"} {
		if strings.Contains(string(blob), leak) {
			t.Errorf("decoded metadata contains %q: %s", leak, blob)
		}
	}
}

// The landed reflection walk (TestDecodeStructsAreNamesOnly) rejects maps and interfaces in anything a Kubernetes
// answer is decoded into. The labels are a slice of tagged pairs for exactly that reason; this keeps the shape.
func TestImpactLabelsAreASliceOfTaggedPairsNotAMap(t *testing.T) {
	typ := reflect.TypeOf(ImpactLabels{})
	if typ.Kind() != reflect.Slice || typ.Elem().Kind() != reflect.Struct {
		t.Fatalf("ImpactLabels is %s of %s, want a slice of structs", typ.Kind(), typ.Elem().Kind())
	}
	if !reflect.PointerTo(typ).Implements(reflect.TypeOf((*json.Unmarshaler)(nil)).Elem()) {
		t.Error("ImpactLabels does not implement json.Unmarshaler")
	}
	elem := typ.Elem()
	for i := 0; i < elem.NumField(); i++ {
		f := elem.Field(i)
		tag := strings.Split(f.Tag.Get("json"), ",")[0]
		if tag == "" || tag == "-" {
			t.Errorf("%s has no json tag", f.Name)
		}
		switch tag {
		case "value", "args", "data", "stringData", "binaryData", "caBundle":
			t.Errorf("%s has the banned JSON name %q", f.Name, tag)
		}
		if f.Type.Kind() != reflect.String {
			t.Errorf("%s is %s, want a string", f.Name, f.Type.Kind())
		}
	}
	for _, holder := range []any{ObjectMeta{}, TemplateMeta{}} {
		field, ok := reflect.TypeOf(holder).FieldByName("Labels")
		if !ok || field.Type != typ || field.Tag.Get("json") != "labels" {
			t.Errorf("%T has no labels field of type ImpactLabels tagged \"labels\": %+v", holder, field)
		}
	}
}

// impact.ai refuses a whole snapshot when one workload holds more than 16 impact.ai labels or a label whose name or value
// is not a Kubernetes label's (capabilities/ai-coverage/src/inventory.ts, go/inventory.go). The API server only lets
// valid labels exist, but one odd object must never cost a cluster its inventory: the reporter keeps to the grammar.
func TestAnObjectReportsAtMostSixteenImpactLabelsAndOnlyValidOnes(t *testing.T) {
	var in strings.Builder
	in.WriteString(`{"impact.ai/opt-out":"true"`)
	for i := 0; i < 24; i++ {
		fmt.Fprintf(&in, `,"impact.ai/l%02d":"v"`, i)
	}
	in.WriteString(`}`)
	var got ImpactLabels
	if err := json.Unmarshal([]byte(in.String()), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != MaxImpactLabels {
		t.Fatalf("%d labels kept, want %d", len(got), MaxImpactLabels)
	}
	keys := make([]string, len(got))
	for i, l := range got {
		keys[i] = l.Key
	}
	if !slices.IsSorted(keys) || keys[0] != "impact.ai/l00" || keys[len(keys)-1] != "impact.ai/l15" {
		t.Errorf("the first 16 by key are kept, in order: %v", keys)
	}
}

func TestALabelThatIsNotAKubernetesLabelIsLeftOutAndTheRestStay(t *testing.T) {
	long := strings.Repeat("v", 64)
	in := `{
		"impact.ai/fine": "ok",
		"impact.ai/empty": "",
		"impact.ai/dotted.value": "a.b-c_d",
		"impact.ai/spaces": "has space",
		"impact.ai/long": "` + long + `",
		"impact.ai/edge": "-leading",
		"impact.ai/trail": "trailing-",
		"impact.ai/quote": "say \"hi\"",
		"impact.ai/-badname": "x",
		"impact.ai/bad name": "x"
	}`
	var got ImpactLabels
	if err := json.Unmarshal([]byte(in), &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"impact.ai/fine": "ok", "impact.ai/empty": "", "impact.ai/dotted.value": "a.b-c_d"}
	if !reflect.DeepEqual(got.Map(), want) {
		t.Errorf("labels = %v, want %v", got.Map(), want)
	}
}
