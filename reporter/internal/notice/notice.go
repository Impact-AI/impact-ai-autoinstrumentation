// SPDX-License-Identifier: Apache-2.0

// Package notice implements the uninstall notice. A Helm pre-delete hook runs
// the reporter in this mode when the customer uninstalls: it stops the running
// reporter, waits until its pods are gone, and only then sends one final
// snapshot in mode "uninstalled". Waiting first means a late snapshot from the
// old reporter can never flip the cluster back to "reporting".
//
// Nothing here may block the uninstall: every failure is logged and the run
// ends normally. The notice holds the cluster identity and nothing else.
//
// The hook does two more things, each only when it is told to. It stops the
// cover pod too, so that no pod is marked while the release is removed. And
// after the final snapshot it deletes the Secret that holds the ingest key the
// install hook made, which Helm does not own.
//
// Its writes to the cluster are those: the scale of two Deployments of the
// kit, and one Secret. It patches no pod: the marks the cover pod put on pods
// are taken off by the uninstall script, under the operator's own rights, so
// that no role of the kit ever holds a write on pods.
package notice

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/inventory"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kube"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kubesecret"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/sender"
)

const (
	pollInterval    = time.Second
	defaultTimeout  = 60 * time.Second
	buildAttempts   = 3
	buildRetryPause = time.Second
	// A request still hanging this long after the deadline is cut off.
	backstopSlack = 5 * time.Second
	// Pods that have already exited cannot send anything, so they are not waited
	// for. Terminating pods keep their phase until their containers have exited.
	livePodsOnly = "status.phase!=Succeeded,status.phase!=Failed"
)

// Builder builds the notice snapshot with the given sequence.
type Builder interface {
	UninstallNotice(ctx context.Context, sequence int64) (*inventory.Snapshot, error)
}

// Options say what to stop and how long to wait.
type Options struct {
	// StopDeployment is scaled to zero. Empty means nothing is scaled.
	StopDeployment string
	// StopNamespace is the namespace of the Deployment and of its pods.
	StopNamespace string
	// PodSelector is the label selector of the old reporter's pods. Empty means
	// there is nothing to wait for.
	PodSelector string
	// StopTimeout bounds the wait for the pods; NoticeTimeout bounds building and
	// sending the notice, retries included. Zero means 60 seconds.
	StopTimeout   time.Duration
	NoticeTimeout time.Duration
	// SelfUID is the UID of the pod running the hook, which is never waited for
	// even if the selector happens to match it.
	SelfUID string

	// CoverDeployment is the cover pod's Deployment. Like the reporter's it is
	// scaled to zero and its pods (CoverPodSelector, in StopNamespace) are
	// waited for, so that no pod is marked once the uninstall is under way.
	// Empty: there is no cover pod.
	CoverDeployment  string
	CoverPodSelector string
	// KeySecret is deleted from StopNamespace after the notice was sent (or
	// failed to be): Helm does not own it, the install hook made it. Empty: the
	// Secret is Helm's, or the customer's, and stays.
	KeySecret string
}

// SecretDeleter deletes a Secret; kubesecret.Store is one.
type SecretDeleter interface {
	Delete(ctx context.Context, namespace, name string) error
}

// ScaleOutcome says what happened to the Deployment.
type ScaleOutcome int

const (
	ScaleSkipped ScaleOutcome = iota // no Deployment was named
	Scaled
	AlreadyGone // the Deployment does not exist
	ScaleFailed
)

func (o ScaleOutcome) String() string {
	return [...]string{"skipped", "scaled", "already-gone", "failed"}[o]
}

// WaitOutcome says how the wait for the pods ended.
type WaitOutcome int

const (
	WaitSkipped  WaitOutcome = iota // no selector was given
	PodsGone                        // no reporter pod is left
	WaitTimedOut                    // pods were still there when the time ran out
	WaitFailed                      // the pods cannot be listed (permission or selector)
	WaitCanceled                    // the context ended
)

func (o WaitOutcome) String() string {
	return [...]string{"skipped", "pods-gone", "timed-out", "failed", "canceled"}[o]
}

// Result describes a run, for logs and tests. The exit code never depends on it.
type Result struct {
	Scale ScaleOutcome
	Wait  WaitOutcome
	Polls int
	// Cover and CoverWait are the same for the cover pod; KeyDeleted says that the key Secret is gone.
	Cover      ScaleOutcome
	CoverWait  WaitOutcome
	KeyDeleted bool
	Built      bool
	Sequence   int64
	// Attempted is true when a send was tried; Sent is its outcome.
	Attempted bool
	Sent      sender.Result
}

