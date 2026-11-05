// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"strings"
	"testing"
)

// The stall signal: for the workloads of a cluster with automatic coverage, the reason of the Deployment's ReplicaFailure
// condition and the waiting reasons of the init container the Operator injected. Names only: the message of a condition
// or of a waiting container can hold what a policy or a registry wrote about a pod, and never leaves the cluster.

const (
	injectPython = "opentelemetry-auto-instrumentation-python"
	injectNode   = "opentelemetry-auto-instrumentation-nodejs"
	// planted is text that must not appear in a snapshot, in the shape of what a message can carry.
	planted = "pods \"api-5d9c7b6f8d-\" is forbidden: image mirror.example.com/team/api@sha256:abc not allowed, token " + secretText
)

var reasonGrammar = regexp.MustCompile(`^[A-Z][A-Za-z0-9]{1,62}$`)

func condition(kind, status, reason, message string) obj {
	return obj{"type": kind, "status": status, "reason": reason, "message": message,
		"lastUpdateTime": "2026-11-03T11:00:00Z", "lastTransitionTime": "2026-11-03T11:00:00Z"}
}

func withConditions(workload obj, conditions ...obj) obj {
	list := make([]any, len(conditions))
	for i, c := range conditions {
		list[i] = c
	}
	workload["status"].(obj)["conditions"] = list
	return workload
}

func initState(name string, state obj) podOpt {
	return func(p obj) {
		status := p["status"].(obj)
		list, _ := status["initContainerStatuses"].([]any)
		status["initContainerStatuses"] = append(list, obj{"name": name, "ready": false, "restartCount": 0,
			"image": "ghcr.io/example/x:1", "imageID": "", "state": state})
	}
}

func initWaiting(name, reason string) podOpt {
	return initState(name, obj{"waiting": obj{"reason": reason, "message": planted}})
}

func app() obj { return template(nil, ctr("app", env("OPENAI_API_KEY", secretText))) }

// stalled builds a cluster around one Deployment named api and returns the snapshot of it, with the cover fields on.
func stalledSnapshot(t *testing.T, opts Options, deployments []any, pods ...any) *Snapshot {
	t.Helper()
	f := newFake(t)
	f.list(pathDeployments, deployments...)
	f.list(pathPods, pods...)
	opts.CoverFields = true
	return f.snapshot(opts)
}

func apiPods(opts ...[]podOpt) []any {
	var pods []any
	for i, o := range opts {
		pods = append(pods, replicaSetPod("shop", "api", "5d9c7b6f8d", fmt.Sprintf("api-%d", i+1), app(), o...))
	}
	return pods
}

func stallOf(t *testing.T, s *Snapshot, name string) *Stall {
	t.Helper()
	return findWorkload(t, s, "shop", "Deployment", name).Stall
}

func TestAReplicaFailureOfADeploymentIsReportedByItsReasonAlone(t *testing.T) {
	failing := withConditions(deployment("shop", "api", 2, 1, app()),
		condition("Available", "False", "MinimumReplicasUnavailable", "Deployment does not have minimum availability."),
		condition("ReplicaFailure", "True", "FailedCreate", planted))
	s := stalledSnapshot(t, Options{}, []any{failing, deployment("shop", "quiet", 1, 1, app())})
	if got := stallOf(t, s, "api"); got == nil || got.ReplicaFailure != "FailedCreate" || len(got.InitWaiting) != 0 {
		t.Fatalf("stall = %+v, want the reason FailedCreate alone", got)
	}
	if stallOf(t, s, "quiet") != nil {
		t.Errorf("a Deployment with no failure has a stall: %+v", stallOf(t, s, "quiet"))
	}
	if blob := mustJSON(t, s); strings.Contains(blob, "forbidden") || strings.Contains(blob, secretText) || strings.Contains(blob, "mirror.example.com") {
		t.Errorf("the message of the condition left the cluster: %s", blob)
	}
}

func TestOnlyAReplicaFailureThatIsTrueCounts(t *testing.T) {
	for name, conditions := range map[string][]obj{
		"a failure that has passed": {condition("ReplicaFailure", "False", "FailedCreate", planted)},
		"an unknown failure":        {condition("ReplicaFailure", "Unknown", "FailedCreate", "")},
		"other conditions":          {condition("Available", "False", "MinimumReplicasUnavailable", ""), condition("Progressing", "False", "ProgressDeadlineExceeded", "")},
		"none":                      nil,
	} {
		t.Run(name, func(t *testing.T) {
			s := stalledSnapshot(t, Options{}, []any{withConditions(deployment("shop", "api", 1, 0, app()), conditions...)})
			if got := stallOf(t, s, "api"); got != nil {
				t.Errorf("stall = %+v, want none", got)
			}
		})
	}
}

