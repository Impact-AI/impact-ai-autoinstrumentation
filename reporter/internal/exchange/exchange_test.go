// SPDX-License-Identifier: Apache-2.0

package exchange

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubesecret"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/sender"
)

const (
	token       = "impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE" // 43 characters of secret
	otherToken  = "impactinstall_e2e_" + "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-ZyXwV"
	secretName  = "impact-ai-install-key"
	namespace   = "impact-ai"
	clusterUID  = "4e5622e7-f420-499c-9662-50af7f8a64b9"
	productID   = "prod_e2e"
	clusterARN  = "arn:aws:eks:eu-west-1:012345678901:cluster/kind-e2e"
	chartNumber = "0.3.0"
)

// fakeSecrets is a namespace with the Secrets the exchange keeps, and the means to make a call fail. It has no Create:
// the exchange is given no way to make a Secret (the Secrets interface has none), only to read, patch and, after the
// refusal of a first install, delete the one Helm made.
type fakeSecrets struct {
	mu       sync.Mutex
	secrets  map[string]*kubesecret.Secret
	calls    []string
	fail     map[string]error // "patch", "get", "delete" -> the error of every such call
	failFrom map[string]int   // "patch" -> n: every such call from the nth on (1 is the first) fails with fail[...]
	count    map[string]int
}

func newSecrets() *fakeSecrets {
	return &fakeSecrets{secrets: map[string]*kubesecret.Secret{}, fail: map[string]error{}, failFrom: map[string]int{}, count: map[string]int{}}
}

var _ Secrets = (*fakeSecrets)(nil)

func (f *fakeSecrets) get(name string) *kubesecret.Secret {
	f.mu.Lock()
	defer f.mu.Unlock()
	if s := f.secrets[name]; s != nil {
		return cloneSecret(s)
	}
	return nil
}

func (f *fakeSecrets) put(name string, s kubesecret.Secret) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.secrets[name] = cloneSecret(&s)
}

func cloneSecret(s *kubesecret.Secret) *kubesecret.Secret {
	out := &kubesecret.Secret{Labels: map[string]string{}, Annotations: map[string]string{}, Data: map[string][]byte{}}
	for k, v := range s.Labels {
		out.Labels[k] = v
	}
	for k, v := range s.Annotations {
		out.Annotations[k] = v
	}
	for k, v := range s.Data {
		out.Data[k] = append([]byte(nil), v...)
	}
	return out
}

func (f *fakeSecrets) record(call string) error {
	f.calls = append(f.calls, call)
	method := strings.SplitN(call, " ", 2)[0]
	f.count[method]++
	if n, ok := f.failFrom[method]; ok && f.count[method] < n {
		return nil
	}
	return f.fail[method]
}

func (f *fakeSecrets) Get(_ context.Context, _, name string) (*kubesecret.Secret, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.record("get " + name); err != nil {
		return nil, err
	}
	if s := f.secrets[name]; s != nil {
		return cloneSecret(s), nil
	}
	return nil, kubesecret.ErrNotFound
}

func (f *fakeSecrets) Patch(_ context.Context, _, name string, p kubesecret.Patch) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.record("patch " + name); err != nil {
		return err
	}
	s := f.secrets[name]
	if s == nil {
		return kubesecret.ErrNotFound
	}
	for k, v := range p.Annotations {
		if v == nil {
			delete(s.Annotations, k)
		} else {
			s.Annotations[k] = *v
		}
	}
	for k, v := range p.Data {
		if v == nil {
			delete(s.Data, k)
		} else {
			s.Data[k] = append([]byte(nil), (*v)...)
		}
	}
	return nil
}

func (f *fakeSecrets) Delete(_ context.Context, _, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.record("delete " + name); err != nil {
		return err
	}
	if f.secrets[name] == nil {
		return kubesecret.ErrNotFound
	}
	delete(f.secrets, name)
	return nil
}

// ingest is the server of the exchange: it records what it gets and answers from a script, each answer able to look at
// the cluster's Secrets at the moment the request arrives.
type ingest struct {
	*httptest.Server
	mu       sync.Mutex
	requests []seen
	script   []func(w http.ResponseWriter, r seen)
}

type seen struct {
	Path, Authorization, ContentType string
	Body                             []byte
}

func newIngest(t *testing.T, script ...func(w http.ResponseWriter, r seen)) *ingest {
	t.Helper()
	in := &ingest{script: script}
	in.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		req := seen{r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("Content-Type"), body}
		in.mu.Lock()
		in.requests = append(in.requests, req)
		var step func(http.ResponseWriter, seen)
		if len(in.script) > 0 {
			step, in.script = in.script[0], in.script[1:]
		}
		in.mu.Unlock()
		if step == nil {
			step = reply(200, success(false))
		}
		step(w, req)
	}))
	t.Cleanup(in.Close)
	return in
}

func (in *ingest) seen() []seen {
	in.mu.Lock()
	defer in.mu.Unlock()
	return append([]seen(nil), in.requests...)
}

