// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const (
	plantedKey = "sk-live-abcdefghijklmnop"
	plantedArg = "--api-key=" + plantedKey
)

// review builds the AdmissionReview an API server sends for a pod being created by a ReplicaSet: with the whole pod,
// planted secrets in its environment, its arguments and an annotation, none of which the cover pod may keep.
func review(mutate func(request map[string]any)) []byte {
	pod := map[string]any{
		"apiVersion": "v1",
		"kind":       "Pod",
		"metadata": map[string]any{
			"generateName": "api-5d9c7b6f8d-",
			"namespace":    "shop",
			"labels":       map[string]any{"app": "api", "pod-template-hash": "5d9c7b6f8d"},
			"annotations":  map[string]any{"kubectl.kubernetes.io/last-applied-configuration": "{\"env\":\"" + plantedKey + "\"}", "team": "payments"},
			"ownerReferences": []any{map[string]any{
				"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "api-5d9c7b6f8d", "uid": "u-1", "controller": true, "blockOwnerDeletion": true,
			}},
		},
		"spec": map[string]any{
			"containers": []any{map[string]any{
				"name": "main", "image": "registry.example.com/shop/api:1",
				"args": []any{plantedArg},
				"env": []any{
					map[string]any{"name": "OPENAI_API_KEY", "value": plantedKey},
					map[string]any{"name": "DB", "valueFrom": map[string]any{"secretKeyRef": map[string]any{"name": "db", "key": "url"}}},
				},
			}},
		},
	}
	request := map[string]any{
		"uid":       "705ab4f5-6393-11e8-b7cc-42010a800002",
		"kind":      map[string]any{"group": "", "version": "v1", "kind": "Pod"},
		"resource":  map[string]any{"group": "", "version": "v1", "resource": "pods"},
		"operation": "CREATE",
		"namespace": "shop",
		"userInfo":  map[string]any{"username": "system:serviceaccount:kube-system:replicaset-controller"},
		"object":    pod,
		"oldObject": nil,
		"dryRun":    false,
	}
	if mutate != nil {
		mutate(request)
	}
	b, err := json.Marshal(map[string]any{"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "request": request})
	if err != nil {
		panic(err)
	}
	return b
}

func objectMeta(request map[string]any) map[string]any {
	return request["object"].(map[string]any)["metadata"].(map[string]any)
}

type response struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Response   struct {
		UID       string `json:"uid"`
		Allowed   bool   `json:"allowed"`
		PatchType string `json:"patchType"`
		Patch     []byte `json:"patch"`
	} `json:"response"`
}

func serve(t *testing.T, h http.Handler, method, path string, body []byte) (*httptest.ResponseRecorder, response) {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out response
	if rec.Code == http.StatusOK {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("the answer is not an AdmissionReview: %v\n%s", err, rec.Body)
		}
	}
	return rec, out
}

func handlerFor(entries ...Entry) (http.Handler, *bytes.Buffer) {
	var logs bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	d := decider(storeOf(entries...))
	d.Log = log
	return NewHandler(d, log), &logs
}