func TestAReasonThatIsNotAPlainNameIsReportedAsUnknownAndNeverAsItIs(t *testing.T) {
	for name, reason := range map[string]string{
		"empty":            "",
		"free text":        "pod creation failed for the user " + secretText,
		"a key":            secretText,
		"lower case":       "failedCreate",
		"a dash":           "Failed-Create",
		"a space":          "Failed Create",
		"one letter":       "F",
		"sixty-four chars": "A" + strings.Repeat("b", 63),
		"a newline":        "FailedCreate\nsecond line",
	} {
		t.Run(name, func(t *testing.T) {
			failing := withConditions(deployment("shop", "api", 1, 0, app()), condition("ReplicaFailure", "True", reason, planted))
			s := stalledSnapshot(t, Options{}, []any{failing})
			got := stallOf(t, s, "api")
			if got == nil || got.ReplicaFailure != "Unknown" {
				t.Fatalf("stall = %+v, want the failure named Unknown", got)
			}
			if strings.Contains(mustJSON(t, s), secretText) {
				t.Errorf("a reason carried text out of the cluster")
			}
		})
	}
	// The longest name that is one.
	reason := "A" + strings.Repeat("b", 62)
	failing := withConditions(deployment("shop", "api", 1, 0, app()), condition("ReplicaFailure", "True", reason, ""))
	if got := stallOf(t, stalledSnapshot(t, Options{}, []any{failing}), "api"); got == nil || got.ReplicaFailure != reason {
		t.Errorf("stall = %+v, want %s", got, reason)
	}
}

func TestOnlyDeploymentsHaveAReplicaFailure(t *testing.T) {
	// A StatefulSet carries conditions of its own kind in some versions: the kit reads ReplicaFailure of Deployments.
	f := newFake(t)
	set := withConditions(replicated("shop", "db", ptr(1), 0, app()), condition("ReplicaFailure", "True", "FailedCreate", planted))
	f.list(pathStatefulSets, set)
	s := f.snapshot(Options{CoverFields: true})
	if got := findWorkload(t, s, "shop", "StatefulSet", "db").Stall; got != nil {
		t.Errorf("stall = %+v", got)
	}
}

func TestTheWaitingReasonsOfTheInjectedInitContainerAreReportedSortedAndOncePerReason(t *testing.T) {
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 3, 1, app())}, apiPods(
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ImagePullBackOff")},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ErrImagePull")},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ImagePullBackOff")},
		[]podOpt{}, // a pod that runs
	)...)
	got := stallOf(t, s, "api")
	if got == nil || got.ReplicaFailure != "" || !reflect.DeepEqual(got.InitWaiting, []string{"ErrImagePull", "ImagePullBackOff"}) {
		t.Fatalf("stall = %+v, want the two reasons sorted", got)
	}
	if want := `{"initWaiting":["ErrImagePull","ImagePullBackOff"]}`; mustJSON(t, got) != want {
		t.Errorf("stall = %s, want %s", mustJSON(t, got), want)
	}
	if blob := mustJSON(t, s); strings.Contains(blob, secretText) || strings.Contains(blob, "forbidden") {
		t.Errorf("the message of a waiting container left the cluster: %s", blob)
	}
}

func TestBothInitContainersTheOperatorAddsAreWatched(t *testing.T) {
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 1, 0, app())}, apiPods(
		[]podOpt{withPhase("Pending"), initWaiting(injectNode, "ErrImagePull")},
	)...)
	if got := stallOf(t, s, "api"); got == nil || !reflect.DeepEqual(got.InitWaiting, []string{"ErrImagePull"}) {
		t.Fatalf("stall = %+v, want the Node.js init container's reason", got)
	}
}

func TestOnlyAFailureIsReportedNotAStartThatIsOnItsWay(t *testing.T) {
	for _, reason := range []string{"ContainerCreating", "PodInitializing"} {
		s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 1, 0, app())}, apiPods(
			[]podOpt{withPhase("Pending"), initWaiting(injectPython, reason)},
		)...)
		if got := stallOf(t, s, "api"); got != nil {
			t.Errorf("%s: stall = %+v, a pod that is starting is no stall", reason, got)
		}
	}
	// Other ways a start can fail are named.
	for _, reason := range []string{"CrashLoopBackOff", "CreateContainerConfigError", "CreateContainerError", "InvalidImageName", "RunContainerError", "ErrImageNeverPull"} {
		s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 1, 0, app())}, apiPods(
			[]podOpt{withPhase("Pending"), initWaiting(injectPython, reason)},
		)...)
		if got := stallOf(t, s, "api"); got == nil || !reflect.DeepEqual(got.InitWaiting, []string{reason}) {
			t.Errorf("%s: stall = %+v", reason, got)
		}
	}
}