func reply(status int, body string, headers ...string) func(http.ResponseWriter, seen) {
	return func(w http.ResponseWriter, _ seen) {
		for i := 0; i+1 < len(headers); i += 2 {
			w.Header().Set(headers[i], headers[i+1])
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func drop(w http.ResponseWriter, _ seen) {
	conn, _, err := w.(http.Hijacker).Hijack()
	if err == nil {
		conn.Close()
	}
}

func success(replayed bool) string {
	return fmt.Sprintf(`{"installId":"ins_0123","keyId":"pk_0123","productId":%q,"autoCover":true,"clusterName":"kind-e2e","replayed":%t}`, productID, replayed)
}

// messages are the fixed sentences of the real exchange, word for word: the
// chart prints the one that came with the refusal.
var messages = map[string]string{
	"malformed_request":   "The install request is not valid. Update the chart and run the install command again.",
	"token_unknown":       "This install command is not recognised. Create a new one in Cover your AI.",
	"token_revoked":       "This install command was cancelled, or the person who made it can no longer install. Create a new one in Cover your AI.",
	"token_wrong_product": "This install command is for another product. Create a new one in Cover your AI for this product.",
	"token_wrong_cluster": "This install command is for a different cluster. Create a new one in Cover your AI for this cluster.",
	"token_used":          "This install command was already used. Create a new one in Cover your AI.",
	"token_expired":       "This install command expired. Install commands are valid for 15 minutes; create a new one in Cover your AI.",
	"rate_limited":        "Too many install attempts. Wait a minute, then run the install command again.",
	"unavailable":         "Impact AI could not take this install just now. The install command is unchanged: run it again in a moment.",
}

func refusal(code string) string {
	body, _ := json.Marshal(map[string]string{"code": code, "message": messages[code]})
	return string(body)
}

// fixture is one run of the exchange against fakes.
type fixture struct {
	t       *testing.T
	secrets *fakeSecrets
	ingest  *ingest
	out     bytes.Buffer
	logs    bytes.Buffer
	sleeps  []time.Duration
	opts    Options
	rand    io.Reader
}

func newFixture(t *testing.T, script ...func(w http.ResponseWriter, r seen)) *fixture {
	t.Helper()
	f := &fixture{t: t, secrets: newSecrets(), ingest: newIngest(t, script...)}
	// What the chart's hook resource makes before the exchange runs: the Secret, empty, with its labels.
	f.secrets.put(secretName, kubesecret.Secret{Labels: map[string]string{"app.kubernetes.io/part-of": "impact-ai"}})
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	f.opts = Options{
		Endpoint:     f.ingest.URL,
		ProductID:    productID,
		Token:        token,
		ClusterUID:   clusterUID,
		ClusterName:  "kind-e2e",
		ClusterARN:   clusterARN,
		ChartVersion: chartNumber,
		Namespace:    namespace,
		SecretName:   secretName,
		Budget:       2 * time.Minute,
		Secrets:      f.secrets,
		Out:          &f.out,
		Log:          slog.New(slog.NewJSONHandler(&f.logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Now:          func() time.Time { return now },
		Sleep: func(ctx context.Context, d time.Duration) error {
			f.sleeps = append(f.sleeps, d)
			return ctx.Err()
		},
		Jitter: func() float64 { return 0.5 },
	}
	return f
}

func (f *fixture) run() Result {
	f.t.Helper()
	f.opts.Rand = f.rand
	return Run(context.Background(), f.opts)
}

func hashOf(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

type wireRequest struct {
	Schema    string `json:"schema"`
	ProductID string `json:"productId"`
	Cluster   struct {
		UID  string  `json:"uid"`
		ARN  *string `json:"arn"`
		Name *string `json:"name"`
	} `json:"cluster"`
	Key struct {
		LookupHash    string `json:"lookupHash"`
		DisplayPrefix string `json:"displayPrefix"`
	} `json:"key"`
	Chart struct {
		Version string `json:"version"`
	} `json:"chart"`
}

func decodeRequest(t *testing.T, r seen) wireRequest {
	t.Helper()
	var w wireRequest
	dec := json.NewDecoder(bytes.NewReader(r.Body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&w); err != nil {
		t.Fatalf("the request is not the documented document: %v\n%s", err, r.Body)
	}
	return w
}

var keyShape = regexp.MustCompile(`^impact_e2e_[A-Za-z0-9_-]{43}$`)

func TestAFreshInstallStoresTheKeyBeforeCallingAndSendsOnlyItsHash(t *testing.T) {
	var keyAtCallTime string
	var annotationsAtCallTime map[string]string
	f := newFixture(t)
	f.ingest.script = []func(http.ResponseWriter, seen){func(w http.ResponseWriter, r seen) {
		// The write-ahead: when impact.ai is asked, the key is already in the cluster.
		if s := f.secrets.get(secretName); s != nil {
			keyAtCallTime = string(s.Data["pending-key"])
			annotationsAtCallTime = s.Annotations
		}
		reply(200, success(false))(w, r)
	}}
	res := f.run()

	if res.Outcome != Exchanged || res.Replayed || res.KeyID != "pk_0123" || res.InstallID != "ins_0123" {
		t.Fatalf("result = %+v", res)
	}
	if !keyShape.MatchString(keyAtCallTime) {
		t.Fatalf("the key in the cluster at call time = %q, want impact_<env>_<43 characters>", keyAtCallTime)
	}
	if annotationsAtCallTime["impact.ai/exchange"] != "pending" || annotationsAtCallTime["impact.ai/pending-token"] != fingerprint(token) {
		t.Errorf("annotations at call time = %v", annotationsAtCallTime)
	}

	reqs := f.ingest.seen()
	if len(reqs) != 1 || reqs[0].Path != "/v1/installs/exchange" || reqs[0].Authorization != "Bearer "+token || reqs[0].ContentType != "application/json" {
		t.Fatalf("requests = %+v", reqs)
	}
	w := decodeRequest(t, reqs[0])
	if w.Schema != "impact.install.v1" || w.ProductID != productID || w.Cluster.UID != clusterUID || w.Chart.Version != chartNumber {
		t.Errorf("request = %+v", w)
	}
	if w.Cluster.ARN == nil || *w.Cluster.ARN != clusterARN || w.Cluster.Name == nil || *w.Cluster.Name != "kind-e2e" {
		t.Errorf("cluster = %+v", w.Cluster)
	}
	if w.Key.LookupHash != hashOf(keyAtCallTime) || w.Key.DisplayPrefix != keyAtCallTime[:len("impact_e2e_")+8] {
		t.Errorf("key = %+v for the key %q", w.Key, keyAtCallTime)
	}
	if bytes.Contains(reqs[0].Body, []byte(keyAtCallTime)) || bytes.Contains(reqs[0].Body, []byte(keyAtCallTime[len("impact_e2e_"):])) {
		t.Errorf("the request carries the key itself: %s", reqs[0].Body)
	}

	// After the 200 the key is the working one, the write-ahead is gone, and the Secret says whose it is.
	s := f.secrets.get(secretName)
	if s == nil || string(s.Data["ingest-key"]) != keyAtCallTime {
		t.Fatalf("secret = %+v", s)
	}
	if _, pending := s.Data["pending-key"]; pending {
		t.Errorf("the pending key was left behind")
	}
	if s.Annotations["impact.ai/exchange"] != "exchanged" || s.Annotations["impact.ai/token"] != fingerprint(token) {
		t.Errorf("annotations = %v", s.Annotations)
	}
	if _, left := s.Annotations["impact.ai/pending-token"]; left {
		t.Errorf("the pending-token annotation was left behind: %v", s.Annotations)
	}
	if s.Labels["app.kubernetes.io/part-of"] != "impact-ai" {
		t.Errorf("labels = %v", s.Labels)
	}
	if f.out.Len() != 0 {
		t.Errorf("a successful exchange printed %q", f.out.String())
	}
}

func TestAnAbsentArnAndNameAreSentAsNull(t *testing.T) {
	f := newFixture(t)
	f.opts.ClusterARN, f.opts.ClusterName = "", ""
	if res := f.run(); res.Outcome != Exchanged {
		t.Fatalf("result = %+v", res)
	}
	w := decodeRequest(t, f.ingest.seen()[0])
	if w.Cluster.ARN != nil || w.Cluster.Name != nil {
		t.Errorf("cluster = %+v, want null arn and name", w.Cluster)
	}
	if !strings.Contains(string(f.ingest.seen()[0].Body), `"arn":null`) {
		t.Errorf("body = %s", f.ingest.seen()[0].Body)
	}
}

func TestEveryRunMakesAKeyOfItsOwnFromTheRandomSource(t *testing.T) {
	a, b := newFixture(t), newFixture(t)
	a.run()
	b.run()
	ka, kb := string(a.secrets.get(secretName).Data["ingest-key"]), string(b.secrets.get(secretName).Data["ingest-key"])
	if !keyShape.MatchString(ka) || ka == kb {
		t.Errorf("keys %q and %q", ka, kb)
	}

	// The source is injectable, and its bytes are the key's secret: 32 random bytes, base64url without padding.
	c := newFixture(t)
	c.rand = bytes.NewReader(bytes.Repeat([]byte{0xff}, 32))
	c.run()
	want := "impact_e2e_" + strings.Repeat("_", 42) + "8"
	if got := string(c.secrets.get(secretName).Data["ingest-key"]); got != want {
		t.Errorf("key from 32 bytes of 0xff = %q, want %q", got, want)
	}
}

func TestALostAnswerIsRetriedWithTheSameHashAndTheKeyIsStoredOnce(t *testing.T) {
	f := newFixture(t, drop, reply(200, success(true)))
	res := f.run()
	if res.Outcome != Exchanged || !res.Replayed {
		t.Fatalf("result = %+v", res)
	}
	reqs := f.ingest.seen()
	if len(reqs) != 2 || !bytes.Equal(reqs[0].Body, reqs[1].Body) {
		t.Fatalf("requests = %d, want two with the same body", len(reqs))
	}
	// Two patches and no more: the key stored ahead once, and promoted once; the retry did not store it again.
	patches := 0
	for _, c := range f.secrets.calls {
		if strings.HasPrefix(c, "patch") {
			patches++
		}
	}
	if patches != 2 {
		t.Errorf("the Secret was patched %d times: %v", patches, f.secrets.calls)
	}
	if got, want := hashOf(string(f.secrets.get(secretName).Data["ingest-key"])), decodeRequest(t, reqs[1]).Key.LookupHash; got != want {
		t.Errorf("the stored key hashes to %s, but the call carried %s", got, want)
	}
}

func TestAJobThatDiedAfterTheWriteAheadResumesWithTheStoredKey(t *testing.T) {
	f := newFixture(t, reply(200, success(true)))
	stored := "impact_e2e_" + strings.Repeat("A", 43)
	f.secrets.put(secretName, kubesecret.Secret{
		Annotations: map[string]string{"impact.ai/exchange": "pending", "impact.ai/pending-token": fingerprint(token)},
		Data:        map[string][]byte{"pending-key": []byte(stored)},
	})
	res := f.run()
	if res.Outcome != Exchanged || !res.Replayed {
		t.Fatalf("result = %+v", res)
	}
	if got := decodeRequest(t, f.ingest.seen()[0]).Key.LookupHash; got != hashOf(stored) {
		t.Errorf("the call carried %s, want the hash of the stored key %s", got, hashOf(stored))
	}
	if got := string(f.secrets.get(secretName).Data["ingest-key"]); got != stored {
		t.Errorf("ingest-key = %q, want the stored key", got)
	}
	if patches := f.secrets.count["patch"]; patches != 1 {
		t.Errorf("a resumed run patched the Secret %d times, want the promotion only: %v", patches, f.secrets.calls)
	}
}

func TestAPendingKeyOfAnotherTokenIsNotReused(t *testing.T) {
	f := newFixture(t)
	old := "impact_e2e_" + strings.Repeat("B", 43)
	f.secrets.put(secretName, kubesecret.Secret{
		Annotations: map[string]string{"impact.ai/exchange": "pending", "impact.ai/pending-token": fingerprint(otherToken)},
		Data:        map[string][]byte{"pending-key": []byte(old)},
	})
	if res := f.run(); res.Outcome != Exchanged {
		t.Fatalf("result = %+v", res)
	}
	got := string(f.secrets.get(secretName).Data["ingest-key"])
	if got == old || !keyShape.MatchString(got) || decodeRequest(t, f.ingest.seen()[0]).Key.LookupHash != hashOf(got) {
		t.Errorf("ingest-key = %q; the key of the other token was reused", got)
	}
}

func TestTheSameTokenAgainChangesNothingAndCallsNobody(t *testing.T) {
	// `helm upgrade --reuse-values` hands the old token to the hook again.
	f := newFixture(t)
	kept := "impact_e2e_" + strings.Repeat("C", 43)
	f.secrets.put(secretName, kubesecret.Secret{
		Annotations: map[string]string{"impact.ai/exchange": "exchanged", "impact.ai/token": fingerprint(token)},
		Data:        map[string][]byte{"ingest-key": []byte(kept)},
	})
	res := f.run()
	if res.Outcome != Skipped {
		t.Fatalf("result = %+v, want Skipped", res)
	}
	if n := len(f.ingest.seen()); n != 0 {
		t.Errorf("%d requests reached impact.ai", n)
	}
	for _, c := range f.secrets.calls {
		if !strings.HasPrefix(c, "get") {
			t.Errorf("a skipped run wrote to the cluster: %v", f.secrets.calls)
		}
	}
	if got := string(f.secrets.get(secretName).Data["ingest-key"]); got != kept {
		t.Errorf("ingest-key = %q", got)
	}
}

func TestANewTokenKeepsTheWorkingKeyUntilTheNewOneIsIn(t *testing.T) {
	old := "impact_e2e_" + strings.Repeat("D", 43)
	seed := func(f *fixture) {
		f.secrets.put(secretName, kubesecret.Secret{
			Annotations: map[string]string{"impact.ai/exchange": "exchanged", "impact.ai/token": fingerprint(otherToken)},
			Data:        map[string][]byte{"ingest-key": []byte(old)},
		})
	}

	t.Run("accepted", func(t *testing.T) {
		var during *kubesecret.Secret
		f := newFixture(t)
		f.ingest.script = []func(http.ResponseWriter, seen){func(w http.ResponseWriter, r seen) {
			during = f.secrets.get(secretName)
			reply(200, success(false))(w, r)
		}}
		seed(f)
		if res := f.run(); res.Outcome != Exchanged {
			t.Fatalf("result = %+v", res)
		}
		if string(during.Data["ingest-key"]) != old || !keyShape.MatchString(string(during.Data["pending-key"])) {
			t.Errorf("while the call was out the Secret held %v: the working key must stay and the new one wait beside it", keys(during))
		}
		s := f.secrets.get(secretName)
		if got := string(s.Data["ingest-key"]); got == old || !keyShape.MatchString(got) {
			t.Errorf("ingest-key = %q, want the new key", got)
		}
		if s.Annotations["impact.ai/token"] != fingerprint(token) || len(s.Data) != 1 {
			t.Errorf("secret = %v %v", s.Annotations, keys(s))
		}
	})

	t.Run("refused", func(t *testing.T) {
		f := newFixture(t, reply(409, refusal("token_used")))
		seed(f)
		if res := f.run(); res.Outcome != Refused {
			t.Fatalf("result = %+v", res)
		}
		s := f.secrets.get(secretName)
		if s == nil || string(s.Data["ingest-key"]) != old || len(s.Data) != 1 {
			t.Fatalf("a refusal must leave the working key alone: %v", keys(s))
		}
		if s.Annotations["impact.ai/token"] != fingerprint(otherToken) || s.Annotations["impact.ai/exchange"] != "exchanged" {
			t.Errorf("annotations = %v", s.Annotations)
		}
		if _, left := s.Annotations["impact.ai/pending-token"]; left {
			t.Errorf("pending-token left behind: %v", s.Annotations)
		}
	})
}

func keys(s *kubesecret.Secret) []string {
	if s == nil {
		return nil
	}
	var out []string
	for k := range s.Data {
		out = append(out, k)
	}
	return out
}

func TestARefusalRemovesTheKeyAndPrintsTheMessageThatCameWithIt(t *testing.T) {
	cases := []struct {
		status int
		code   string
	}{
		{401, "token_unknown"}, {403, "token_revoked"}, {409, "token_used"}, {410, "token_expired"},
		{403, "token_wrong_product"}, {403, "token_wrong_cluster"}, {400, "malformed_request"},
	}
	for _, tc := range cases {
		t.Run(tc.code, func(t *testing.T) {
			f := newFixture(t, reply(tc.status, refusal(tc.code)), reply(200, success(false)))
			res := f.run()
			if res.Outcome != Refused || res.Code != tc.code || res.Status != tc.status {
				t.Fatalf("result = %+v", res)
			}
			if n := len(f.ingest.seen()); n != 1 {
				t.Errorf("%d requests: a refusal is not retried", n)
			}
			// A refused first install leaves nothing: the Secret Helm made, and the pending key in it, are gone.
			if s := f.secrets.get(secretName); s != nil {
				t.Errorf("the pending Secret was left behind: %v", keys(s))
			}
			lines := strings.Split(strings.TrimSpace(f.out.String()), "\n")
			// The first line is the message impact.ai sent, as it sent it, and nothing else of the answer.
			if lines[0] != messages[tc.code] {
				t.Errorf("first line = %q, want %q", lines[0], messages[tc.code])
			}
			// The second says what the customer is left with: a fact of the cluster, not of the answer.
			if len(lines) != 2 || !strings.Contains(lines[1], `"`+namespace+`"`) || !strings.Contains(lines[1], "Instrumentation") {
				t.Errorf("lines = %q", lines)
			}
			for _, secret := range []string{token, "pending-key", tc.code} {
				if strings.Contains(f.out.String(), secret) {
					t.Errorf("%q was printed", secret)
				}
			}
		})
	}
}

func TestAnAnswerWithoutAMessageGetsTheSentenceOfItsCode(t *testing.T) {
	// An older or other server: the code (or, lacking that, the status) still gets a plain sentence.
	cases := []struct {
		status int
		body   string
		says   string
	}{
		{409, `{"code":"token_used"}`, "already used"},
		{410, `{"code":"token_expired"}`, "expired"},
		{403, `{"code":"token_wrong_product"}`, "another product"},
		{409, ``, "already used"},
		{418, `{"code":"something_new"}`, "refused the install (status 418)"},
		{418, `not json at all`, "refused the install (status 418)"},
	}
	for _, tc := range cases {
		f := newFixture(t, reply(tc.status, tc.body))
		res := f.run()
		if res.Outcome != Refused {
			t.Errorf("%s: result = %+v", tc.body, res)
		}
		if !strings.Contains(f.out.String(), tc.says) || !strings.Contains(f.out.String(), "Cover your AI") {
			t.Errorf("%d %q: output = %q, want it to say %q", tc.status, tc.body, f.out.String(), tc.says)
		}
	}
}

func TestAMessageIsOnlyPrintedWhenItIsASinglePlainLine(t *testing.T) {
	// The message is text from outside, printed to a customer's terminal: nothing that could move the cursor, hide
	// text, or smuggle in the token is ever printed, and a message that is not plain gives way to the sentence of
	// the code.
	hostile := map[string]string{
		"an escape sequence": "This install command was already used.\x1b[2J\x1b[H",
		"a new line":         "This install command was already used.\nrm -rf /",
		"a carriage return":  "Create a new one.\rThis command is fine.",
		"a tab":              "This\tcommand",
		"too long":           strings.Repeat("a plain word ", 60),
		"empty":              "   ",
		"the token":          "Your token " + token + " was used.",
		"a bearer header":    "Authorization: Bearer abc",
		"invalid utf-8":      "This \xff command",
	}
	for name, message := range hostile {
		body, _ := json.Marshal(map[string]string{"code": "token_used", "message": message})
		f := newFixture(t, reply(409, string(body)))
		if res := f.run(); res.Outcome != Refused {
			t.Errorf("%s: result = %+v", name, res)
		}
		out := f.out.String()
		if first := strings.Split(out, "\n")[0]; !strings.Contains(first, "already used") || strings.Contains(first, message) {
			t.Errorf("%s: first line = %q, want the sentence of the code in place of the message", name, first)
		}
		for _, bad := range []string{"\x1b", "\r", "\t", "rm -rf", token, "Bearer"} {
			if strings.Contains(out, bad) {
				t.Errorf("%s: the output holds %q: %q", name, bad, out)
			}
		}
	}
	// A plain message is printed as it is, however it is punctuated.
	f := newFixture(t, reply(409, `{"code":"token_used","message":"Wiederholen: bitte neu erstellen \u2014 jetzt."}`))
	f.run()
	if first := strings.Split(f.out.String(), "\n")[0]; first != "Wiederholen: bitte neu erstellen \u2014 jetzt." {
		t.Errorf("first line = %q", first)
	}
}

func TestAMalformedTokenIsRefusedWithoutCallingImpactAi(t *testing.T) {
	for _, bad := range []string{"", "abc", "impact_e2e_" + strings.Repeat("A", 43), "impactinstall__" + strings.Repeat("A", 43), "impactinstall_E2E_" + strings.Repeat("A", 43), "impactinstall_e2e_short"} {
		f := newFixture(t)
		f.opts.Token = bad
		res := f.run()
		if res.Outcome != Refused || res.Code != "token_unknown" {
			t.Errorf("token %q: result = %+v", bad, res)
		}
		// Impact AI is not called, and the empty Secret Helm made is read and deleted: a refusal leaves nothing.
		if len(f.ingest.seen()) != 0 || strings.Join(f.secrets.calls, ",") != "get "+secretName+",delete "+secretName {
			t.Errorf("token %q: impact.ai was called (%d) or the cluster touched in another way (%v)", bad, len(f.ingest.seen()), f.secrets.calls)
		}
		if f.secrets.get(secretName) != nil {
			t.Errorf("token %q: the empty Secret was left behind", bad)
		}
		if first := strings.Split(f.out.String(), "\n")[0]; first != messages["token_unknown"] {
			t.Errorf("token %q: output = %q", bad, f.out.String())
		}
	}
}

func TestAMalformedTokenDeletesNothingWhenTheSecretCannotBeRead(t *testing.T) {
	f := newFixture(t)
	f.opts.Token = "abc"
	f.secrets.fail["get"] = errors.New("the API server is down")
	if res := f.run(); res.Outcome != Refused {
		t.Fatalf("result = %+v", res)
	}
	for _, call := range f.secrets.calls {
		if strings.HasPrefix(call, "delete") {
			t.Errorf("a Secret that could not be read was deleted: %v", f.secrets.calls)
		}
	}
	if f.secrets.get(secretName) == nil {
		t.Error("the Secret is gone")
	}
}

func TestARefusalOfAFreshCommandKeepsTheSecretAndTheWorkingKey(t *testing.T) {
	f := newFixture(t, reply(409, refusal("token_used")))
	f.secrets.put(secretName, kubesecret.Secret{
		Annotations: map[string]string{"impact.ai/exchange": "exchanged", "impact.ai/token": fingerprint(otherToken)},
		Data:        map[string][]byte{"ingest-key": []byte("impact_e2e_" + strings.Repeat("W", 43))},
	})
	if res := f.run(); res.Outcome != Refused {
		t.Fatalf("result = %+v", res)
	}
	for _, call := range f.secrets.calls {
		if strings.HasPrefix(call, "delete") {
			t.Errorf("the Secret of a cluster that reports was deleted: %v", f.secrets.calls)
		}
	}
	if s := f.secrets.get(secretName); s == nil || len(s.Data["ingest-key"]) == 0 || len(s.Data) != 1 {
		t.Errorf("the working key was not left alone: %v", keys(s))
	}
}

func TestWhenImpactAiIsBusyTheKeyStaysForTheNextRun(t *testing.T) {
	var script []func(http.ResponseWriter, seen)
	for range 5 {
		script = append(script, reply(503, refusal("unavailable"), "Retry-After", "5"))
	}
	f := newFixture(t, script...)
	res := f.run()
	if res.Outcome != Unavailable || res.Status != 503 {
		t.Fatalf("result = %+v", res)
	}
	if n := len(f.ingest.seen()); n != 5 {
		t.Errorf("%d attempts, want 5", n)
	}
	for _, d := range f.sleeps {
		if d != 5*time.Second {
			t.Errorf("sleeps = %v, want Retry-After's 5s each time", f.sleeps)
			break
		}
	}
	s := f.secrets.get(secretName)
	if s == nil || !keyShape.MatchString(string(s.Data["pending-key"])) || s.Annotations["impact.ai/pending-token"] != fingerprint(token) {
		t.Fatalf("the write-ahead must stay so that the next run replays: %+v", s)
	}
	if got := strings.TrimSpace(f.out.String()); got != messages["unavailable"] {
		t.Errorf("output = %q, want the message impact.ai sent", got)
	}

	// The next run, with the very same token, sends the very same hash.
	f2 := newFixture(t)
	f2.secrets = f.secrets
	f2.opts.Secrets = f.secrets
	if res := f2.run(); res.Outcome != Exchanged {
		t.Fatalf("second run: %+v", res)
	}
	if decodeRequest(t, f2.ingest.seen()[0]).Key.LookupHash != hashOf(string(f.secrets.get(secretName).Data["ingest-key"])) {
		t.Errorf("the second run did not reuse the stored key")
	}
}

func TestWhenNothingAnswersTheCustomerIsToldSoAndTheSameCommandCanBeRunAgain(t *testing.T) {
	var script []func(http.ResponseWriter, seen)
	for range 5 {
		script = append(script, drop)
	}
	f := newFixture(t, script...)
	res := f.run()
	if res.Outcome != Unavailable || res.Status != 0 {
		t.Fatalf("result = %+v", res)
	}
	for _, want := range []string{"could not be reached", "Nothing was changed", "run the same command again"} {
		if !strings.Contains(f.out.String(), want) {
			t.Errorf("output %q lacks %q", f.out.String(), want)
		}
	}
}

func TestRateLimitingIsWaitedOutWithRetryAfterAndTheTokenIsNotSpent(t *testing.T) {
	f := newFixture(t, reply(429, `{"code":"rate_limited"}`, "Retry-After", "7"), reply(200, success(false)))
	if res := f.run(); res.Outcome != Exchanged {
		t.Fatalf("result = %+v", res)
	}
	if len(f.sleeps) != 1 || f.sleeps[0] != 7*time.Second {
		t.Errorf("sleeps = %v, want 7s", f.sleeps)
	}
}

func TestWhenTheKeyCannotBeStoredNothingIsSentToImpactAi(t *testing.T) {
	f := newFixture(t)
	f.secrets.fail["patch"] = errors.New("secrets is forbidden") // the write-ahead is the first patch
	res := f.run()
	if res.Outcome != Failed {
		t.Fatalf("result = %+v", res)
	}
	if n := len(f.ingest.seen()); n != 0 {
		t.Errorf("%d requests: the token must not be spent while the key has nowhere to go", n)
	}
	if !strings.Contains(f.out.String(), "could not store") || !strings.Contains(f.out.String(), "Nothing was sent to impact.ai") {
		t.Errorf("output = %q", f.out.String())
	}
}

func TestWhenTheAnswerCannotBeStoredTheKeyStaysPendingForTheNextRun(t *testing.T) {
	f := newFixture(t)
	f.secrets.fail["patch"] = errors.New("the API server is down")
	f.secrets.failFrom["patch"] = 2 // the write-ahead works, the promotion does not
	res := f.run()
	if res.Outcome != Failed {
		t.Fatalf("result = %+v", res)
	}
	s := f.secrets.get(secretName)
	if s == nil || !keyShape.MatchString(string(s.Data["pending-key"])) {
		t.Fatalf("the key must stay where the next run finds it: %+v", s)
	}
	if _, promoted := s.Data["ingest-key"]; promoted {
		t.Errorf("a half-promoted Secret: %v", keys(s))
	}
}

func TestWithoutTheSecretHelmMakesNothingIsSentAndNothingIsCreated(t *testing.T) {
	f := newFixture(t)
	delete(f.secrets.secrets, secretName)
	res := f.run()
	if res.Outcome != Failed {
		t.Fatalf("result = %+v", res)
	}
	if n := len(f.ingest.seen()); n != 0 {
		t.Errorf("%d requests: the token must not be spent while the key has nowhere to go", n)
	}
	if f.secrets.get(secretName) != nil {
		t.Errorf("the exchange made the Secret itself")
	}
	for _, call := range f.secrets.calls {
		if !strings.HasPrefix(call, "get") {
			t.Errorf("the exchange wrote with no Secret to write to: %v", f.secrets.calls)
		}
	}
	if !strings.Contains(f.out.String(), "Helm") || !strings.Contains(f.out.String(), "Nothing was sent to impact.ai") {
		t.Errorf("output = %q", f.out.String())
	}
}

func TestOnlyTheKeyAndTheTwoMarkersAreWrittenAndEveryWriteIsAPatchOfTheOneSecret(t *testing.T) {
	f := newFixture(t)
	if res := f.run(); res.Outcome != Exchanged {
		t.Fatalf("result = %+v", res)
	}
	for _, call := range f.secrets.calls {
		if call != "get "+secretName && call != "patch "+secretName {
			t.Errorf("call %q: the exchange reads and patches the one Secret and does nothing else", call)
		}
	}
	s := f.secrets.get(secretName)
	if len(s.Data) != 1 || len(s.Annotations) != 2 {
		t.Errorf("secret = %v %v, want the key and the two markers", keys(s), s.Annotations)
	}
}

func TestAnAnswerForAnotherProductIsNotTrusted(t *testing.T) {
	f := newFixture(t, reply(200, `{"installId":"ins_1","keyId":"pk_1","productId":"prod_other","autoCover":true,"replayed":false}`))
	res := f.run()
	if res.Outcome != Failed {
		t.Fatalf("result = %+v", res)
	}
	if s := f.secrets.get(secretName); s != nil && s.Data["ingest-key"] != nil {
		t.Errorf("the key was installed on an answer for another product")
	}
}

func TestAnAnswerThatIsNotTheDocumentIsNotTrusted(t *testing.T) {
	for _, body := range []string{``, `not json`, `{}`, `{"productId":"prod_e2e"}`, `{"keyId":"pk_1"}`} {
		f := newFixture(t, reply(200, body))
		if res := f.run(); res.Outcome != Failed {
			t.Errorf("answer %q: result = %+v", body, res)
		}
	}
}

func TestNeitherTheTokenNorTheKeyIsEverLogged(t *testing.T) {
	scripts := [][]func(http.ResponseWriter, seen){
		{reply(200, success(false))},
		{drop, reply(200, success(true))},
		{reply(409, refusal("token_used"))},
		{reply(503, refusal("unavailable")), reply(200, success(false))},
	}
	for i, script := range scripts {
		f := newFixture(t, script...)
		f.run()
		key := ""
		if s := f.secrets.get(secretName); s != nil {
			key = string(s.Data["ingest-key"])
		}
		for _, secret := range []string{token, token[len("impactinstall_e2e_"):], key} {
			if secret != "" && strings.Contains(f.logs.String()+f.out.String(), secret) {
				t.Errorf("script %d: %q was logged or printed", i, secret)
			}
		}
	}
}

func TestTheFingerprintIsNotTheTokenAndDiffersPerToken(t *testing.T) {
	a, b := fingerprint(token), fingerprint(otherToken)
	if len(a) != 16 || a == b || strings.Contains(token, a) {
		t.Errorf("fingerprints %q %q", a, b)
	}
	if got, want := a, hashOf(token)[:16]; got != want {
		t.Errorf("fingerprint = %q, want the first 16 hex characters of the token's sha256 (%q): the chart computes the same", got, want)
	}
}

func TestTheRequestStaysUnderFourKiBForTheLongestValues(t *testing.T) {
	f := newFixture(t)
	f.opts.ClusterName = strings.Repeat("n", 253)
	f.opts.ClusterARN = "arn:aws:eks:eu-west-1:012345678901:cluster/" + strings.Repeat("c", 2000)
	f.opts.ProductID = "prod_" + strings.Repeat("p", 100)
	f.ingest.script = []func(http.ResponseWriter, seen){reply(200, strings.Replace(success(false), productID, f.opts.ProductID, 1))}
	if res := f.run(); res.Outcome != Exchanged {
		t.Fatalf("result = %+v", res)
	}
	if n := len(f.ingest.seen()[0].Body); n > 4096 {
		t.Errorf("the request is %d bytes, over the server's 4 KiB limit", n)
	}
}

func TestTheBudgetBoundsTheWholeRun(t *testing.T) {
	f := newFixture(t, reply(429, `{"code":"rate_limited"}`, "Retry-After", "600"))
	f.opts.Budget = 30 * time.Second
	res := f.run()
	if res.Outcome != Unavailable || res.Status != 429 {
		t.Fatalf("result = %+v: a Retry-After beyond the budget ends the run", res)
	}
	if len(f.sleeps) != 0 {
		t.Errorf("slept %v", f.sleeps)
	}
}

// The default clock and sleeper are the real ones, and the run honours the context.
func TestACanceledRunLeavesTheKeyPending(t *testing.T) {
	f := newFixture(t, reply(503, `{}`))
	ctx, cancel := context.WithCancel(context.Background())
	f.opts.Sleep = func(ctx context.Context, d time.Duration) error { cancel(); return ctx.Err() }
	res := Run(ctx, f.opts)
	if res.Outcome != Unavailable && res.Outcome != Failed {
		t.Errorf("result = %+v", res)
	}
	if s := f.secrets.get(secretName); s == nil || s.Data["pending-key"] == nil {
		t.Errorf("a canceled run must leave the write-ahead: %+v", s)
	}
}

var _ = sender.Accepted

func TestAClusterNameAndAnArnTheServerCouldNotReadAreLeftOutNotSentToFailTheInstall(t *testing.T) {
	// The chart accepts a name of 253 characters and an ARN of 2,048; the exchange reads 128 and 512 and refuses the
	// whole request for more. They are only labels: the install goes on without them.
	cases := []struct {
		name, arn   string
		wantName    string
		wantARN     bool
		wantNameCut bool
	}{
		{name: strings.Repeat("n", 253), arn: "arn:aws:eks:eu-west-1:012345678901:cluster/ok", wantNameCut: true, wantARN: true},
		{name: "kind-e2e", arn: "arn:" + strings.Repeat("a", 600), wantName: "kind-e2e"},
		{name: "has\na newline", arn: "arn:aws:eks:x y", wantName: ""},
		{name: strings.Repeat("é", 200), arn: "", wantNameCut: true},
	}
	for _, tc := range cases {
		f := newFixture(t)
		f.opts.ClusterName, f.opts.ClusterARN = tc.name, tc.arn
		if res := f.run(); res.Outcome != Exchanged {
			t.Fatalf("%q: result = %+v", tc.name, res)
		}
		w := decodeRequest(t, f.ingest.seen()[0])
		switch {
		case tc.wantNameCut:
			if w.Cluster.Name == nil || utf8.RuneCountInString(*w.Cluster.Name) != 128 {
				t.Errorf("name = %v, want 128 characters", w.Cluster.Name)
			}
		case tc.wantName == "":
			if w.Cluster.Name != nil {
				t.Errorf("name = %q, want none: it holds a control character", *w.Cluster.Name)
			}
		case w.Cluster.Name == nil || *w.Cluster.Name != tc.wantName:
			t.Errorf("name = %v", w.Cluster.Name)
		}
		if (w.Cluster.ARN != nil) != tc.wantARN {
			t.Errorf("arn = %v, want present: %v", w.Cluster.ARN, tc.wantARN)
		}
	}
}
