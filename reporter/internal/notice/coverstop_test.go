// SPDX-License-Identifier: Apache-2.0

package notice

import (
	"context"
	"errors"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kube"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubesecret"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubetest"
)

// The uninstall of a kit with automatic coverage: the cover pod is stopped beside the reporter, and the Secret the install
// hook made for the ingest key is deleted once the notice has been tried. The pods the cover pod marked are not touched
// here: the uninstall script takes their marks off, under the operator's own rights (scripts/uninstall-cluster-kit.sh).

const (
	coverDeploy   = "impact-cluster-kit-cover"
	coverSelector = "app.kubernetes.io/name=cover,app.kubernetes.io/instance=impact-cluster-kit"
	coverScale    = "/apis/apps/v1/namespaces/impact-system/deployments/impact-cluster-kit-cover/scale"
	allPods       = "/api/v1/pods"
	keySecret     = "impact-cluster-kit-install-key"
	keySecretPath = "/api/v1/namespaces/impact-system/secrets/impact-cluster-kit-install-key"
)

// fakeSecrets records the Secrets deleted.
type fakeSecrets struct {
	mu      sync.Mutex
	deleted []string
	err     error
}

func (f *fakeSecrets) Delete(_ context.Context, namespace, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleted = append(f.deleted, namespace+"/"+name)
	return f.err
}

// coverRig is the rig with the uninstall of a kit that has a cover pod and a key Secret of its own.
func coverRig(t *testing.T) (*rig, *fakeSecrets) {
	t.Helper()
	r := newRig(t)
	r.srv.HandleSequence(http.MethodPatch, coverScale, kubetest.Response{Body: `{"kind":"Scale"}`})
	secrets := &fakeSecrets{}
	r.runner.Secrets = secrets
	r.runner.Options.CoverDeployment = coverDeploy
	r.runner.Options.CoverPodSelector = coverSelector
	r.runner.Options.KeySecret = keySecret
	return r, secrets
}

func TestStopsTheCoverPodWaitsForItSendsTheNoticeThenDeletesTheKey(t *testing.T) {
	r, secrets := coverRig(t)
	res := r.runner.Run(context.Background())

	var order []string
	for _, req := range r.srv.Requests() {
		switch {
		case req.Method == http.MethodPatch && req.Path == scalePath:
			order = append(order, "scale reporter")
		case req.Method == http.MethodPatch && req.Path == coverScale:
			order = append(order, "scale cover")
		case req.Method == http.MethodGet && req.Path == podsPath:
			order = append(order, "wait pods")
		case req.Method == http.MethodPost && req.Path == ingest:
			order = append(order, "notice")
		}
	}
	// de-duplicate consecutive repeats of the polling steps
	var compact []string
	for _, step := range order {
		if len(compact) == 0 || compact[len(compact)-1] != step {
			compact = append(compact, step)
		}
	}
	want := []string{"scale reporter", "scale cover", "wait pods", "notice"}
	if !reflect.DeepEqual(compact, want) {
		t.Fatalf("order = %v\nwant   %v", compact, want)
	}
	if !reflect.DeepEqual(secrets.deleted, []string{"impact-system/" + keySecret}) {
		t.Errorf("deleted = %v", secrets.deleted)
	}
	if res.Cover != Scaled || !res.KeyDeleted {
		t.Errorf("result = %+v", res)
	}
}

// The hook's writes to the cluster are the scale of the kit's two Deployments and the notice (the key Secret goes through
// the Secret client, which has its own tests): no pod is patched one by one, and no pod outside the release namespace
// is read. Its permission on pods is for reading; the uninstall script takes the marks off the pods, under the
// operator's own rights.
func TestTheUninstallWritesNothingToAPodAndReadsNoneOutsideItsNamespace(t *testing.T) {
	r, _ := coverRig(t)
	r.runner.Run(context.Background())

	writes := map[string]bool{}
	for _, req := range r.srv.Requests() {
		if req.Method != http.MethodGet {
			writes[req.Method+" "+req.Path] = true
		}
		if strings.Contains(req.Path, "/pods/") || req.Path == allPods {
			t.Errorf("%s %s: the uninstall hook does not touch pods one by one", req.Method, req.Path)
		}
	}
	want := map[string]bool{"PATCH " + scalePath: true, "PATCH " + coverScale: true, "POST " + ingest: true}
	if !reflect.DeepEqual(writes, want) {
		t.Errorf("writes = %v, want %v", writes, want)
	}
}

