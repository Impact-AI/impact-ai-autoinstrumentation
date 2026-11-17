// SPDX-License-Identifier: Apache-2.0

package kubesecret

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kube"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kubetest"
)

const secretPath = "/api/v1/namespaces/impact-ai/secrets/impact-ai-install-key"

func store(t *testing.T, api *kubetest.Server) *Store {
	t.Helper()
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := New(kube.Config{Host: api.URL, TokenFile: token, UserAgent: "test"})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

func TestGetDecodesTheAnnotationsAndTheData(t *testing.T) {
	api := kubetest.New(t)
	api.Handle(secretPath, map[string]any{
		"metadata": map[string]any{"name": "impact-ai-install-key", "annotations": map[string]string{"impact.ai/exchange": "pending"}},
		"data":     map[string]string{"pending-key": b64("the-key"), "other": b64("x")},
	})
	got, err := store(t, api).Get(context.Background(), "impact-ai", "impact-ai-install-key")
	if err != nil {
		t.Fatal(err)
	}
	if got.Annotations["impact.ai/exchange"] != "pending" || string(got.Data["pending-key"]) != "the-key" {
		t.Errorf("secret = %+v", got)
	}
	req := api.Requests()[0]
	if req.Method != http.MethodGet || req.AuthHead != "Bearer sa-token" {
		t.Errorf("request = %s, auth %q", req.Method, req.AuthHead)
	}
}

func TestGetOfAMissingSecretIsErrNotFound(t *testing.T) {
	api := kubetest.New(t)
	api.HandleStatus(secretPath, http.StatusNotFound, `{"kind":"Status","code":404}`)
	if _, err := store(t, api).Get(context.Background(), "impact-ai", "impact-ai-install-key"); !errors.Is(err, ErrNotFound) {
		t.Errorf("err = %v, want ErrNotFound", err)
	}
}

func TestThereIsNoWayToCreateASecret(t *testing.T) {
	// The install hook's role holds no create, and the package has none: a POST would be refused by the API server, and
	// a method that sends one would have no honest caller.
	src, err := os.ReadFile("kubesecret.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"http.MethodPost", "ErrExists", ") Create("} {
		if strings.Contains(string(src), forbidden) {
			t.Errorf("kubesecret.go mentions %s: the reporter reads, patches and deletes a Secret, it does not create one", forbidden)
		}
	}
}

func TestPatchIsAMergePatchThatSetsAndRemoves(t *testing.T) {
	api := kubetest.New(t)
	api.HandleSequence(http.MethodPatch, secretPath, kubetest.Response{Body: `{}`})
	err := store(t, api).Patch(context.Background(), "impact-ai", "impact-ai-install-key", Patch{
		Annotations: map[string]*string{"impact.ai/exchange": ptr("exchanged"), "impact.ai/pending-token": nil},
		Data:        map[string]*[]byte{"ingest-key": ptrBytes("the-key"), "pending-key": nil},
	})
	if err != nil {
		t.Fatal(err)
	}
	req := api.Requests()[0]
	if req.Method != http.MethodPatch || req.ContentType != "application/merge-patch+json" {
		t.Errorf("request = %s %q", req.Method, req.ContentType)
	}
	var sent map[string]map[string]any
	if err := json.Unmarshal(req.Body, &sent); err != nil {
		t.Fatal(err)
	}
	annotations := sent["metadata"]["annotations"].(map[string]any)
	if annotations["impact.ai/exchange"] != "exchanged" || annotations["impact.ai/pending-token"] != nil {
		t.Errorf("annotations = %v", annotations)
	}
	if _, present := annotations["impact.ai/pending-token"]; !present {
		t.Errorf("a removed annotation must be sent as null: %s", req.Body)
	}
	data := sent["data"]
	if data["ingest-key"] != b64("the-key") {
		t.Errorf("data = %v", data)
	}
	if v, present := data["pending-key"]; !present || v != nil {
		t.Errorf("a removed key must be sent as null: %s", req.Body)
	}
}

func TestPatchOfAMissingSecretIsErrNotFound(t *testing.T) {
	api := kubetest.New(t)
	api.HandleSequence(http.MethodPatch, secretPath, kubetest.Response{Status: http.StatusNotFound, Body: `{}`})
	err := store(t, api).Patch(context.Background(), "impact-ai", "impact-ai-install-key", Patch{})
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("err = %v, want ErrNotFound", err)
	}
}

func TestDeleteDeletesTheNamedSecretOnly(t *testing.T) {
	api := kubetest.New(t)
	api.HandleSequence(http.MethodDelete, secretPath, kubetest.Response{Body: `{"kind":"Status"}`})
	if err := store(t, api).Delete(context.Background(), "impact-ai", "impact-ai-install-key"); err != nil {
		t.Fatal(err)
	}
	reqs := api.Requests()
	if len(reqs) != 1 || reqs[0].Method != http.MethodDelete || reqs[0].Path != secretPath {
		t.Errorf("requests = %+v", reqs)
	}

	api2 := kubetest.New(t)
	api2.HandleSequence(http.MethodDelete, secretPath, kubetest.Response{Status: http.StatusNotFound, Body: `{}`})
	if err := store(t, api2).Delete(context.Background(), "impact-ai", "impact-ai-install-key"); !errors.Is(err, ErrNotFound) {
		t.Errorf("deleting a missing Secret: err = %v, want ErrNotFound", err)
	}
}

func TestOtherStatusesAreStatusErrorsThatNameNoData(t *testing.T) {
	api := kubetest.New(t)
	api.HandleStatus(secretPath, http.StatusForbidden, `{"message":"secrets \"x\" is forbidden","details":{"data":"`+b64("leaked")+`"}}`)
	_, err := store(t, api).Get(context.Background(), "impact-ai", "impact-ai-install-key")
	var status *kube.StatusError
	if !errors.As(err, &status) || status.Code != http.StatusForbidden {
		t.Fatalf("err = %v, want a *kube.StatusError 403", err)
	}
	if strings.Contains(err.Error(), "leaked") || strings.Contains(err.Error(), b64("leaked")) {
		t.Errorf("the error carries the answer's body: %v", err)
	}
}

func TestNamesThatCouldChangeThePathAreRefused(t *testing.T) {
	api := kubetest.New(t)
	s := store(t, api)
	for _, bad := range [][2]string{{"impact-ai", "../x"}, {"impact-ai", "a/b"}, {"a/b", "x"}, {"", "x"}, {"impact-ai", ""}, {"impact-ai", "X"}, {"Impact", "x"}} {
		if _, err := s.Get(context.Background(), bad[0], bad[1]); err == nil {
			t.Errorf("Get(%q, %q) was sent", bad[0], bad[1])
		}
		if err := s.Delete(context.Background(), bad[0], bad[1]); err == nil {
			t.Errorf("Delete(%q, %q) was sent", bad[0], bad[1])
		}
	}
	if n := len(api.Requests()); n != 0 {
		t.Errorf("%d requests reached the API server", n)
	}
}

func ptr(s string) *string { return &s }

func ptrBytes(s string) *[]byte { b := []byte(s); return &b }
