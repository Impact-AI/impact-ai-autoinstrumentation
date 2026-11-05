// SPDX-License-Identifier: Apache-2.0

// Package cover is the logic of the cover pod: automatic coverage without restarting anything.
//
// impact.ai decides which workloads of a cluster are covered and publishes the list (contract impact.cover.v1,
// served at /v1/inventory/cover). The cover pod only applies it: a mutating admission webhook adds the injection
// annotation, and its marker as an annotation and a label, to the pods of the listed workloads as they are created, so
// no workload object changes, nothing rolls and a GitOps tool has nothing to fight. The OpenTelemetry Operator, whose
// webhook sees the pod afterwards, does the injecting. The pod has no Kubernetes permission and mounts no service
// account token; the list reaches it over HTTPS.
//
// It fails open everywhere: with no list, a list that says automatic coverage is off, a pod it does not understand
// or any error, the pod is admitted untouched.
package cover

// The annotations the cover pod reads and writes, and the one label it adds. Nothing else of a pod is ever changed.
const (
	// InjectPrefix starts every annotation with which the Operator is asked to inject.
	InjectPrefix = "instrumentation.opentelemetry.io/inject-"
	// InjectPython and InjectNode ask for the Python and the Node.js instrumentation.
	InjectPython = InjectPrefix + "python"
	InjectNode   = InjectPrefix + "nodejs"
	// ContainerNames limits the injection to the named container.
	ContainerNames = "instrumentation.opentelemetry.io/container-names"
	// PythonPlatform tells the Python instrumentation its C library (musl on Alpine).
	PythonPlatform = "instrumentation.opentelemetry.io/otel-python-platform"
	// Marker is put on every pod the cover pod annotated, with the time. The reporter reports it.
	Marker = "impact.ai/auto-covered"
	// MarkerLabel is the marker as a label, with the value MarkerLabelValue: the uninstall script (scripts/uninstall-
	// cluster-kit.sh) selects the pods the cover pod annotated by it, which the API server evaluates, and takes the
	// annotations and the label off them. The label has the name of the annotation and is added beside it.
	MarkerLabel      = Marker
	MarkerLabelValue = "true"
	// OptOut keeps a workload, a pod or a namespace out of automatic coverage.
	OptOut = "impact.ai/opt-out"
)
