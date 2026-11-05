// SPDX-License-Identifier: Apache-2.0

// Package injector finds out whether another product already injects into the
// pods of a cluster. The signature table lives in this one file.
//
// Only mutating webhook configurations are looked at, and only their metadata:
// names, Helm release annotations, webhook names and the service they call.
package injector

import (
	"strings"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kube"
)

// Owners reported as clusterOwner.
const (
	AWSCloudWatch         = "aws-cloudwatch"
	Datadog               = "datadog"
	Dynatrace             = "dynatrace"
	OpenTelemetryOperator = "opentelemetry-operator"
)

// Release identifies this install, so its own webhooks are not mistaken for
// another injector's.
type Release struct {
	Name      string
	Namespace string
}

type signature struct {
	owner string
	match func(kube.MutatingWebhookConfiguration) bool
}

// signatures is ordered: when several injectors are present, the first one
// names the owner. The AWS add-on is built on the OpenTelemetry Operator and
// shares its webhook names, so it must come before the generic Operator check.
var signatures = []signature{
	{AWSCloudWatch, func(c kube.MutatingWebhookConfiguration) bool {
		name := c.Metadata.Name
		return strings.Contains(name, "cloudwatch-observability") ||
			strings.Contains(name, "amazon-cloudwatch") ||
			anyWebhook(c, func(w kube.Webhook) bool {
				return w.ClientConfig.Service != nil && w.ClientConfig.Service.Namespace == "amazon-cloudwatch"
			})
	}},
	{Datadog, func(c kube.MutatingWebhookConfiguration) bool {
		return c.Metadata.Name == "datadog-webhook" ||
			anyWebhook(c, func(w kube.Webhook) bool { return strings.HasPrefix(w.Name, "datadog.webhook.") })
	}},
	{Dynatrace, func(c kube.MutatingWebhookConfiguration) bool {
		return c.Metadata.Name == "dynatrace-webhook" ||
			anyWebhook(c, func(w kube.Webhook) bool { return strings.HasSuffix(w.Name, ".dynatrace.com") })
	}},
	{OpenTelemetryOperator, func(c kube.MutatingWebhookConfiguration) bool {
		return anyWebhook(c, func(w kube.Webhook) bool {
			if w.Name == "mpod.kb.io" || w.Name == "minstrumentation.kb.io" {
				return true
			}
			svc := w.ClientConfig.Service
			if svc == nil {
				return false
			}
			return svc.Path == "/mutate-opentelemetry-io-v1alpha1-instrumentation" ||
				(svc.Path == "/mutate-v1-pod" && strings.Contains(svc.Name, "opentelemetry-operator"))
		})
	}},
}

func anyWebhook(c kube.MutatingWebhookConfiguration, pred func(kube.Webhook) bool) bool {
	for _, w := range c.Webhooks {
		if pred(w) {
			return true
		}
	}
	return false
}

func ownedBy(c kube.MutatingWebhookConfiguration, own Release) bool {
	if own.Name == "" || own.Namespace == "" {
		return false
	}
	return c.Metadata.Annotations[kube.HelmReleaseNameAnnotation] == own.Name &&
		c.Metadata.Annotations[kube.HelmReleaseNamespaceAnnotation] == own.Namespace
}

// Detect returns the owner of the first signature that matches a configuration
// not created by our own release, or "" when no other injector is present.
func Detect(configs []kube.MutatingWebhookConfiguration, own Release) string {
	var foreign []kube.MutatingWebhookConfiguration
	for _, c := range configs {
		if !ownedBy(c, own) {
			foreign = append(foreign, c)
		}
	}
	for _, sig := range signatures {
		for _, c := range foreign {
			if sig.match(c) {
				return sig.owner
			}
		}
	}
	return ""
}
