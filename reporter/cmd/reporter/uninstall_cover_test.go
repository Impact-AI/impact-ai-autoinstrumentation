// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubetest"
)

const (
	hookCoverDeployment = "impact-ai-cover"
	hookCoverScalePath  = "/apis/apps/v1/namespaces/impact-system/deployments/impact-ai-cover/scale"
	hookKeySecretPath   = "/api/v1/namespaces/impact-system/secrets/impact-ai-install-key"
)

func coverNoticeAPI(t *testing.T) *kubetest.Server {
	t.Helper()
	api := noticeAPI(t)
	api.HandleSequence(http.MethodPatch, hookCoverScalePath, kubetest.Response{Body: `{"kind":"Scale"}`})
	api.HandleSequence(http.MethodDelete, hookKeySecretPath, kubetest.Response{Body: `{"kind":"Status"}`})
	return api
}

func coverNoticeArgs(api *kubetest.Server, keyFile string, extra ...string) []string {
	return noticeArgs(api, keyFile, append([]string{
		"--cover-deployment=" + hookCoverDeployment, "--cover-pod-selector=app.kubernetes.io/name=cover",
		"--key-secret=impact-ai-install-key",
	}, extra...)...)
}

func TestUninstallNoticeStopsTheCoverPodSendsTheNoticeAndDeletesTheKey(t *testing.T) {
	api := coverNoticeAPI(t)
	clk := &clock{now: fixedNow}
	var stderr bytes.Buffer
	code := run(context.Background(), coverNoticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, &stderr, clockDeps(clk))
	if code != 0 {
		t.Fatalf("exit %d, stderr:\n%s", code, stderr.String())
	}

	var order []string
	for _, r := range api.Requests() {
		switch {
		case r.Method == http.MethodPatch && r.Path == hookScalePath:
			order = append(order, "scale reporter")
		case r.Method == http.MethodPatch && r.Path == hookCoverScalePath:
			order = append(order, "scale cover")
		case r.Method == http.MethodPatch:
			t.Errorf("a patch of %s: the hook patches the scale of two Deployments and nothing else", r.Path)
		case r.Method == http.MethodPost && r.Path == "/v1/inventory":
			order = append(order, "notice")
		case r.Method == http.MethodDelete:
			order = append(order, "delete "+r.Path)
		}
		if r.Path == "/api/v1/pods" {
			t.Errorf("%s %s: the hook reads no pod outside its own namespace", r.Method, r.Path)
		}
	}
	want := []string{"scale reporter", "scale cover", "notice", "delete " + hookKeySecretPath}
	if !reflect.DeepEqual(order, want) {
		t.Fatalf("order = %v, want %v", order, want)
	}
}

func TestUninstallNoticeWithoutTheNewFlagsDoesNoneOfIt(t *testing.T) {
	api := coverNoticeAPI(t)
	code := run(context.Background(), noticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, io.Discard, clockDeps(&clock{now: fixedNow}))
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, r := range api.Requests() {
		if r.Method == http.MethodDelete || r.Path == hookCoverScalePath || r.Path == "/api/v1/pods" {
			t.Errorf("unexpected %s %s", r.Method, r.Path)
		}
	}
}

func TestUninstallNoticeNeverBlocksTheUninstallWhenTheNewStepsFail(t *testing.T) {
	api := coverNoticeAPI(t)
	api.HandleStatus(hookKeySecretPath, http.StatusForbidden, `{}`)
	api.HandleStatus(hookCoverScalePath, http.StatusInternalServerError, `{}`)
	var stderr bytes.Buffer
	code := run(context.Background(), coverNoticeArgs(api, writeKeyFile(t)), testEnv(), io.Discard, &stderr, clockDeps(&clock{now: fixedNow}))
	if code != 0 {
		t.Fatalf("exit %d, stderr:\n%s", code, stderr.String())
	}
	if n := len(postedNotices(t, api)); n != 1 {
		t.Errorf("%d notices, want the final snapshot to go out anyway", n)
	}
}

func TestUninstallNoticeNewFlagUsageErrors(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want string
	}{
		// The strip moved into the uninstall script: a hook that still passes its flags is told so, not silently obeyed.
		{"the strip flag of an older chart", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--strip-pods"}, "strip-pods"},
		{"the strip timeout of an older chart", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--strip-timeout=45s"}, "strip-timeout"},
		{"a cover deployment but no namespace", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--cover-deployment=cover"}, "stop namespace"},
		{"a key secret but no namespace", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--key-secret=key"}, "stop namespace"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var stderr bytes.Buffer
			if code := run(context.Background(), tc.args, env(), io.Discard, &stderr, testDeps()); code != 2 || !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("exit %d, stderr %q; want 2 mentioning %q", code, stderr.String(), tc.want)
			}
		})
	}
}