func TestAListedPodGetsTheAnnotationPatch(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api", container("main")))
	rec, got := serve(t, h, http.MethodPost, "/mutate", review(nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q", ct)
	}
	if got.APIVersion != "admission.k8s.io/v1" || got.Kind != "AdmissionReview" {
		t.Errorf("answer is %s %s", got.APIVersion, got.Kind)
	}
	if got.Response.UID != "705ab4f5-6393-11e8-b7cc-42010a800002" || !got.Response.Allowed {
		t.Errorf("response = %+v, want the request's uid and allowed", got.Response)
	}
	if got.Response.PatchType != "JSONPatch" || len(got.Response.Patch) == 0 {
		t.Fatalf("response = %+v, want a JSON Patch", got.Response)
	}

	// The pod, as the API server holds it, with the patch applied.
	var request struct {
		Request struct {
			Object map[string]any `json:"object"`
		} `json:"request"`
	}
	if err := json.Unmarshal(review(nil), &request); err != nil {
		t.Fatal(err)
	}
	pod := request.Request.Object
	before := clone(t, pod)
	applyPatch(t, pod, got.Response.Patch)
	annotations := pod["metadata"].(map[string]any)["annotations"].(map[string]any)
	for key, want := range map[string]string{
		"instrumentation.opentelemetry.io/inject-python":   instrumentation,
		"instrumentation.opentelemetry.io/container-names": "main",
		"impact.ai/auto-covered":                           marker,
		"team":                                             "payments", // what the pod had is still there
	} {
		if annotations[key] != want {
			t.Errorf("annotation %s = %v, want %q", key, annotations[key], want)
		}
	}
	// The marker is a label too: the uninstall finds the pods the cover pod annotated by it.
	labels := pod["metadata"].(map[string]any)["labels"].(map[string]any)
	if fmt.Sprint(labels) != "map[app:api impact.ai/auto-covered:true pod-template-hash:5d9c7b6f8d]" {
		t.Errorf("labels = %v, want the pod's own and the marker", labels)
	}
	for _, doc := range []map[string]any{before, pod} {
		delete(doc["metadata"].(map[string]any), "annotations")
		delete(doc["metadata"].(map[string]any), "labels")
	}
	if fmt.Sprint(before) != fmt.Sprint(pod) {
		t.Errorf("the patch changed more than the annotations and the marker label")
	}
}

func TestPodsThatAreNotCoveredAreAdmittedUntouched(t *testing.T) {
	cases := map[string]func(request map[string]any){
		"another workload": func(r map[string]any) {
			objectMeta(r)["ownerReferences"].([]any)[0].(map[string]any)["name"] = "other-5d9c7b6f8d"
		},
		"another namespace": func(r map[string]any) { r["namespace"] = "elsewhere"; objectMeta(r)["namespace"] = "elsewhere" },
		"an update":         func(r map[string]any) { r["operation"] = "UPDATE" },
		"a delete":          func(r map[string]any) { r["operation"] = "DELETE" },
		"a connect":         func(r map[string]any) { r["operation"] = "CONNECT" },
		"no operation":      func(r map[string]any) { delete(r, "operation") },
		"an opt-out label":  func(r map[string]any) { objectMeta(r)["labels"].(map[string]any)[OptOut] = "true" },
		"an opt-out annotation": func(r map[string]any) {
			objectMeta(r)["annotations"].(map[string]any)[OptOut] = "true"
		},
		"an inject annotation": func(r map[string]any) {
			objectMeta(r)["annotations"].(map[string]any)["instrumentation.opentelemetry.io/inject-java"] = "true"
		},
		"no owner": func(r map[string]any) { delete(objectMeta(r), "ownerReferences") },
		"the owner is not the controller": func(r map[string]any) {
			objectMeta(r)["ownerReferences"].([]any)[0].(map[string]any)["controller"] = false
		},
		"no hash label": func(r map[string]any) { delete(objectMeta(r)["labels"].(map[string]any), "pod-template-hash") },
		"no object":     func(r map[string]any) { delete(r, "object") },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			h, _ := handlerFor(entry("Deployment", "api"))
			rec, got := serve(t, h, http.MethodPost, "/mutate", review(mutate))
			if rec.Code != http.StatusOK || !got.Response.Allowed || got.Response.UID == "" {
				t.Fatalf("status %d, response %+v: the pod must be admitted", rec.Code, got.Response)
			}
			if got.Response.PatchType != "" || len(got.Response.Patch) != 0 {
				t.Errorf("a patch was sent for a pod that must be left alone: %+v", got.Response)
			}
		})
	}
}

func TestWithoutAListEverythingIsAdmittedUntouched(t *testing.T) {
	var logs bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&logs, nil))
	d := &Decider{Store: &Store{}, Instrumentation: instrumentation, Log: log}
	_, got := serve(t, NewHandler(d, log), http.MethodPost, "/mutate", review(nil))
	if !got.Response.Allowed || got.Response.PatchType != "" {
		t.Errorf("response = %+v", got.Response)
	}
}

func TestTheNamespaceComesFromTheRequestThenFromThePod(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api"))
	_, got := serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { r["namespace"] = "" }))
	if got.Response.PatchType != "JSONPatch" {
		t.Errorf("the pod's own namespace is used when the request has none: %+v", got.Response)
	}
	// The request's namespace wins over the pod's.
	_, got = serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { objectMeta(r)["namespace"] = "elsewhere" }))
	if got.Response.PatchType != "JSONPatch" {
		t.Errorf("the request's namespace is used first: %+v", got.Response)
	}
	_, got = serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) {
		r["namespace"] = ""
		objectMeta(r)["namespace"] = "elsewhere"
	}))
	if got.Response.PatchType != "" {
		t.Errorf("a pod of another namespace was covered: %+v", got.Response)
	}
}

