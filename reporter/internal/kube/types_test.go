// SPDX-License-Identifier: Apache-2.0

package kube

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestAnnotationAllowlist(t *testing.T) {
	cases := []struct {
		key  string
		want bool
	}{
		{"instrumentation.opentelemetry.io/inject-python", true},
		{"instrumentation.opentelemetry.io/otel-python-platform", true},
		{"impact.ai/opt-out", true},
		{"impact.ai/cover", true},
		{"admission.datadoghq.com/enabled", true},
		{"admission.datadoghq.com/python-lib.version", true},
		{"cloudwatch.aws.amazon.com/auto-annotate-python", true},
		{"dynatrace.com/inject", true},
		{"oneagent.dynatrace.com/inject", true},
		{"container.inject.dynatrace.com/app", true},
		{"feature.dynatrace.com/automatic-injection", true},
		// Near misses must not pass.
		{"evil-dynatrace.com/inject", false},
		{"notimpact.ai/opt-out", false},
		{"sub.impact.ai/opt-out", false},
		{"instrumentation.opentelemetry.io.evil/inject", false},
		{"admission.datadoghq.com.evil/enabled", false},
		{"cloudwatch.aws.amazon.com.evil/x", false},
		{"impact.ai", false},
		{"dynatrace.com", false},
		{"", false},
		{"/inject", false},
		// The classic leak and ordinary application annotations.
		{"kubectl.kubernetes.io/last-applied-configuration", false},
		{"kubectl.kubernetes.io/restartedAt", false},
		{"example.com/team", false},
		{"checksum/config", false},
		{"vault.hashicorp.com/agent-inject-secret-db", false},
	}
	for _, tc := range cases {
		if got := IsAllowedAnnotation(tc.key); got != tc.want {
			t.Errorf("IsAllowedAnnotation(%q) = %v, want %v", tc.key, got, tc.want)
		}
	}
}

