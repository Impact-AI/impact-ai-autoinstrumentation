// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
)

const (
	maxRequestBytes  = 8 << 20
	reviewAPIVersion = "admission.k8s.io/v1"
	reviewKind       = "AdmissionReview"
)

// Review is the part of an AdmissionReview the cover pod reads. It is a decode target of a pod being created, which
// holds environment values, container arguments and Secret references the cover pod must never see: so it declares
// no field for any of them, and the maps of labels and annotations (keyed) keep every key but the value of only the
// two keys the decision reads.
type Review struct {
	APIVersion string   `json:"apiVersion"`
	Kind       string   `json:"kind"`
	Request    *Request `json:"request"`
}

// Request is the request of an AdmissionReview.
type Request struct {
	UID       string `json:"uid"`
	Operation string `json:"operation"`
	Namespace string `json:"namespace"`
	DryRun    bool   `json:"dryRun"`
	Object    struct {
		Metadata Metadata `json:"metadata"`
	} `json:"object"`
}

// Metadata is the metadata of the pod being created.
type Metadata struct {
	Name            string  `json:"name"`
	GenerateName    string  `json:"generateName"`
	Namespace       string  `json:"namespace"`
	Labels          keyed   `json:"labels"`
	Annotations     keyed   `json:"annotations"`
	OwnerReferences []Owner `json:"ownerReferences"`
}

// Owner is an owner reference.
type Owner struct {
	Kind       string `json:"kind"`
	Name       string `json:"name"`
	Controller bool   `json:"controller"`
}

// keyed decodes a labels or annotations object into a map that has every key and, for the keys the decision reads
// (the opt-out and the pod-template-hash), their string value. The value of any other key, which may be a whole
// manifest (kubectl.kubernetes.io/last-applied-configuration), is read past and not kept. Anything that is not an
// object, and null, are no keys.
type keyed map[string]string

func readsValueOf(key string) bool { return key == OptOut || key == "pod-template-hash" }

type skipped struct{}

func (*skipped) UnmarshalJSON([]byte) error { return nil }

func (k *keyed) UnmarshalJSON(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		*k = nil
		return nil
	}
	out := keyed{}
	for dec.More() {
		keyToken, err := dec.Token()
		if err != nil {
			break
		}
		key, _ := keyToken.(string)
		if readsValueOf(key) {
			var value string
			_ = dec.Decode(&value) // a value that is not a string reads as empty: the key is there
			out[key] = value
			continue
		}
		var skip skipped
		if err := dec.Decode(&skip); err != nil {
			break
		}
		out[key] = ""
	}
	*k = out
	return nil
}

// Pod returns what the decision reads of the pod in the review: the namespace of the request (else the pod's own),
// the labels and annotations, and the owners.
func (r Review) Pod() Pod {
	if r.Request == nil {
		return Pod{}
	}
	meta := r.Request.Object.Metadata
	namespace := r.Request.Namespace
	if namespace == "" {
		namespace = meta.Namespace
	}
	owners := make([]OwnerRef, 0, len(meta.OwnerReferences))
	for _, o := range meta.OwnerReferences {
		owners = append(owners, OwnerRef{Kind: o.Kind, Name: o.Name, Controller: o.Controller})
	}
	return Pod{
		Namespace:   namespace,
		Labels:      meta.Labels,
		Annotations: meta.Annotations,
		Owners:      owners,
		DryRun:      r.Request.DryRun,
	}
}

type reviewResponse struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Response   struct {
		UID       string `json:"uid"`
		Allowed   bool   `json:"allowed"`
		PatchType string `json:"patchType,omitempty"`
		Patch     []byte `json:"patch,omitempty"`
	} `json:"response"`
}

type webhook struct {
	decider *Decider
	log     *slog.Logger
}

// NewHandler serves the mutating webhook at POST /mutate. It never denies a pod: every request is allowed, with a JSON
// Patch when the decision is to cover the pod, and a request it cannot read is refused with an error status, which the
// webhook configuration's failurePolicy: Ignore turns into admission without a patch.
func NewHandler(d *Decider, log *slog.Logger) http.Handler {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	w := &webhook{decider: d, log: log}
	mux := http.NewServeMux()
	mux.HandleFunc("/mutate", w.mutate)
	return mux
}

func (h *webhook) mutate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequestBytes))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			h.log.Warn("admission request too large", "limit", maxRequestBytes)
			http.Error(w, "request too large", http.StatusRequestEntityTooLarge)
			return
		}
		h.log.Warn("could not read the admission request", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	var review Review
	if err := json.Unmarshal(body, &review); err != nil || review.Request == nil {
		h.log.Warn("the admission request is not an AdmissionReview", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	var out reviewResponse
	out.APIVersion, out.Kind = reviewAPIVersion, reviewKind
	out.Response.UID = review.Request.UID
	out.Response.Allowed = true
	if review.Request.Operation == "CREATE" {
		if decision := h.decider.Decide(review.Pod()); len(decision.Ops) > 0 {
			if patch, err := MarshalPatch(decision.Ops); err == nil {
				out.Response.PatchType, out.Response.Patch = "JSONPatch", patch
			}
		}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}