// Runner performs the uninstall notice.
type Runner struct {
	Kube    *kube.Client
	Builder Builder
	// Ingest is the configured sender. The runner sets its interval, clock and
	// sleeper so that the notice budget covers every retry.
	Ingest  sender.Sender
	KeyFile string
	Options Options
	// Secrets deletes Options.KeySecret. Nil with a KeySecret named is logged and skipped.
	Secrets SecretDeleter

	// Test seams. Nil means the real clock and sleep.
	Now   func() time.Time
	Sleep func(ctx context.Context, d time.Duration) error
	Log   *slog.Logger
}

func (r *Runner) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Runner) sleep(ctx context.Context, d time.Duration) error {
	if r.Sleep != nil {
		return r.Sleep(ctx, d)
	}
	if d <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (r *Runner) log() *slog.Logger {
	if r.Log != nil {
		return r.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func orDefault(d time.Duration) time.Duration {
	if d <= 0 {
		return defaultTimeout
	}
	return d
}

// Run stops the reporter (and the cover pod), waits for their pods, sends the
// notice and deletes the key Secret. It does not return an error: whatever
// fails is logged and the run goes on, because the uninstall must never be
// blocked.
func (r *Runner) Run(ctx context.Context) Result {
	log := r.log()
	o := r.Options
	var res Result
	log.Info("uninstall notice starting", "namespace", o.StopNamespace, "deployment", o.StopDeployment,
		"stopTimeout", orDefault(o.StopTimeout).String(), "noticeTimeout", orDefault(o.NoticeTimeout).String())

	if o.StopDeployment != "" {
		res.Scale = r.scale(ctx, o.StopDeployment)
	}
	if o.CoverDeployment != "" {
		res.Cover = r.scale(ctx, o.CoverDeployment)
	}
	switch {
	case o.PodSelector != "":
		res.Wait, res.Polls = r.waitForPods(ctx, o.PodSelector)
	case o.StopDeployment != "":
		log.Warn("no pod selector was given, so the run does not wait for the reporter pods to exit")
	}
	if o.CoverPodSelector != "" {
		res.CoverWait, _ = r.waitForPods(ctx, o.CoverPodSelector)
	}
	if ctx.Err() != nil {
		log.Warn("stopped before the notice could be sent", "error", ctx.Err())
		r.finish(ctx, &res)
		return res
	}
	r.sendNotice(ctx, &res)
	r.finish(ctx, &res)
	return res
}

// finish deletes the key Secret. It is the last thing the hook does: the notice has been tried, and the key is no
// longer needed. A canceled hook still tries, for a few seconds.
func (r *Runner) finish(ctx context.Context, res *Result) {
	o := r.Options
	if o.KeySecret == "" {
		return
	}
	log := r.log()
	if r.Secrets == nil {
		log.Warn("no way to delete the key Secret; it stays", "secret", o.KeySecret)
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
	defer cancel()
	switch err := r.Secrets.Delete(ctx, o.StopNamespace, o.KeySecret); {
	case err == nil:
		log.Info("deleted the key Secret", "secret", o.KeySecret)
		res.KeyDeleted = true
	case errors.Is(err, kubesecret.ErrNotFound):
		log.Info("the key Secret is already gone", "secret", o.KeySecret)
		res.KeyDeleted = true
	default:
		log.Warn("could not delete the key Secret; it stays until the namespace is deleted", "secret", o.KeySecret, "error", err)
	}
}

// scale sets the replicas of a Deployment of the kit to zero.
func (r *Runner) scale(ctx context.Context, deployment string) ScaleOutcome {
	log := r.log()
	o := r.Options
	path := fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments/%s/scale", url.PathEscape(o.StopNamespace), url.PathEscape(deployment))
	log.Info("scaling a deployment of the kit to zero", "namespace", o.StopNamespace, "deployment", deployment)
	err := r.Kube.MergePatch(ctx, path, []byte(`{"spec":{"replicas":0}}`), nil)
	var status *kube.StatusError
	switch {
	case err == nil:
		return Scaled
	case errors.As(err, &status) && status.Code == 404:
		log.Info("the deployment does not exist; nothing to stop", "namespace", o.StopNamespace, "deployment", deployment)
		return AlreadyGone
	default:
		log.Warn("could not scale the deployment to zero; continuing", "deployment", deployment, "error", err)
		return ScaleFailed
	}
}

// podsRemaining counts the pods of a selector that have not exited, other than
// the pod running this hook.
func (r *Runner) podsRemaining(ctx context.Context, selector string) (int, error) {
	o := r.Options
	errFound := errors.New("found a pod")
	path := "/api/v1/namespaces/" + url.PathEscape(o.StopNamespace) + "/pods"
	query := kube.ListQuery{Limit: 100, LabelSelector: selector, FieldSelector: livePodsOnly}
	count := 0
	err := kube.ListWith(ctx, r.Kube, path, query, func(p *kube.Pod) error {
		if o.SelfUID != "" && p.Metadata.UID == o.SelfUID {
			return nil
		}
		count++
		return errFound // one is enough to know the wait is not over
	})
	if errors.Is(err, errFound) {
		err = nil
	}
	return count, err
}

// waitForPods looks at the pods of a selector every second until none is left or
// the stop timeout passes.
func (r *Runner) waitForPods(ctx context.Context, selector string) (WaitOutcome, int) {
	log := r.log()
	timeout := orDefault(r.Options.StopTimeout)
	deadline := r.now().Add(timeout)
	ctx, cancel := context.WithTimeout(ctx, timeout+backstopSlack)
	defer cancel()
	polls := 0
	for {
		polls++
		n, err := r.podsRemaining(ctx, selector)
		var status *kube.StatusError
		switch {
		case err == nil && n == 0:
			log.Info("the old reporter's pods are gone", "polls", polls)
			return PodsGone, polls
		case err == nil:
			log.Debug("the old reporter is still running", "pods", n)
		case errors.As(err, &status) && status.Code == 404:
			log.Info("the namespace is gone, so the old reporter's pods are too")
			return PodsGone, polls
		case errors.As(err, &status) && (status.Code == 400 || status.Code == 401 || status.Code == 403):
			log.Warn("cannot list the old reporter's pods; not waiting for them", "error", err)
			return WaitFailed, polls
		case ctx.Err() != nil:
			return WaitCanceled, polls
		default:
			log.Warn("could not list the old reporter's pods; will look again", "error", err)
		}
		now := r.now()
		if !now.Before(deadline) {
			log.Warn("timed out waiting for the old reporter's pods; sending the notice anyway", "timeout", timeout.String(), "polls", polls)
			return WaitTimedOut, polls
		}
		if err := r.sleep(ctx, min(pollInterval, deadline.Sub(now))); err != nil {
			if ctx.Err() != nil {
				return WaitCanceled, polls
			}
			return WaitTimedOut, polls
		}
	}
}

func readKey(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// budgetSleep sleeps like the runner does but never past the deadline of the
// notice budget: the last pause ends at the deadline and reports it.
func (r *Runner) budgetSleep(deadline time.Time) func(ctx context.Context, d time.Duration) error {
	return func(ctx context.Context, d time.Duration) error {
		remaining := deadline.Sub(r.now())
		if remaining <= 0 {
			return context.DeadlineExceeded
		}
		if d >= remaining {
			_ = r.sleep(ctx, remaining)
			return context.DeadlineExceeded
		}
		return r.sleep(ctx, d)
	}
}

// build reads the cluster and builds the notice. The sequence is the Unix time
// at that moment, which is above any sequence of a reporter that has been
// stopped: those start at the time they started and add one per interval, and
// an interval is at least five seconds.
func (r *Runner) build(ctx context.Context, sleep func(context.Context, time.Duration) error) (*inventory.Snapshot, error) {
	log := r.log()
	for attempt := 1; ; attempt++ {
		snap, err := r.Builder.UninstallNotice(ctx, r.now().Unix())
		if err == nil {
			return snap, nil
		}
		if attempt >= buildAttempts || ctx.Err() != nil {
			return nil, err
		}
		log.Warn("could not read the cluster for the uninstall notice; will retry", "attempt", attempt, "error", err)
		if serr := sleep(ctx, buildRetryPause); serr != nil {
			return nil, err
		}
	}
}

// sendNotice builds and sends the notice within the notice budget.
func (r *Runner) sendNotice(ctx context.Context, res *Result) {
	log := r.log()
	key, err := readKey(r.KeyFile)
	if err != nil || key == "" {
		log.Error("no ingest key; the uninstall notice is not sent", "file", r.KeyFile, "readError", err != nil)
		return
	}

	budget := orDefault(r.Options.NoticeTimeout)
	deadline := r.now().Add(budget)
	ctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	sleep := r.budgetSleep(deadline)

	snap, err := r.build(ctx, sleep)
	if err != nil {
		log.Error("could not build the uninstall notice; it is not sent", "error", err)
		return
	}
	res.Built, res.Sequence = true, snap.Sequence
	enc, err := inventory.Encode(snap)
	if err != nil {
		log.Error("could not encode the uninstall notice; it is not sent", "error", err)
		return
	}

	s := r.Ingest
	s.Interval = budget // a Retry-After beyond the budget ends the send at once
	s.Now = r.now
	s.Sleep = sleep
	if s.Log == nil {
		s.Log = log
	}
	res.Attempted = true
	res.Sent = s.Send(ctx, key, enc.Body)
	attrs := []any{"sequence", snap.Sequence, "status", res.Sent.Status, "attempts", res.Sent.Attempts, "outcome", res.Sent.Outcome.String()}
	if res.Sent.Outcome == sender.Accepted {
		log.Info("uninstall notice sent", attrs...)
		return
	}
	if res.Sent.Err != nil {
		attrs = append(attrs, "error", res.Sent.Err)
	}
	log.Warn("uninstall notice not delivered", attrs...)
}
