// SPDX-License-Identifier: Apache-2.0

package kube

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kubetest"
)

type obj = map[string]any

func newClient(t *testing.T, cfg Config) *Client {
	t.Helper()
	c, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListFollowsContinueTokens(t *testing.T) {
	srv := kubetest.New(t)
	var items []any
	for _, name := range []string{"a", "b", "c", "d", "e", "f", "g"} {
		items = append(items, map[string]any{"metadata": map[string]any{"name": name}})
	}
	srv.HandleList("/api/v1/namespaces", items, 3)

	c := newClient(t, Config{Host: srv.URL})
	var got []string
	err := List(context.Background(), c, "/api/v1/namespaces", 500, func(ns *Namespace) error {
		got = append(got, ns.Metadata.Name)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, "") != "abcdefg" {
		t.Errorf("items = %v, want a..g in order", got)
	}
	reqs := srv.RequestsFor("/api/v1/namespaces")
	if len(reqs) != 3 {
		t.Fatalf("made %d requests, want 3 pages", len(reqs))
	}
	if !strings.Contains(reqs[0].Query, "limit=500") {
		t.Errorf("first request %q must ask for limit=500", reqs[0].Query)
	}
	if !strings.Contains(reqs[1].Query, "continue=tok-3") || !strings.Contains(reqs[2].Query, "continue=tok-6") {
		t.Errorf("continue tokens not followed: %q, %q", reqs[1].Query, reqs[2].Query)
	}
}

func TestListSendsTheFieldSelectorOnEveryPage(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleList("/api/v1/pods", []any{obj{}, obj{}, obj{}}, 2)
	c := newClient(t, Config{Host: srv.URL})
	n := 0
	q := ListQuery{Limit: 500, FieldSelector: "status.phase!=Succeeded,status.phase!=Failed"}
	if err := ListWith(context.Background(), c, "/api/v1/pods", q, func(*Pod) error { n++; return nil }); err != nil {
		t.Fatal(err)
	}
	reqs := srv.RequestsFor("/api/v1/pods")
	if n != 3 || len(reqs) != 2 {
		t.Fatalf("items %d, requests %d", n, len(reqs))
	}
	for i, r := range reqs {
		values, err := url.ParseQuery(r.Query)
		if err != nil {
			t.Fatal(err)
		}
		if got := values.Get("fieldSelector"); got != q.FieldSelector {
			t.Errorf("page %d fieldSelector = %q, want %q", i+1, got, q.FieldSelector)
		}
	}
}

func TestListStopsWhenTheCallbackFails(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleList("/api/v1/pods", []any{map[string]any{}, map[string]any{}, map[string]any{}, map[string]any{}}, 2)
	c := newClient(t, Config{Host: srv.URL})
	boom := errors.New("boom")
	err := List(context.Background(), c, "/api/v1/pods", 500, func(*Pod) error { return boom })
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v, want the callback error", err)
	}
	if n := len(srv.RequestsFor("/api/v1/pods")); n != 1 {
		t.Errorf("made %d requests, want 1 (no further pages after a failure)", n)
	}
}

func TestBearerTokenIsReadFreshForEveryRequest(t *testing.T) {
	srv := kubetest.New(t)
	srv.Handle("/version", `{"gitVersion":"v1.34.11"}`)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("token-one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := newClient(t, Config{Host: srv.URL, TokenFile: tokenFile})
	var v VersionInfo
	if err := c.GetJSON(context.Background(), "/version", nil, &v); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("token-two"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := c.GetJSON(context.Background(), "/version", nil, &v); err != nil {
		t.Fatal(err)
	}
	reqs := srv.RequestsFor("/version")
	if len(reqs) != 2 || reqs[0].AuthHead != "Bearer token-one" || reqs[1].AuthHead != "Bearer token-two" {
		t.Errorf("auth headers = %+v, want the rotated token on the second request", reqs)
	}
	if v.GitVersion != "v1.34.11" {
		t.Errorf("gitVersion = %q", v.GitVersion)
	}
}

func TestErrorsCarryTheStatusButNotTheBody(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleStatus("/api/v1/nodes", http.StatusForbidden, `{"message":"nodes is forbidden: secret detail"}`)
	c := newClient(t, Config{Host: srv.URL})
	err := List(context.Background(), c, "/api/v1/nodes", 20, func(*Node) error { return nil })
	var se *StatusError
	if !errors.As(err, &se) || se.Code != http.StatusForbidden {
		t.Fatalf("err = %v, want a StatusError with code 403", err)
	}
	if strings.Contains(err.Error(), "secret detail") {
		t.Errorf("the error must not echo the response body: %v", err)
	}
	if !strings.Contains(err.Error(), "/api/v1/nodes") || !strings.Contains(err.Error(), "403") {
		t.Errorf("error %q should name the path and the status", err)
	}
}

func TestInClusterConfig(t *testing.T) {
	env := map[string]string{"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"}
	cfg, err := InClusterConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Host != "https://10.96.0.1:443" {
		t.Errorf("host = %q", cfg.Host)
	}
	if cfg.TokenFile != "/var/run/secrets/kubernetes.io/serviceaccount/token" || cfg.CAFile != "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt" {
		t.Errorf("paths = %q, %q", cfg.TokenFile, cfg.CAFile)
	}

	env["KUBERNETES_SERVICE_HOST"] = "fd00::1"
	cfg, err = InClusterConfig(func(k string) string { return env[k] })
	if err != nil || cfg.Host != "https://[fd00::1]:443" {
		t.Errorf("IPv6 host = %q, %v", cfg.Host, err)
	}

	if _, err := InClusterConfig(func(string) string { return "" }); err == nil {
		t.Error("outside a cluster InClusterConfig must fail")
	}
}

func TestTLSUsesTheGivenCA(t *testing.T) {
	srv := kubetest.NewTLS(t)
	srv.Handle("/version", `{"gitVersion":"v1.34.11"}`)
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := os.WriteFile(caFile, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}

	var v VersionInfo
	if err := newClient(t, Config{Host: srv.URL, CAFile: caFile}).GetJSON(context.Background(), "/version", nil, &v); err != nil {
		t.Fatalf("with the CA: %v", err)
	}
	var verr x509.UnknownAuthorityError
	err := newClient(t, Config{Host: srv.URL}).GetJSON(context.Background(), "/version", nil, &v)
	if err == nil || !errors.As(err, &verr) {
		t.Errorf("without the CA the certificate must be rejected, got %v", err)
	}
	if err := newClient(t, Config{Host: srv.URL, Insecure: true}).GetJSON(context.Background(), "/version", nil, &v); err != nil {
		t.Errorf("insecure: %v", err)
	}
	if _, err := New(Config{Host: srv.URL, CAFile: filepath.Join(t.TempDir(), "missing")}); err == nil {
		t.Error("a missing CA file must fail")
	}
}

func TestRequestTimeout(t *testing.T) {
	block := make(chan struct{})
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-block:
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(func() { close(block); slow.Close() })
	c := newClient(t, Config{Host: slow.URL, Timeout: 50 * time.Millisecond})
	start := time.Now()
	var v VersionInfo
	if err := c.GetJSON(context.Background(), "/version", nil, &v); err == nil {
		t.Fatal("expected a timeout")
	}
	if time.Since(start) > 5*time.Second {
		t.Errorf("took %v, the per-request timeout did not apply", time.Since(start))
	}
}

func TestContextCancellationStopsRequests(t *testing.T) {
	srv := kubetest.New(t)
	srv.Handle("/version", `{}`)
	c := newClient(t, Config{Host: srv.URL})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var v VersionInfo
	if err := c.GetJSON(ctx, "/version", nil, &v); !errors.Is(err, context.Canceled) {
		t.Errorf("err = %v, want context.Canceled", err)
	}
}

const scalePath = "/apis/apps/v1/namespaces/impact-system/deployments/reporter/scale"

func TestMergePatchSendsAJSONMergePatch(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Body: `{"kind":"Scale","spec":{"replicas":0}}`})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("hook-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := newClient(t, Config{Host: srv.URL, TokenFile: tokenFile})
	if err := c.MergePatch(context.Background(), scalePath, []byte(`{"spec":{"replicas":0}}`), nil); err != nil {
		t.Fatal(err)
	}
	reqs := srv.RequestsFor(scalePath)
	if len(reqs) != 1 {
		t.Fatalf("%d requests", len(reqs))
	}
	r := reqs[0]
	if r.Method != http.MethodPatch || r.ContentType != "application/merge-patch+json" || string(r.Body) != `{"spec":{"replicas":0}}` || r.AuthHead != "Bearer hook-token" {
		t.Errorf("request = %s %q %q %q", r.Method, r.ContentType, r.Body, r.AuthHead)
	}
}

func TestMergePatchDecodesTheAnswerWhenAskedTo(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Body: `{"spec":{"replicas":0},"status":{"replicas":1}}`})
	c := newClient(t, Config{Host: srv.URL})
	var scale struct {
		Spec struct {
			Replicas int `json:"replicas"`
		} `json:"spec"`
		Status struct {
			Replicas int `json:"replicas"`
		} `json:"status"`
	}
	if err := c.MergePatch(context.Background(), scalePath, []byte(`{"spec":{"replicas":0}}`), &scale); err != nil {
		t.Fatal(err)
	}
	if scale.Spec.Replicas != 0 || scale.Status.Replicas != 1 {
		t.Errorf("scale = %+v", scale)
	}
}

