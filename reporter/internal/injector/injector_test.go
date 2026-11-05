// SPDX-License-Identifier: Apache-2.0

package injector

import (
	"testing"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kube"
)

func wh(name, svcName, svcNamespace, path string) kube.Webhook {
	w := kube.Webhook{Name: name}
	if svcName != "" || svcNamespace != "" || path != "" {
		w.ClientConfig.Service = &kube.ServiceReference{Name: svcName, Namespace: svcNamespace, Path: path}
	}
	return w
}

func cfg(name string, hooks ...kube.Webhook) kube.MutatingWebhookConfiguration {
	return kube.MutatingWebhookConfiguration{
		Metadata: kube.WebhookConfigMeta{Name: name, Annotations: kube.ReleaseAnnotations{}},
		Webhooks: hooks,
	}
}

func helmOwned(c kube.MutatingWebhookConfiguration, release, namespace string) kube.MutatingWebhookConfiguration {
	c.Metadata.Annotations[kube.HelmReleaseNameAnnotation] = release
	c.Metadata.Annotations[kube.HelmReleaseNamespaceAnnotation] = namespace
	return c
}

// The webhooks of the upstream OpenTelemetry Operator chart.
func otelOperator(name string) kube.MutatingWebhookConfiguration {
	return cfg(name,
		wh("minstrumentation.kb.io", name+"-webhook", "otel", "/mutate-opentelemetry-io-v1alpha1-instrumentation"),
		wh("mopentelemetrycollectorbeta.kb.io", name+"-webhook", "otel", "/mutate-opentelemetry-io-v1beta1-opentelemetrycollector"),
		wh("mpod.kb.io", name+"-webhook", "otel", "/mutate-v1-pod"),
	)
}

var own = Release{Name: "impact-cluster-kit", Namespace: "impact-system"}