func TestTheWaitingOfAnythingButTheInjectedInitContainerIsNoBusinessOfTheKit(t *testing.T) {
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 3, 0, app())}, apiPods(
		// another tool's init container
		[]podOpt{withPhase("Pending"), initWaiting("istio-init", "CrashLoopBackOff")},
		// the application's own init container, named like ours but with another prefix
		[]podOpt{withPhase("Pending"), initWaiting("wait-for-db", "ImagePullBackOff")},
		// the injected init container is running or has finished
		[]podOpt{withPhase("Pending"), initState(injectPython, obj{"running": obj{"startedAt": "2026-11-03T11:00:00Z"}})},
		[]podOpt{withPhase("Pending"), initState(injectPython, obj{"terminated": obj{"reason": "Completed", "exitCode": 0, "message": planted}})},
		[]podOpt{withPhase("Pending"), initState(injectPython, obj{})},
	)...)
	if got := stallOf(t, s, "api"); got != nil {
		t.Errorf("stall = %+v, want none", got)
	}
}

func TestAReasonThatIsNotAPlainNameIsDroppedFromTheWaitingReasons(t *testing.T) {
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 1, 0, app())}, apiPods(
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "Back-off pulling image for "+secretText)},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, secretText)},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "")},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ImagePullBackOff")},
	)...)
	got := stallOf(t, s, "api")
	if got == nil || !reflect.DeepEqual(got.InitWaiting, []string{"ImagePullBackOff"}) {
		t.Fatalf("stall = %+v, want the one plain name", got)
	}
	if strings.Contains(mustJSON(t, s), secretText) {
		t.Errorf("a reason carried text out of the cluster")
	}
}

func TestAtMostEightReasonsAreReportedForAWorkload(t *testing.T) {
	var opts [][]podOpt
	var all []string
	for i := 0; i < 12; i++ {
		reason := fmt.Sprintf("Reason%02d", i)
		all = append(all, reason)
		opts = append(opts, []podOpt{withPhase("Pending"), initWaiting(injectPython, reason)})
	}
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 12, 0, app())}, apiPods(opts...)...)
	got := stallOf(t, s, "api")
	if got == nil || !reflect.DeepEqual(got.InitWaiting, all[:8]) {
		t.Fatalf("stall = %+v, want the first eight by name", got)
	}
}

func TestAFinishedPodIsNoStall(t *testing.T) {
	s := stalledSnapshot(t, Options{}, []any{deployment("shop", "api", 1, 1, app())}, apiPods(
		[]podOpt{withPhase("Failed"), initWaiting(injectPython, "ImagePullBackOff")},
		[]podOpt{withPhase("Succeeded"), initWaiting(injectPython, "ImagePullBackOff")},
	)...)
	if got := stallOf(t, s, "api"); got != nil {
		t.Errorf("stall = %+v", got)
	}
}

func TestBothSignalsOfOneWorkloadAreReportedTogether(t *testing.T) {
	failing := withConditions(deployment("shop", "api", 3, 1, app()), condition("ReplicaFailure", "True", "FailedCreate", planted))
	s := stalledSnapshot(t, Options{}, []any{failing}, apiPods(
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ImagePullBackOff")},
	)...)
	want := `{"replicaFailure":"FailedCreate","initWaiting":["ImagePullBackOff"]}`
	if got := stallOf(t, s, "api"); got == nil || mustJSON(t, got) != want {
		t.Errorf("stall = %v, want %s", got, want)
	}
}

