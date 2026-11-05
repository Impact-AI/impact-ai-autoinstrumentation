// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"
)

// Pod is the part of a pod being created that the decision reads. Annotations and Labels hold every key the pod has
// (the patch depends on whether it has any annotation at all); only the values of the keys the decision reads matter.
type Pod struct {
	Namespace   string
	Labels      map[string]string
	Annotations map[string]string
	Owners      []OwnerRef
	// DryRun marks a request that changes nothing: it is answered the same but not announced.
	DryRun bool
}

// Why a pod was or was not annotated.
const (
	ReasonCovered    = "covered"
	ReasonNoList     = "no list"
	ReasonAnnotated  = "already annotated"
	ReasonOptOut     = "opted out"
	ReasonNotListed  = "not listed"
	ReasonNoWorkload = "no workload"
	ReasonRefused    = "patch refused"
)

// Op is one operation of an RFC 6902 JSON Patch. The cover pod only ever adds.
type Op struct {
	Op    string `json:"op"`
	Path  string `json:"path"`
	Value any    `json:"value"`
}

// Decision is what to do with a pod: Ops is empty unless Reason is ReasonCovered.
type Decision struct {
	Ops      []Op
	Workload Workload
	Reason   string
}

// Decider decides, for a pod being created, which annotations to add.
type Decider struct {
	Store *Store
	// Instrumentation is the value of the inject annotation: "<namespace>/<name>" of the Instrumentation resource.
	Instrumentation string
	Now             func() time.Time
	Log             *slog.Logger
}

