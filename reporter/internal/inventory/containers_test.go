// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kube"
)

func TestExecutables(t *testing.T) {
	long := strings.Repeat("a", 65)
	cases := []struct {
		first string
		want  []string
	}{
		{"python", []string{"python"}},
		{"/usr/local/bin/python3.12", []string{"python3.12"}},
		{"/bin/sh", []string{"sh"}},
		{"./server", []string{"server"}},
		{"node", []string{"node"}},
		{strings.Repeat("a", 64), []string{strings.Repeat("a", 64)}},
		{"", []string{}},
		{"/", []string{}},
		{".", []string{}},
		{long, []string{}},
		{"--api-key=" + fakeKey, []string{}},
		{"python app.py", []string{}},
		{"py\tthon", []string{}},
		{"/opt/app dir/server", []string{}},
		{"bad=name", []string{}},
		{"ctl\x01", []string{}},
	}
	for _, tc := range cases {
		if got := executables(tc.first); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("executables(%q) = %q, want %q", tc.first, got, tc.want)
		}
	}
}

func TestContainerFactsEnvironment(t *testing.T) {
	var env []kube.EnvVar
	for _, n := range []string{"Z_LAST", "A_FIRST", "A_FIRST", "OPENAI_API_KEY", "", "has space", "has=equals", "ctl\x07", strings.Repeat("N", 254)} {
		env = append(env, kube.EnvVar{Name: n})
	}
	env = append(env,
		kube.EnvVar{Name: strings.Repeat("N", 253)},
		kube.EnvVar{Name: "API_KEY", ValueFrom: &kube.EnvVarSource{SecretKeyRef: &kube.KeyRef{Name: "openai-credentials", Key: "api-key"}}},
		kube.EnvVar{Name: "API_KEY", ValueFrom: &kube.EnvVarSource{SecretKeyRef: &kube.KeyRef{Name: "openai-credentials", Key: "api-key"}}},
		kube.EnvVar{Name: "TOKEN", ValueFrom: &kube.EnvVarSource{SecretKeyRef: &kube.KeyRef{Name: "b-secret", Key: "token"}}},
		kube.EnvVar{Name: "EMPTY_REF", ValueFrom: &kube.EnvVarSource{SecretKeyRef: &kube.KeyRef{Name: "", Key: "k"}}},
		kube.EnvVar{Name: "NO_KEY", ValueFrom: &kube.EnvVarSource{SecretKeyRef: &kube.KeyRef{Name: "s", Key: ""}}},
		kube.EnvVar{Name: "FROM_FIELD", ValueFrom: &kube.EnvVarSource{}},
	)
	c := kube.Container{
		Name:  "app",
		Image: "registry.example.com/shop/app:1.0",
		Env:   env,
		EnvFrom: []kube.EnvFromSource{
			{SecretRef: &kube.NameRef{Name: "llm-config"}},
			{ConfigMapRef: &kube.NameRef{Name: "app-config"}},
			{SecretRef: &kube.NameRef{Name: "llm-config"}},
			{SecretRef: &kube.NameRef{Name: ""}},
			{},
		},
		Command: kube.FirstArg{First: "/usr/bin/python"},
	}
	got := containerFacts(c)

	wantNames := []string{"API_KEY", "A_FIRST", "EMPTY_REF", "FROM_FIELD", strings.Repeat("N", 253), "NO_KEY", "OPENAI_API_KEY", "TOKEN", "Z_LAST"}
	if !reflect.DeepEqual(got.EnvNames, wantNames) {
		t.Errorf("envNames = %q\nwant %q", got.EnvNames, wantNames)
	}
	wantRefs := []SecretRef{
		{EnvName: "API_KEY", Secret: "openai-credentials", Key: "api-key"},
		{EnvName: "TOKEN", Secret: "b-secret", Key: "token"},
	}
	if !reflect.DeepEqual(got.SecretRefs, wantRefs) {
		t.Errorf("secretRefs = %v, want %v", got.SecretRefs, wantRefs)
	}
	wantFrom := []EnvFromRef{{Kind: "ConfigMap", Name: "app-config"}, {Kind: "Secret", Name: "llm-config"}}
	if !reflect.DeepEqual(got.EnvFrom, wantFrom) {
		t.Errorf("envFrom = %v, want %v", got.EnvFrom, wantFrom)
	}
	if got.Name != "app" || got.Image != "registry.example.com/shop/app:1.0" || !reflect.DeepEqual(got.Executables, []string{"python"}) || got.Injected.Python {
		t.Errorf("container = %+v", got)
	}
}

func TestEnvNamesAreCappedAtFiveHundred(t *testing.T) {
	var env []kube.EnvVar
	for i := 0; i < 700; i++ {
		env = append(env, kube.EnvVar{Name: fmt.Sprintf("VAR_%04d", i)})
	}
	got := containerFacts(kube.Container{Name: "app", Env: env}).EnvNames
	if len(got) != 500 || got[0] != "VAR_0000" || got[499] != "VAR_0499" {
		t.Errorf("got %d names, first %q last %q; want the first 500 in sorted order", len(got), got[0], got[len(got)-1])
	}
}

func TestEmptyContainerHasEmptyListsNotNulls(t *testing.T) {
	got := containerFacts(kube.Container{Name: "app"})
	if got.Executables == nil || got.EnvNames == nil || got.SecretRefs == nil || got.EnvFrom == nil {
		t.Errorf("lists must be non-nil so they encode as []: %+v", got)
	}
}

func TestAnnotationAllowlistIsAppliedToWhatWeSend(t *testing.T) {
	long := strings.Repeat("x", 400)
	in := map[string]string{
		"instrumentation.opentelemetry.io/inject-python": "true",
		"impact.ai/opt-out":                                "true",
		"admission.datadoghq.com/enabled":                  "false",
		"oneagent.dynatrace.com/inject":                    "false",
		"cloudwatch.aws.amazon.com/auto-annotate-python":   long,
		"kubectl.kubernetes.io/last-applied-configuration": fakeKey,
		"example.com/team":                                 "payments",
		"evil-dynatrace.com/inject":                        "x",
	}
	got := filterAnnotations(in)
	want := map[string]string{
		"instrumentation.opentelemetry.io/inject-python": "true",
		"impact.ai/opt-out":                              "true",
		"admission.datadoghq.com/enabled":                "false",
		"oneagent.dynatrace.com/inject":                  "false",
		"cloudwatch.aws.amazon.com/auto-annotate-python": long[:256],
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("filterAnnotations = %v\nwant %v", got, want)
	}
	if empty := filterAnnotations(nil); empty == nil || len(empty) != 0 {
		t.Errorf("an empty result must be a non-nil empty map, got %#v", empty)
	}
}