func TestTheCoverPodsOwnPodsAreWaitedForByTheirSelector(t *testing.T) {
	r, _ := coverRig(t)
	r.runner.Options.CoverPodSelector = "app.kubernetes.io/name=cover"
	r.runner.Run(context.Background())
	var selectors []string
	for _, req := range r.srv.Requests() {
		if req.Method == http.MethodGet && req.Path == podsPath && strings.Contains(req.Query, "name%3Dcover") {
			selectors = append(selectors, req.Query)
		}
	}
	if len(selectors) == 0 {
		t.Errorf("the cover pods were never looked for: %v", r.srv.Requests())
	}
}

func TestTheKeyIsDeletedEvenWhenTheNoticeFailsAndNotBefore(t *testing.T) {
	r, secrets := coverRig(t)
	r.srv.HandleSequence(http.MethodPost, ingest, kubetest.Response{Status: http.StatusInternalServerError})
	res := r.runner.Run(context.Background())
	if res.Sent.Outcome.String() == "accepted" {
		t.Fatalf("the notice should have failed: %+v", res.Sent)
	}
	if !reflect.DeepEqual(secrets.deleted, []string{"impact-system/" + keySecret}) {
		t.Errorf("deleted = %v: the key goes whether or not the notice was delivered", secrets.deleted)
	}
}

func TestTheKeyIsReadForTheNoticeBeforeItIsDeleted(t *testing.T) {
	// The notice authenticates with the mounted key; the Secret is only deleted once the notice has gone out.
	r, secrets := coverRig(t)
	r.runner.Secrets = &orderSecrets{inner: secrets, rig: r}
	r.runner.Run(context.Background())
	seenNotice := false
	for _, req := range r.srv.Requests() {
		if req.Method == http.MethodPost && req.Path == ingest {
			seenNotice = true
		}
	}
	if !seenNotice {
		t.Fatal("no notice")
	}
}

type orderSecrets struct {
	inner *fakeSecrets
	rig   *rig
}

func (o *orderSecrets) Delete(ctx context.Context, namespace, name string) error {
	for _, req := range o.rig.srv.Requests() {
		if req.Method == http.MethodPost && req.Path == ingest {
			return o.inner.Delete(ctx, namespace, name)
		}
	}
	o.rig.t.Error("the key Secret was deleted before the notice was sent")
	return o.inner.Delete(ctx, namespace, name)
}

func TestAMissingOrUndeletableKeySecretNeverBlocksTheUninstall(t *testing.T) {
	for name, err := range map[string]error{"missing": kubesecret.ErrNotFound, "forbidden": &kube.StatusError{Code: 403, Path: keySecretPath, Method: "DELETE"}, "network": errors.New("connection refused")} {
		t.Run(name, func(t *testing.T) {
			r, secrets := coverRig(t)
			secrets.err = err
			res := r.runner.Run(context.Background())
			if !res.Attempted {
				t.Errorf("the notice did not go out: %+v", res)
			}
			if name == "missing" && !res.KeyDeleted {
				t.Errorf("a Secret that is already gone counts as deleted")
			}
			if name != "missing" && res.KeyDeleted {
				t.Errorf("a failed delete counted as done")
			}
		})
	}
}

func TestNothingNewHappensUnlessAskedFor(t *testing.T) {
	r := newRig(t) // no cover pod, no key Secret
	r.runner.Secrets = &fakeSecrets{}
	r.runner.Run(context.Background())
	for _, req := range r.srv.Requests() {
		if req.Path == allPods || strings.Contains(req.Path, "/secrets") || req.Path == coverScale {
			t.Errorf("unexpected %s %s", req.Method, req.Path)
		}
	}
	if len(r.runner.Secrets.(*fakeSecrets).deleted) != 0 {
		t.Errorf("a Secret was deleted without being named")
	}
}