func TestEveryKindOfWorkloadReportsTheWaitingOfItsPods(t *testing.T) {
	f := newFake(t)
	app := app()
	f.list(pathStatefulSets, replicated("shop", "db", ptr(1), 0, app))
	f.list(pathDaemonSets, daemonSet("shop", "agent", 1, 0, app))
	f.list(pathCronJobs, cronJob("shop", "nightly", false, 1, app))
	f.list(pathJobs, job("shop", "nightly-29000123", nil, nil, app, controller("CronJob", "nightly")), job("shop", "migrate", nil, nil, app))
	waiting := []podOpt{withPhase("Pending"), initWaiting(injectPython, "ErrImagePull")}
	f.list(pathPods,
		pod("shop", "db-0", app, ptr(controller("StatefulSet", "db")), waiting...),
		pod("shop", "agent-x", app, ptr(controller("DaemonSet", "agent")), waiting...),
		pod("shop", "nightly-29000123-abc", app, ptr(controller("Job", "nightly-29000123")), waiting...),
		pod("shop", "migrate-abc", app, ptr(controller("Job", "migrate")), waiting...),
		pod("shop", "debug-shell", app, nil, waiting...),
	)
	s := f.snapshot(Options{CoverFields: true})
	for _, w := range []struct{ kind, name string }{{"StatefulSet", "db"}, {"DaemonSet", "agent"}, {"CronJob", "nightly"}, {"Job", "migrate"}, {"Pod", "debug-shell"}} {
		got := findWorkload(t, s, "shop", w.kind, w.name).Stall
		if got == nil || !reflect.DeepEqual(got.InitWaiting, []string{"ErrImagePull"}) {
			t.Errorf("%s %s: stall = %+v", w.kind, w.name, got)
		}
	}
}

func TestTheStallIsOnlySentWhenTheChartAsksForTheCoverFields(t *testing.T) {
	failing := withConditions(deployment("shop", "api", 1, 0, app()), condition("ReplicaFailure", "True", "FailedCreate", planted))
	build := func(opts Options) string {
		f := newFake(t)
		f.list(pathDeployments, failing)
		f.list(pathPods, apiPods([]podOpt{withPhase("Pending"), initWaiting(injectPython, "ImagePullBackOff")})...)
		return mustJSON(t, f.snapshot(opts))
	}
	// A server that does not know the field never sees it: without the cover fields the snapshot is as it always was.
	if blob := build(Options{}); strings.Contains(blob, `"stall"`) {
		t.Errorf("CoverFields off but the snapshot has a stall: %s", blob)
	}
	if blob := build(Options{CoverFields: true}); !strings.Contains(blob, `"stall":{"replicaFailure":"FailedCreate","initWaiting":["ImagePullBackOff"]}`) {
		t.Errorf("CoverFields on but the snapshot has no stall: %s", blob)
	}
}

func TestAHealthyClusterHasNoStallAnywhere(t *testing.T) {
	f := coverCluster(t, true)
	if blob := encoded(t, f.snapshot(Options{CoverFields: true})); strings.Contains(blob, "stall") {
		t.Errorf("a healthy cluster reports a stall: %s", blob)
	}
}

func TestTheStallIsAContractOfNamesAlone(t *testing.T) {
	// The fields of Stall, by reflection: two, both reasons, so that a field added later is a decision, not an accident.
	typ := reflect.TypeOf(Stall{})
	var fields []string
	for i := 0; i < typ.NumField(); i++ {
		tag := strings.Split(typ.Field(i).Tag.Get("json"), ",")
		fields = append(fields, tag[0]+","+strings.Join(tag[1:], ","))
	}
	if want := []string{"replicaFailure,omitempty", "initWaiting,omitempty"}; !reflect.DeepEqual(fields, want) {
		t.Errorf("Stall has the fields %v, want %v", fields, want)
	}
	// Whatever the cluster holds, every reason in a snapshot is a plain name.
	failing := withConditions(deployment("shop", "api", 1, 0, app()), condition("ReplicaFailure", "True", "pods are forbidden "+secretText, planted))
	s := stalledSnapshot(t, Options{}, []any{failing}, apiPods(
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "ErrImagePull")},
		[]podOpt{withPhase("Pending"), initWaiting(injectPython, "bad reason")},
	)...)
	var decoded struct {
		Workloads []struct {
			Stall *struct {
				ReplicaFailure string   `json:"replicaFailure"`
				InitWaiting    []string `json:"initWaiting"`
			} `json:"stall"`
		} `json:"workloads"`
	}
	if err := json.Unmarshal([]byte(mustJSON(t, s)), &decoded); err != nil {
		t.Fatal(err)
	}
	for _, w := range decoded.Workloads {
		if w.Stall == nil {
			continue
		}
		for _, reason := range append([]string{w.Stall.ReplicaFailure}, w.Stall.InitWaiting...) {
			if reason != "" && !reasonGrammar.MatchString(reason) {
				t.Errorf("reason %q is not a plain name", reason)
			}
		}
	}
}