func TestAPodWithoutAnnotationsGetsTheWholeObject(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api"))
	_, got := serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { delete(objectMeta(r), "annotations") }))
	var ops []Op
	if err := json.Unmarshal(got.Response.Patch, &ops); err != nil || len(ops) != 2 || ops[0].Path != "/metadata/annotations" ||
		ops[1].Path != "/metadata/labels/impact.ai~1auto-covered" {
		t.Errorf("patch = %s (%v): the annotations object, then the marker label", got.Response.Patch, err)
	}
	// "annotations": null is the same as none.
	_, got = serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { objectMeta(r)["annotations"] = nil }))
	if err := json.Unmarshal(got.Response.Patch, &ops); err != nil || len(ops) != 2 || ops[0].Path != "/metadata/annotations" {
		t.Errorf("patch for null annotations = %s (%v)", got.Response.Patch, err)
	}
}

func TestAPodWithoutLabelsGetsTheWholeLabelsObjectWithTheMarker(t *testing.T) {
	// A pod of a StatefulSet owns its name: nothing needs a label for the cover pod to find its workload.
	h, _ := handlerFor(entry("StatefulSet", "db"))
	statefulSet := func(r map[string]any) {
		meta := objectMeta(r)
		delete(meta, "labels")
		meta["ownerReferences"] = []any{map[string]any{"kind": "StatefulSet", "name": "db", "controller": true}}
	}
	_, got := serve(t, h, http.MethodPost, "/mutate", review(statefulSet))
	var ops []Op
	if err := json.Unmarshal(got.Response.Patch, &ops); err != nil || len(ops) == 0 {
		t.Fatalf("patch = %s (%v)", got.Response.Patch, err)
	}
	last := ops[len(ops)-1]
	if last.Path != "/metadata/labels" || fmt.Sprint(last.Value) != "map[impact.ai/auto-covered:true]" {
		t.Errorf("last operation = %+v, want the labels object with the marker", last)
	}
}

func TestOddValuesDoNotBreakTheRequest(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api"))
	odd := func(r map[string]any) {
		objectMeta(r)["labels"] = map[string]any{"pod-template-hash": "5d9c7b6f8d", "number": 5, "null": nil, "object": map[string]any{"a": "b"}, "list": []any{"x"}}
		objectMeta(r)["annotations"] = map[string]any{"number": 5, "null": nil, "object": map[string]any{"a": "b"}, "list": []any{"x"}, "team": "x"}
	}
	rec, got := serve(t, h, http.MethodPost, "/mutate", review(odd))
	if rec.Code != http.StatusOK || !got.Response.Allowed || got.Response.PatchType != "JSONPatch" {
		t.Errorf("status %d, response %+v", rec.Code, got.Response)
	}
	// Labels that are not an object at all.
	for _, labels := range []any{nil, "text", 5, []any{"a"}} {
		rec, got = serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { objectMeta(r)["labels"] = labels }))
		if rec.Code != http.StatusOK || !got.Response.Allowed || got.Response.PatchType != "" {
			t.Errorf("labels %v: status %d, response %+v (without the hash label the pod has no Deployment)", labels, rec.Code, got.Response)
		}
	}
}

func TestNothingButNamesAndTheKeysItReadsIsKept(t *testing.T) {
	var parsed Review
	if err := json.Unmarshal(review(func(r map[string]any) {
		objectMeta(r)["annotations"].(map[string]any)[OptOut] = "false"
	}), &parsed); err != nil {
		t.Fatal(err)
	}
	text := fmt.Sprintf("%+v", parsed)
	for _, leak := range []string{plantedKey, plantedArg, "OPENAI_API_KEY", "secretKeyRef", "registry.example.com", "payments", "last-applied", "replicaset-controller"} {
		if strings.Contains(text, leak) {
			// The names of unrelated annotations are kept (their count matters), but never their values.
			if leak == "last-applied" {
				continue
			}
			t.Errorf("the decoded review keeps %q: %s", leak, text)
		}
	}
	pod := parsed.Pod()
	if pod.Namespace != "shop" || pod.Labels["pod-template-hash"] != "5d9c7b6f8d" || pod.Annotations[OptOut] != "false" {
		t.Errorf("pod = %+v", pod)
	}
	if v, ok := pod.Annotations["team"]; !ok || v != "" {
		t.Errorf("an unrelated annotation must be present (the patch depends on it) with no value: %q, %v", v, ok)
	}
	if v, ok := pod.Annotations["kubectl.kubernetes.io/last-applied-configuration"]; !ok || v != "" {
		t.Errorf("last-applied-configuration kept its value: %q, %v", v, ok)
	}
	if len(pod.Owners) != 1 || pod.Owners[0] != (OwnerRef{Kind: "ReplicaSet", Name: "api-5d9c7b6f8d", Controller: true}) {
		t.Errorf("owners = %+v", pod.Owners)
	}
}