func TestDetect(t *testing.T) {
	certManager := cfg("cert-manager-webhook", wh("webhook.cert-manager.io", "cert-manager-webhook", "cert-manager", "/mutate"))
	istio := cfg("istio-sidecar-injector", wh("namespace.sidecar-injector.istio.io", "istiod", "istio-system", "/inject"))
	kyverno := cfg("kyverno-resource-mutating-webhook-cfg", wh("mutate.kyverno.svc-fail", "kyverno-svc", "kyverno", "/mutate/fail"))
	otherPodWebhook := cfg("policy", wh("policy.example.com", "policy-svc", "policy", "/mutate-v1-pod"))

	cases := []struct {
		name    string
		configs []kube.MutatingWebhookConfiguration
		want    string
	}{
		{"no webhooks at all", nil, ""},
		{"harmless webhooks only", []kube.MutatingWebhookConfiguration{certManager, istio, kyverno, otherPodWebhook}, ""},

		{"aws add-on by name", []kube.MutatingWebhookConfiguration{
			cfg("amazon-cloudwatch-observability-mutating-webhook-configuration",
				wh("mpod.kb.io", "amazon-cloudwatch-observability-webhook-service", "amazon-cloudwatch", "/mutate-v1-pod"),
				wh("minstrumentation.kb.io", "amazon-cloudwatch-observability-webhook-service", "amazon-cloudwatch", "/mutate-cloudwatch-aws-amazon-com-v1alpha1-instrumentation")),
		}, AWSCloudWatch},
		{"aws add-on by name fragment", []kube.MutatingWebhookConfiguration{cfg("my-cloudwatch-observability-hook", wh("x.example.com", "s", "n", "/p"))}, AWSCloudWatch},
		{"aws add-on by service namespace", []kube.MutatingWebhookConfiguration{cfg("renamed", wh("a.example.com", "svc", "amazon-cloudwatch", "/x"))}, AWSCloudWatch},

		{"datadog by name", []kube.MutatingWebhookConfiguration{cfg("datadog-webhook", wh("datadog.webhook.config", "datadog-cluster-agent-admission-controller", "datadog", "/injectconfig"))}, Datadog},
		{"datadog by webhook prefix", []kube.MutatingWebhookConfiguration{cfg("renamed", wh("datadog.webhook.auto.instrumentation", "agent", "dd", "/injectlib"))}, Datadog},

		{"dynatrace by name", []kube.MutatingWebhookConfiguration{cfg("dynatrace-webhook", wh("webhook.pod.dynatrace.com", "dynatrace-webhook", "dynatrace", "/inject"))}, Dynatrace},
		{"dynatrace by webhook suffix", []kube.MutatingWebhookConfiguration{cfg("renamed", wh("webhook.dynatrace.com", "s", "n", "/inject"))}, Dynatrace},
		{"a webhook that merely mentions dynatrace is not dynatrace", []kube.MutatingWebhookConfiguration{cfg("renamed", wh("notdynatrace.com", "s", "n", "/inject"))}, ""},

		{"opentelemetry operator, upstream manifests", []kube.MutatingWebhookConfiguration{otelOperator("opentelemetry-operator-mutating-webhook-configuration")}, OpenTelemetryOperator},
		{"opentelemetry operator, helm chart name", []kube.MutatingWebhookConfiguration{otelOperator("my-release-opentelemetry-operator-mutation")}, OpenTelemetryOperator},
		{"opentelemetry operator, only the instrumentation webhook", []kube.MutatingWebhookConfiguration{cfg("x", wh("minstrumentation.kb.io", "s", "n", "/something"))}, OpenTelemetryOperator},
		{"opentelemetry operator, only the pod webhook name", []kube.MutatingWebhookConfiguration{cfg("x", wh("mpod.kb.io", "s", "n", "/something"))}, OpenTelemetryOperator},
		{"opentelemetry operator, pod path on an operator service", []kube.MutatingWebhookConfiguration{cfg("x", wh("renamed.example.com", "my-opentelemetry-operator-webhook", "obs", "/mutate-v1-pod"))}, OpenTelemetryOperator},
		{"opentelemetry operator, instrumentation path only", []kube.MutatingWebhookConfiguration{cfg("x", wh("renamed.example.com", "s", "n", "/mutate-opentelemetry-io-v1alpha1-instrumentation"))}, OpenTelemetryOperator},

		// When several injectors are present the first in this order names the owner.
		{"aws before datadog", []kube.MutatingWebhookConfiguration{cfg("datadog-webhook"), cfg("amazon-cloudwatch-observability-x")}, AWSCloudWatch},
		{"aws before the operator it is built on", []kube.MutatingWebhookConfiguration{otelOperator("opentelemetry-operator-x"), cfg("amazon-cloudwatch-observability-x")}, AWSCloudWatch},
		{"datadog before dynatrace", []kube.MutatingWebhookConfiguration{cfg("dynatrace-webhook"), cfg("datadog-webhook")}, Datadog},
		{"dynatrace before the operator", []kube.MutatingWebhookConfiguration{otelOperator("opentelemetry-operator-x"), cfg("dynatrace-webhook")}, Dynatrace},
		{"a harmless webhook does not hide the owner", []kube.MutatingWebhookConfiguration{certManager, cfg("datadog-webhook"), istio}, Datadog},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Detect(tc.configs, own); got != tc.want {
				t.Errorf("Detect() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestOurOwnReleaseIsNotAnotherInjector(t *testing.T) {
	ours := helmOwned(otelOperator("impact-cluster-kit-opentelemetry-operator"), "impact-cluster-kit", "impact-system")

	if got := Detect([]kube.MutatingWebhookConfiguration{ours}, own); got != "" {
		t.Errorf("our own operator must not count as another injector, got %q", got)
	}

	// Same objects from a different release or namespace do count.
	for _, other := range []kube.MutatingWebhookConfiguration{
		helmOwned(otelOperator("their-opentelemetry-operator"), "their-release", "impact-system"),
		helmOwned(otelOperator("their-opentelemetry-operator"), "impact-cluster-kit", "other-namespace"),
		otelOperator("unmanaged-opentelemetry-operator"),
	} {
		if got := Detect([]kube.MutatingWebhookConfiguration{other}, own); got != OpenTelemetryOperator {
			t.Errorf("a foreign operator (%s) must be detected, got %q", other.Metadata.Name, got)
		}
	}

	// Ours plus a foreign one: the foreign one decides.
	both := []kube.MutatingWebhookConfiguration{ours, cfg("datadog-webhook")}
	if got := Detect(both, own); got != Datadog {
		t.Errorf("got %q, want datadog", got)
	}

	// Without release flags nothing can be recognised as ours.
	if got := Detect([]kube.MutatingWebhookConfiguration{ours}, Release{}); got != OpenTelemetryOperator {
		t.Errorf("without a release identity got %q, want opentelemetry-operator", got)
	}
	if got := Detect([]kube.MutatingWebhookConfiguration{ours}, Release{Name: "impact-cluster-kit"}); got != OpenTelemetryOperator {
		t.Errorf("a release name without a namespace is not an identity, got %q", got)
	}
}

func TestOwnersAreTheDocumentedValues(t *testing.T) {
	want := []string{"aws-cloudwatch", "datadog", "dynatrace", "opentelemetry-operator"}
	for i, sig := range signatures {
		if sig.owner != want[i] {
			t.Errorf("signature %d owner = %q, want %q (the order is the precedence)", i, sig.owner, want[i])
		}
	}
	if len(signatures) != len(want) {
		t.Errorf("have %d signatures, want %d", len(signatures), len(want))
	}
}