func (d *Decider) log() *slog.Logger {
	if d.Log != nil {
		return d.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (d *Decider) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

// optedOut: the pod has the annotation with any value but "false" (as the server reads it), or the label with any
// value (the webhook's object selector skips such pods already).
func optedOut(p Pod) bool {
	if value, ok := p.Annotations[OptOut]; ok && !strings.EqualFold(value, "false") {
		return true
	}
	_, ok := p.Labels[OptOut]
	return ok
}

// Decide acts only when all of these hold: a list is loaded and automatic coverage is on; the pod does not ask for
// injection itself (no inject-* annotation); nothing opts it out; the workload it belongs to is in the list. Anything
// else leaves the pod alone.
func (d *Decider) Decide(p Pod) Decision {
	if d == nil || d.Store == nil || !d.Store.Active() {
		return Decision{Reason: ReasonNoList}
	}
	for key := range p.Annotations {
		if strings.HasPrefix(key, InjectPrefix) {
			return d.left(p, Decision{Reason: ReasonAnnotated})
		}
	}
	if optedOut(p) {
		return d.left(p, Decision{Reason: ReasonOptOut})
	}
	candidates := Candidates(p.Owners, p.Labels)
	if len(candidates) == 0 {
		return d.left(p, Decision{Reason: ReasonNoWorkload})
	}
	for _, c := range candidates {
		e, ok := d.Store.Lookup(p.Namespace, c.Kind, c.Name)
		if !ok {
			continue
		}
		ops := d.patch(p, e)
		if err := ValidatePatch(ops); err != nil {
			d.log().Error("the cover patch was refused; the pod is admitted untouched", "workload", name(p.Namespace, c), "error", err)
			return Decision{Workload: c, Reason: ReasonRefused}
		}
		level := slog.LevelInfo
		if p.DryRun {
			level = slog.LevelDebug
		}
		d.log().Log(context.Background(), level, "pod covered", "workload", name(p.Namespace, c))
		return Decision{Ops: ops, Workload: c, Reason: ReasonCovered}
	}
	return d.left(p, Decision{Reason: ReasonNotListed})
}

func name(namespace string, w Workload) string { return namespace + "/" + w.Kind + "/" + w.Name }

// left logs why a pod was left alone, with names only.
func (d *Decider) left(p Pod, dec Decision) Decision {
	d.log().Debug("pod left alone", "namespace", p.Namespace, "reason", dec.Reason)
	return dec
}

type addition struct{ key, value string }

// patch builds the additions for a listed workload: the inject annotation of its language, the container to limit it
// to and the C library when the entry says so (never over a value the pod already has), the marker, and the marker as
// a label.
func (d *Decider) patch(p Pod, e Entry) []Op {
	inject := InjectPython
	if e.Lang == "node" {
		inject = InjectNode
	}
	adds := []addition{{inject, d.Instrumentation}}
	if _, has := p.Annotations[ContainerNames]; e.Container != "" && !has {
		adds = append(adds, addition{ContainerNames, e.Container})
	}
	if _, has := p.Annotations[PythonPlatform]; e.Lang == "python" && e.Musl && !has {
		adds = append(adds, addition{PythonPlatform, "musl"})
	}
	adds = append(adds, addition{Marker, d.now().UTC().Format(time.RFC3339)})

	ops := make([]Op, 0, len(adds)+1)
	if len(p.Annotations) == 0 {
		// JSON Patch cannot add a member to an object that is not there: add the whole object.
		all := make(map[string]string, len(adds))
		for _, a := range adds {
			all[a.key] = a.value
		}
		ops = append(ops, Op{Op: "add", Path: annotationsPath, Value: all})
	} else {
		for _, a := range adds {
			ops = append(ops, Op{Op: "add", Path: annotationsPath + "/" + escapePointer(a.key), Value: a.value})
		}
	}
	if len(p.Labels) == 0 {
		return append(ops, Op{Op: "add", Path: labelsPath, Value: map[string]string{MarkerLabel: MarkerLabelValue}})
	}
	return append(ops, Op{Op: "add", Path: labelsPath + "/" + escapePointer(MarkerLabel), Value: MarkerLabelValue})
}

const (
	annotationsPath = "/metadata/annotations"
	labelsPath      = "/metadata/labels"
)

// escapePointer escapes a key for a JSON Pointer (RFC 6901).
func escapePointer(key string) string {
	return strings.ReplaceAll(strings.ReplaceAll(key, "~", "~0"), "/", "~1")
}

var allowedKeys = []string{InjectPython, InjectNode, ContainerNames, PythonPlatform, Marker}

func allowedKey(key string) bool {
	for _, k := range allowedKeys {
		if k == key {
			return true
		}
	}
	return false
}

// ValidatePatch checks that the operations touch nothing but the annotations of the cover pod and its marker label: only
// `add`, only the annotations object (with those keys inside) or one of those annotations, and the labels object with
// the marker label as its only member or the marker label itself. The decision builds nothing else; this is the guard
// that keeps a mistake in it from changing a customer's pod in any other way.
func ValidatePatch(ops []Op) error {
	for _, op := range ops {
		if op.Op != "add" {
			return fmt.Errorf("operation %q is not allowed: only add", op.Op)
		}
		if op.Path == labelsPath {
			all, ok := op.Value.(map[string]string)
			if !ok || len(all) != 1 || all[MarkerLabel] != MarkerLabelValue {
				return fmt.Errorf("%s must be added with the marker label %s=%s and nothing else", op.Path, MarkerLabel, MarkerLabelValue)
			}
			continue
		}
		if op.Path == labelsPath+"/"+escapePointer(MarkerLabel) {
			if value, ok := op.Value.(string); !ok || value != MarkerLabelValue {
				return fmt.Errorf("%s must be added with the string %q", op.Path, MarkerLabelValue)
			}
			continue
		}
		if op.Path == annotationsPath {
			all, ok := op.Value.(map[string]string)
			if !ok {
				return fmt.Errorf("%s must be added with a map of annotations", op.Path)
			}
			for key := range all {
				if !allowedKey(key) {
					return fmt.Errorf("annotation %q is not one the cover pod may add", key)
				}
			}
			continue
		}
		allowed := false
		for _, key := range allowedKeys {
			if op.Path == annotationsPath+"/"+escapePointer(key) {
				if _, ok := op.Value.(string); !ok {
					return fmt.Errorf("%s must be added with a string", op.Path)
				}
				allowed = true
				break
			}
		}
		if !allowed {
			return fmt.Errorf("path %q is not one the cover pod may change", op.Path)
		}
	}
	return nil
}

// MarshalPatch encodes operations as the JSON Patch the API server expects.
func MarshalPatch(ops []Op) ([]byte, error) {
	if ops == nil {
		ops = []Op{}
	}
	return json.Marshal(ops)
}