func TestNothingSecretIsLogged(t *testing.T) {
	h, logs := handlerFor(entry("Deployment", "api"))
	serve(t, h, http.MethodPost, "/mutate", review(nil))
	serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { r["operation"] = "UPDATE" }))
	serve(t, h, http.MethodPost, "/mutate", []byte("{"+plantedKey))
	for _, leak := range []string{plantedKey, plantedArg, "OPENAI_API_KEY", "payments", "last-applied"} {
		if strings.Contains(logs.String(), leak) {
			t.Errorf("the log contains %q:\n%s", leak, logs)
		}
	}
	if !strings.Contains(logs.String(), "shop/Deployment/api") {
		t.Errorf("the covered workload is not in the log:\n%s", logs)
	}
}

func TestADryRunIsAnsweredLikeAnyOtherRequestAndNotAnnounced(t *testing.T) {
	h, logs := handlerFor(entry("Deployment", "api"))
	_, got := serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { r["dryRun"] = true }))
	if got.Response.PatchType != "JSONPatch" {
		t.Errorf("a dry run is answered like the real request: %+v", got.Response)
	}
	if strings.Contains(logs.String(), `"level":"INFO"`) {
		t.Errorf("a dry run was announced as a covered pod:\n%s", logs)
	}
}

func TestBadRequestsAreRefusedPlainly(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api"))
	cases := []struct {
		name   string
		method string
		path   string
		body   []byte
		want   int
	}{
		{"not JSON", http.MethodPost, "/mutate", []byte("{"), http.StatusBadRequest},
		{"empty", http.MethodPost, "/mutate", nil, http.StatusBadRequest},
		{"an array", http.MethodPost, "/mutate", []byte("[]"), http.StatusBadRequest},
		{"no request", http.MethodPost, "/mutate", []byte(`{"apiVersion":"admission.k8s.io/v1","kind":"AdmissionReview"}`), http.StatusBadRequest},
		{"a GET", http.MethodGet, "/mutate", nil, http.StatusMethodNotAllowed},
		{"a PUT", http.MethodPut, "/mutate", review(nil), http.StatusMethodNotAllowed},
		{"another path", http.MethodPost, "/elsewhere", review(nil), http.StatusNotFound},
		{"the root", http.MethodPost, "/", review(nil), http.StatusNotFound},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec, _ := serve(t, h, tc.method, tc.path, tc.body)
			if rec.Code != tc.want {
				t.Errorf("status = %d, want %d (%s)", rec.Code, tc.want, rec.Body)
			}
			if tc.want == http.StatusMethodNotAllowed && rec.Header().Get("Allow") != http.MethodPost {
				t.Errorf("Allow = %q", rec.Header().Get("Allow"))
			}
		})
	}
}

func TestABodyOverEightMebibytesIsRefused(t *testing.T) {
	h, _ := handlerFor(entry("Deployment", "api"))
	pad := strings.Repeat("x", 8<<20)
	rec, _ := serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { r["padding"] = pad }))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413", rec.Code)
	}
	// A big pod below the limit is still read.
	rec, got := serve(t, h, http.MethodPost, "/mutate", review(func(r map[string]any) { r["padding"] = pad[:4<<20] }))
	if rec.Code != http.StatusOK || got.Response.PatchType != "JSONPatch" {
		t.Errorf("a 4 MiB request: status %d, response %+v", rec.Code, got.Response)
	}
}

func TestAHandlerWithoutADeciderStillAnswers(t *testing.T) {
	h := NewHandler(nil, nil)
	rec, got := serve(t, h, http.MethodPost, "/mutate", review(nil))
	if rec.Code != http.StatusOK || !got.Response.Allowed || got.Response.PatchType != "" {
		t.Errorf("status %d, response %+v", rec.Code, got.Response)
	}
}