func TestAllowedAnnotationsKeepsOnlyAllowlistedKeys(t *testing.T) {
	long := strings.Repeat("v", 600)
	in := `{
		"kubectl.kubernetes.io/last-applied-configuration": "{\"env\":[{\"name\":\"OPENAI_API_KEY\",\"value\":\"sk-live-abcdefghijklmnop\"}]}",
		"instrumentation.opentelemetry.io/inject-python": "impact-system/impact-cluster-kit",
		"impact.ai/opt-out": "true",
		"example.com/token": "sk-live-abcdefghijklmnop",
		"admission.datadoghq.com/python-lib.version": "` + long + `",
		"oneagent.dynatrace.com/inject": "false"
	}`
	var got AllowedAnnotations
	if err := json.Unmarshal([]byte(in), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != 4 {
		t.Fatalf("kept %d keys, want 4: %v", len(got), got)
	}
	for _, key := range []string{
		"instrumentation.opentelemetry.io/inject-python",
		"impact.ai/opt-out",
		"admission.datadoghq.com/python-lib.version",
		"oneagent.dynatrace.com/inject",
	} {
		if _, ok := got[key]; !ok {
			t.Errorf("missing allowlisted key %q", key)
		}
	}
	if v := got["admission.datadoghq.com/python-lib.version"]; len(v) != MaxAnnotationValueLength {
		t.Errorf("long value has length %d, want it capped at %d", len(v), MaxAnnotationValueLength)
	}
	blob, _ := json.Marshal(got)
	for _, secret := range []string{"sk-live", "OPENAI_API_KEY", "last-applied", "example.com/token"} {
		if strings.Contains(string(blob), secret) {
			t.Errorf("decoded annotations contain %q: %s", secret, blob)
		}
	}
}

func TestAllowedAnnotationsToleratesOddInput(t *testing.T) {
	for _, in := range []string{`null`, `{}`, `{"impact.ai/opt-out": 5}`, `{"impact.ai/opt-out": null, "impact.ai/x": "y"}`} {
		var got AllowedAnnotations
		if err := json.Unmarshal([]byte(in), &got); err != nil {
			t.Errorf("Unmarshal(%s) failed: %v", in, err)
		}
	}
	var got AllowedAnnotations
	if err := json.Unmarshal([]byte(`{"impact.ai/opt-out": 5, "impact.ai/x": "y"}`), &got); err != nil {
		t.Fatal(err)
	}
	if got["impact.ai/x"] != "y" {
		t.Errorf("a non-string allowlisted value must not break the following keys: %v", got)
	}
}

func TestFirstArgKeepsOnlyTheFirstElement(t *testing.T) {
	var c Container
	in := `{"name":"app","command":["/bin/sh","-c","python app.py --api-key=sk-live-abcdefghijklmnop"],"args":["--api-key=sk-live-abcdefghijklmnop"]}`
	if err := json.Unmarshal([]byte(in), &c); err != nil {
		t.Fatal(err)
	}
	if c.Command.First != "/bin/sh" {
		t.Errorf("first = %q, want /bin/sh", c.Command.First)
	}
	blob, _ := json.Marshal(c)
	if strings.Contains(string(blob), "sk-live") || strings.Contains(string(blob), "python app.py") {
		t.Errorf("container kept more than the first command element: %s", blob)
	}
	for _, in := range []string{`{"command":null}`, `{"command":[]}`, `{}`} {
		var c Container
		if err := json.Unmarshal([]byte(in), &c); err != nil || c.Command.First != "" {
			t.Errorf("Unmarshal(%s) = %q, %v; want empty and no error", in, c.Command.First, err)
		}
	}
}

func TestContainerDecodingNeverSeesValues(t *testing.T) {
	in := `{
		"name": "app", "image": "registry.example.com/shop/app:1.2.3",
		"command": ["python"], "args": ["--api-key=sk-live-abcdefghijklmnop"],
		"env": [
			{"name": "OPENAI_API_KEY", "value": "sk-live-abcdefghijklmnop"},
			{"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "openai-credentials", "key": "api-key"}}},
			{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}
		],
		"envFrom": [
			{"secretRef": {"name": "llm-config"}, "prefix": "LLM_"},
			{"configMapRef": {"name": "app-config"}}
		],
		"volumeMounts": [{"name": "otel", "mountPath": "/otel-auto-instrumentation-python"}]
	}`
	var c Container
	if err := json.Unmarshal([]byte(in), &c); err != nil {
		t.Fatal(err)
	}
	blob, _ := json.Marshal(c)
	if strings.Contains(string(blob), "sk-live") {
		t.Fatalf("a secret value survived decoding: %s", blob)
	}
	if len(c.Env) != 3 || c.Env[0].Name != "OPENAI_API_KEY" {
		t.Errorf("env names lost: %+v", c.Env)
	}
	if ref := c.Env[1].ValueFrom; ref == nil || ref.SecretKeyRef == nil || ref.SecretKeyRef.Name != "openai-credentials" || ref.SecretKeyRef.Key != "api-key" {
		t.Errorf("secretKeyRef lost: %+v", c.Env[1])
	}
	if c.EnvFrom[0].SecretRef == nil || c.EnvFrom[0].SecretRef.Name != "llm-config" || c.EnvFrom[1].ConfigMapRef == nil {
		t.Errorf("envFrom lost: %+v", c.EnvFrom)
	}
}

func TestPodMetaKeepsTheUIDToTellPodsApart(t *testing.T) {
	var pod PodMeta
	in := `{"name":"p","namespace":"ns","uid":"3f1c0d2e-0000-4000-8000-000000000001","deletionTimestamp":"2026-11-03T12:00:00Z"}`
	if err := json.Unmarshal([]byte(in), &pod); err != nil {
		t.Fatal(err)
	}
	if pod.UID != "3f1c0d2e-0000-4000-8000-000000000001" {
		t.Errorf("uid = %q", pod.UID)
	}
}

func TestLabelsKeepOnlyKnownKeys(t *testing.T) {
	var pod PodMeta
	in := `{"name":"p","labels":{"pod-template-hash":"5d9c7b6f8d","app":"support","secret-ish":"sk-live-abcdefghijklmnop"}}`
	if err := json.Unmarshal([]byte(in), &pod); err != nil {
		t.Fatal(err)
	}
	if len(pod.Labels) != 1 || pod.Labels["pod-template-hash"] != "5d9c7b6f8d" {
		t.Errorf("pod labels = %v, want only pod-template-hash", pod.Labels)
	}

	var node NodeMeta
	in = `{"labels":{"eks.amazonaws.com/nodegroup":"ng-1","topology.kubernetes.io/region":"us-east-1","kubernetes.io/hostname":"ip-10-0-0-1","x":"y"}}`
	if err := json.Unmarshal([]byte(in), &node); err != nil {
		t.Fatal(err)
	}
	if len(node.Labels) != 2 || node.Labels["eks.amazonaws.com/nodegroup"] != "ng-1" || node.Labels["topology.kubernetes.io/region"] != "us-east-1" {
		t.Errorf("node labels = %v", node.Labels)
	}
}

func TestWebhookConfigurationDecodingSkipsCABundles(t *testing.T) {
	in := `{"metadata":{"name":"x","annotations":{"meta.helm.sh/release-name":"kit","meta.helm.sh/release-namespace":"impact-system","other":"secret-annotation"}},
		"webhooks":[{"name":"mpod.kb.io","clientConfig":{"caBundle":"TOP-SECRET-CA","service":{"name":"svc","namespace":"ns","path":"/mutate-v1-pod","port":443}}}]}`
	var cfg MutatingWebhookConfiguration
	if err := json.Unmarshal([]byte(in), &cfg); err != nil {
		t.Fatal(err)
	}
	blob, _ := json.Marshal(cfg)
	for _, leak := range []string{"TOP-SECRET-CA", "secret-annotation"} {
		if strings.Contains(string(blob), leak) {
			t.Errorf("decoded webhook configuration contains %q: %s", leak, blob)
		}
	}
	if cfg.Metadata.Annotations[HelmReleaseNameAnnotation] != "kit" || cfg.Metadata.Annotations[HelmReleaseNamespaceAnnotation] != "impact-system" {
		t.Errorf("release annotations lost: %v", cfg.Metadata.Annotations)
	}
	if got := cfg.Webhooks[0].ClientConfig.Service; got == nil || got.Name != "svc" || got.Namespace != "ns" || got.Path != "/mutate-v1-pod" {
		t.Errorf("service lost: %+v", got)
	}
}

func TestWorkloadConditionsKeepTheTypeTheStatusAndTheReasonAndNeverTheMessage(t *testing.T) {
	in := `{"metadata":{"name":"api","namespace":"shop"},"status":{"readyReplicas":1,"conditions":[
		{"type":"Available","status":"False","reason":"MinimumReplicasUnavailable","message":"Deployment does not have minimum availability.","lastUpdateTime":"2026-11-03T11:00:00Z"},
		{"type":"ReplicaFailure","status":"True","reason":"FailedCreate","message":"pods \"api-1\" is forbidden: token sk-live-abcdefghijklmnop is not allowed","lastTransitionTime":"2026-11-03T11:00:00Z"}]}}`
	var d Deployment
	if err := json.Unmarshal([]byte(in), &d); err != nil {
		t.Fatal(err)
	}
	if len(d.Status.Conditions) != 2 || d.Status.Conditions[1] != (WorkloadCondition{Type: "ReplicaFailure", Status: "True", Reason: "FailedCreate"}) {
		t.Errorf("conditions = %+v", d.Status.Conditions)
	}
	blob, _ := json.Marshal(d)
	for _, leak := range []string{"sk-live", "forbidden", "minimum availability", "message"} {
		if strings.Contains(string(blob), leak) {
			t.Errorf("a condition kept %q: %s", leak, blob)
		}
	}
	for _, in := range []string{`{"status":{"conditions":null}}`, `{"status":{"conditions":[]}}`, `{"status":{"conditions":[{"type":5,"reason":["x"]}]}}`} {
		var d Deployment
		_ = json.Unmarshal([]byte(in), &d) // an odd condition may fail its own page, never a panic
	}
}

func TestInitContainerStatusesKeepTheNameAndTheWaitingReasonAndNeverTheMessage(t *testing.T) {
	in := `{"metadata":{"name":"api-1"},"status":{"phase":"Pending","initContainerStatuses":[
		{"name":"opentelemetry-auto-instrumentation-python","image":"ghcr.io/example/python:1","imageID":"","ready":false,"restartCount":0,
		 "state":{"waiting":{"reason":"ImagePullBackOff","message":"Back-off pulling image \"mirror.example.com/x\": token sk-live-abcdefghijklmnop"}},
		 "lastState":{"terminated":{"reason":"Error","message":"sk-live-abcdefghijklmnop","exitCode":1}}},
		{"name":"istio-init","state":{"running":{"startedAt":"2026-11-03T11:00:00Z"}}},
		{"name":"wait","state":{"terminated":{"reason":"Completed","exitCode":0}}}],
		"containerStatuses":[{"name":"app","state":{"waiting":{"reason":"PodInitializing"}}}]}}`
	var p Pod
	if err := json.Unmarshal([]byte(in), &p); err != nil {
		t.Fatal(err)
	}
	statuses := p.Status.InitContainerStatuses
	if len(statuses) != 3 || statuses[0].Name != "opentelemetry-auto-instrumentation-python" ||
		statuses[0].State.Waiting == nil || statuses[0].State.Waiting.Reason != "ImagePullBackOff" {
		t.Fatalf("init container statuses = %+v", statuses)
	}
	if statuses[1].State.Waiting != nil || statuses[2].State.Waiting != nil {
		t.Errorf("a container that runs or has finished is not waiting: %+v", statuses)
	}
	blob, _ := json.Marshal(p)
	for _, leak := range []string{"sk-live", "mirror.example.com", "Back-off", "message", "Error"} {
		if strings.Contains(string(blob), leak) {
			t.Errorf("a status kept %q: %s", leak, blob)
		}
	}
}

func TestLenientTime(t *testing.T) {
	var s JobStatus
	in := `{"active":1,"completionTime":"2026-11-03T12:00:00Z","conditions":[{"type":"Complete","status":"True","lastTransitionTime":"not-a-time"}]}`
	if err := json.Unmarshal([]byte(in), &s); err != nil {
		t.Fatalf("a bad timestamp must not fail the whole page: %v", err)
	}
	if s.CompletionTime.IsZero() || !s.Conditions[0].LastTransitionTime.IsZero() {
		t.Errorf("times = %v / %v", s.CompletionTime, s.Conditions[0].LastTransitionTime)
	}
}

// Every type a Kubernetes response is decoded into must be names only (AE-3).
// The guard walks the types by reflection: no field may carry a value-bearing
// JSON name, every field must be tagged (an untagged field would be matched by
// its Go name), and free-form containers (any, RawMessage, plain maps) are not
// allowed because they could hold values. The few custom decoders are listed
// explicitly and each has its own test above.
func TestDecodeStructsAreNamesOnly(t *testing.T) {
	// A message can hold what a policy, a registry or a webhook wrote about an object: it is read nowhere.
	banned := map[string]bool{"value": true, "args": true, "data": true, "stringData": true, "binaryData": true, "caBundle": true, "message": true}
	roots := []any{
		VersionInfo{}, Namespace{}, Node{}, Deployment{}, StatefulSet{}, DaemonSet{}, CronJob{}, Job{}, Pod{},
		MutatingWebhookConfiguration{},
	}
	opaque := map[reflect.Type]bool{
		reflect.TypeOf(FirstArg{}):           true,
		reflect.TypeOf(LenientTime{}):        true,
		reflect.TypeOf(AllowedAnnotations{}): true,
		reflect.TypeOf(ReleaseAnnotations{}): true,
		reflect.TypeOf(PodLabels{}):          true,
		reflect.TypeOf(NodeLabels{}):         true,
	}
	for typ := range opaque {
		if !reflect.PointerTo(typ).Implements(reflect.TypeOf((*json.Unmarshaler)(nil)).Elem()) {
			t.Errorf("%s is listed as a custom decoder but does not implement json.Unmarshaler", typ)
		}
	}
	seen := map[reflect.Type]bool{}
	var walk func(path string, typ reflect.Type)
	walk = func(path string, typ reflect.Type) {
		if seen[typ] || opaque[typ] {
			return
		}
		seen[typ] = true
		switch typ.Kind() {
		case reflect.Pointer, reflect.Slice, reflect.Array:
			walk(path, typ.Elem())
		case reflect.Map:
			t.Errorf("%s: map type %s may carry values", path, typ)
		case reflect.Interface:
			t.Errorf("%s: interface type %s may carry values", path, typ)
		case reflect.Struct:
			if typ == reflect.TypeOf(json.RawMessage{}) {
				t.Errorf("%s: json.RawMessage may carry values", path)
				return
			}
			for i := 0; i < typ.NumField(); i++ {
				f := typ.Field(i)
				tag := strings.Split(f.Tag.Get("json"), ",")[0]
				switch {
				case tag == "" || tag == "-":
					t.Errorf("%s.%s has no json tag", path, f.Name)
				case banned[tag]:
					t.Errorf("%s.%s has banned JSON name %q", path, f.Name, tag)
				}
				walk(path+"."+f.Name, f.Type)
			}
		}
	}
	for _, root := range roots {
		walk(reflect.TypeOf(root).Name(), reflect.TypeOf(root))
	}
	if len(seen) < 25 {
		t.Errorf("the guard only visited %d types; did the walk break?", len(seen))
	}
}