func TestMergePatchSurfacesTheStatusAndTheMethod(t *testing.T) {
	for _, code := range []int{http.StatusNotFound, http.StatusForbidden, http.StatusConflict} {
		srv := kubetest.New(t)
		srv.HandleSequence(http.MethodPatch, scalePath, kubetest.Response{Status: code, Body: `{"message":"secret detail"}`})
		err := newClient(t, Config{Host: srv.URL}).MergePatch(context.Background(), scalePath, []byte(`{}`), nil)
		var se *StatusError
		if !errors.As(err, &se) || se.Code != code {
			t.Fatalf("status %d: err = %v", code, err)
		}
		if !strings.Contains(err.Error(), "PATCH") || strings.Contains(err.Error(), "secret detail") {
			t.Errorf("status %d: error %q should name the method and not echo the body", code, err)
		}
	}
}

func TestListSendsTheLabelSelector(t *testing.T) {
	srv := kubetest.New(t)
	srv.HandleList("/api/v1/namespaces/impact-system/pods", []any{obj{}, obj{}, obj{}}, 2)
	c := newClient(t, Config{Host: srv.URL})
	q := ListQuery{Limit: 100, LabelSelector: "app.kubernetes.io/name=reporter,app.kubernetes.io/instance=impact-ai"}
	if err := ListWith(context.Background(), c, "/api/v1/namespaces/impact-system/pods", q, func(*Pod) error { return nil }); err != nil {
		t.Fatal(err)
	}
	for i, r := range srv.RequestsFor("/api/v1/namespaces/impact-system/pods") {
		values, _ := url.ParseQuery(r.Query)
		if values.Get("labelSelector") != q.LabelSelector {
			t.Errorf("page %d labelSelector = %q", i+1, values.Get("labelSelector"))
		}
	}
}
