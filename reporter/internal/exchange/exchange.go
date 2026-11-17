// SPDX-License-Identifier: Apache-2.0

// Package exchange trades the install token of a Cover your AI command for this cluster's own ingest key, inside the
// cluster. It runs as a Helm pre-install and pre-upgrade hook (`reporter exchange`).
//
// The key is made here, with crypto/rand, and only its SHA-256 goes to impact.ai: no answer carries a secret and
// impact.ai never sees the key, so the key exists only in a Secret of this cluster. Nobody copies, pastes or types it.
//
// The Secret is made by Helm, empty, before this hook runs (a hook resource of the chart). The hook reads, patches and,
// when impact.ai refuses a first install, deletes it, by name: it has no right to create a Secret, so it has none to read
// or change any other.
//
// Three rules make a run safe to repeat and safe to lose halfway:
//
//   - Write ahead. The key is stored in the Secret before impact.ai is asked (as `pending-key`, beside the fingerprint
//     of the token it belongs to). If the answer is lost, or the pod dies, the next run finds the key and sends the same
//     hash; impact.ai answers a repeat of the same hash with replayed:true instead of spending the token twice. If the
//     key cannot be stored, nothing is sent and the token is not spent.
//   - Promote only on success. On a 200 the pending key becomes `ingest-key`. A key that was working stays in
//     `ingest-key` until then, so a refused fresh command on a cluster that already reports changes nothing.
//   - Skip a stale token. `helm upgrade --reuse-values` hands the old token to the hook again; when the Secret carries
//     that token's fingerprint the hook has nothing to do.
//
// A refusal (a token that is used, expired, cancelled, for another product or cluster) removes the pending key, and the
// Secret with it when it holds no working key (a refused first install leaves nothing of the kit), and prints the
// sentence impact.ai sent with it: every refusal carries one fixed, plain message, which names nothing the
// caller sent. Only that message is taken from the answer, and only when it is one plain line; an answer without one gets
// the sentence of its code. A transient failure (impact.ai busy or out of reach) keeps the key.
package exchange

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/kubesecret"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/sender"
)

// Path is the ingest route of the exchange.
const Path = "/v1/installs/exchange"

// Schema is the schema name of the request.
const Schema = "impact.install.v1"

// What the Secret holds. `ingest-key` is the name the reporter, the gateway and the cover pod read.
const (
	keyData        = "ingest-key"
	pendingKeyData = "pending-key"
	// exchangeAnnotation says whether the Secret holds a working key: "exchanged", or "pending" before the first.
	exchangeAnnotation = "impact.ai/exchange"
	// tokenAnnotation is the fingerprint of the token whose exchange produced the working key.
	tokenAnnotation = "impact.ai/token"
	// pendingTokenAnnotation is the fingerprint of the token of the exchange in flight, whose key is `pending-key`.
	pendingTokenAnnotation = "impact.ai/pending-token"
)

// The token is impactinstall_<environment>_<secret>; the environment names the key, which is impact_<environment>_<secret>.
var tokenShape = regexp.MustCompile(`^impactinstall_([a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?)_[A-Za-z0-9_-]{16,}$`)

const keyRandomBytes = 32 // the length of every impact.ai key: 43 base64url characters

// Secrets is the part of the Kubernetes API the exchange uses: read one Secret, patch it and, after the refusal of a
// first install, delete it. kubesecret.Store implements it. The exchange makes no Secret.
type Secrets interface {
	Get(ctx context.Context, namespace, name string) (*kubesecret.Secret, error)
	Patch(ctx context.Context, namespace, name string, p kubesecret.Patch) error
	Delete(ctx context.Context, namespace, name string) error
}

// Options say what to exchange and where to keep the result.
type Options struct {
	// Endpoint is the ingest origin; the exchange is POST <Endpoint>/v1/installs/exchange.
	Endpoint  string
	ProductID string
	// Token is the install token. It is never logged or printed.
	Token string
	// ClusterUID is the UID of kube-system; ClusterName and ClusterARN are optional.
	ClusterUID   string
	ClusterName  string
	ClusterARN   string
	ChartVersion string

	// Namespace and SecretName say where the key is kept: a Secret Helm made empty before the hook ran.
	Namespace  string
	SecretName string
	Secrets    Secrets

	// Budget bounds the whole run, retries included; zero means two minutes.
	Budget time.Duration
	// Out gets the plain sentences for the customer (Helm shows the hook's output when it fails).
	Out io.Writer
	Log *slog.Logger

	// Test seams. Nil means the real clock, sleep, randomness and HTTP client.
	Now    func() time.Time
	Sleep  func(ctx context.Context, d time.Duration) error
	Jitter func() float64
	Rand   io.Reader
	Client *http.Client
}

// Outcome says how a run ended.
type Outcome int

const (
	// Exchanged: the key is in the Secret, and impact.ai knows its hash.
	Exchanged Outcome = iota
	// Skipped: the Secret already holds the key of this very token.
	Skipped
	// Refused: impact.ai (or the shape of the token) said no for good. The pending key is gone.
	Refused
	// Unavailable: impact.ai could not be reached or is busy. The pending key stays, so that a rerun replays.
	Unavailable
	// Failed: something on our side (the cluster, an answer that is not the document). The pending key stays.
	Failed
)

func (o Outcome) String() string {
	return [...]string{"exchanged", "skipped", "refused", "unavailable", "failed"}[o]
}

// Result describes a run, for the exit code and the tests.
type Result struct {
	Outcome Outcome
	// Code and Status are what impact.ai answered when it refused or failed.
	Code   string
	Status int
	// InstallID and KeyID name the install and the key at impact.ai (not secrets). Replayed says that impact.ai
	// had the hash already: an earlier answer was lost.
	InstallID string
	KeyID     string
	Replayed  bool
}

// fingerprint is the first 16 hex characters of the token's SHA-256. The chart computes the same.
func fingerprint(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])[:16]
}

func (o *Options) log() *slog.Logger {
	if o.Log != nil {
		return o.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// say writes one line for the customer. The sentence is a plain line of text: what Helm shows of a failed hook is what
// the customer reads.
func (o *Options) say(format string, args ...any) {
	if o.Out != nil {
		fmt.Fprintf(o.Out, format+"\n", args...)
	}
}

func (o *Options) random() io.Reader {
	if o.Rand != nil {
		return o.Rand
	}
	return rand.Reader
}

// newKey makes impact_<environment>_<43 characters>, as impact.ai does for a key it makes itself.
func (o *Options) newKey(environment string) (string, error) {
	var b [keyRandomBytes]byte
	if _, err := io.ReadFull(o.random(), b[:]); err != nil {
		return "", fmt.Errorf("no randomness for the key: %w", err)
	}
	return "impact_" + environment + "_" + base64.RawURLEncoding.EncodeToString(b[:]), nil
}

func lookupHash(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

// displayPrefix is what impact.ai shows of a key: its first 8 secret characters.
func displayPrefix(environment, key string) string {
	secret := strings.TrimPrefix(key, "impact_"+environment+"_")
	return "impact_" + environment + "_" + secret[:8]
}

type request struct {
	Schema    string         `json:"schema"`
	ProductID string         `json:"productId"`
	Cluster   requestCluster `json:"cluster"`
	Key       requestKey     `json:"key"`
	Chart     requestChart   `json:"chart"`
}

type requestCluster struct {
	UID  string  `json:"uid"`
	ARN  *string `json:"arn"`
	Name *string `json:"name"`
}

type requestKey struct {
	LookupHash    string `json:"lookupHash"`
	DisplayPrefix string `json:"displayPrefix"`
}

type requestChart struct {
	Version string `json:"version"`
}

type answer struct {
	InstallID string `json:"installId"`
	KeyID     string `json:"keyId"`
	ProductID string `json:"productId"`
	AutoCover bool   `json:"autoCover"`
	Replayed  bool   `json:"replayed"`
}

func orNull(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// The exchange reads a cluster name of up to 128 characters, printable, and an ARN of up to 512 of the characters an
// ARN has; for more it refuses the whole request. Both are only labels, so the install goes on with what can be read:
// the name is cut (or left out when it holds a control character), an ARN that could not be one is left out.
const maxClusterNameRunes = 128

var arnShape = regexp.MustCompile(`^[A-Za-z0-9/][A-Za-z0-9._:/@+=,-]{0,511}$`)

// credentials is what the exchange takes for a credential in a value it is given, whatever the field: a key or an install
// token by its exact shape, a Bearer header, a JWT, cloud and code-host tokens, a private key (the rule of
// the impact.ai service, copied). A cluster name or an ARN of that shape is refused with the whole
// request, so the reporter leaves it out.
var credentials = regexp.MustCompile(`impact(?:install)?_[a-z0-9-]{1,32}_[A-Za-z0-9_-]{43}|(?i:\bBearer +[A-Za-z0-9._~+/-]+=*)|eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:AKIA|ASIA)[A-Z0-9]{16}|(?:sk-|ghp_|github_pat_|xoxb-|xoxp-|AIza)[A-Za-z0-9_-]{20,}|(?s:-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)`)

func credentialShaped(value string) bool { return credentials.MatchString(value) }

func (o *Options) clusterLabels() (name, arn string) {
	name = o.ClusterName
	if utf8.RuneCountInString(name) > maxClusterNameRunes {
		name = string([]rune(name)[:maxClusterNameRunes])
	}
	if !utf8.ValidString(name) || strings.IndexFunc(name, unicode.IsControl) >= 0 || credentialShaped(name) {
		name = ""
	}
	if name != o.ClusterName {
		o.log().Warn("the cluster name is not sent as given: the exchange reads 128 printable characters", "given", utf8.RuneCountInString(o.ClusterName))
	}
	arn = o.ClusterARN
	if arn != "" && (!arnShape.MatchString(arn) || credentialShaped(arn)) {
		o.log().Warn("the cluster ARN is not sent: it is not one the exchange can read", "length", len(arn))
		arn = ""
	}
	return name, arn
}

func (o *Options) body(environment, key string) ([]byte, error) {
	name, arn := o.clusterLabels()
	return json.Marshal(request{
		Schema:    Schema,
		ProductID: o.ProductID,
		Cluster:   requestCluster{UID: o.ClusterUID, ARN: orNull(arn), Name: orNull(name)},
		Key:       requestKey{LookupHash: lookupHash(key), DisplayPrefix: displayPrefix(environment, key)},
		Chart:     requestChart{Version: o.ChartVersion},
	})
}

// Run exchanges the token. It never returns an error: the outcome says what happened, and the customer's sentence has
// been written to Out.
func Run(ctx context.Context, o Options) Result {
	log := o.log()
	environment, ok := tokenEnvironment(o.Token)
	if !ok {
		res := o.refused(Result{Outcome: Refused, Code: "token_unknown"}, nil, false)
		o.discardPending(o.holdsAWorkingKey())
		return res
	}
	budget := o.Budget
	if budget <= 0 {
		budget = 2 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	fp := fingerprint(o.Token)

	// What the cluster holds of an earlier run.
	state, err := o.Secrets.Get(ctx, o.Namespace, o.SecretName)
	switch {
	case errors.Is(err, kubesecret.ErrNotFound):
		log.Error("the Secret that Helm makes for the ingest key is not there", "secret", o.SecretName)
		o.say("The Secret %q, which Helm makes for the ingest key before this step, is not in the cluster. Nothing was sent to impact.ai, so the install command is still valid; run it again.", o.SecretName)
		return Result{Outcome: Failed}
	case err != nil:
		log.Error("could not read the Secret that holds the ingest key", "secret", o.SecretName, "error", err)
		o.say("The install could not read the Secret %q in the cluster (%v). Nothing was sent to impact.ai; fix the permission it names and run the command again.", o.SecretName, err)
		return Result{Outcome: Failed}
	}
	hasWorkingKey := len(state.Data[keyData]) > 0
	if hasWorkingKey && state.Annotations[exchangeAnnotation] == "exchanged" && state.Annotations[tokenAnnotation] == fp {
		log.Info("this install token was already exchanged for the key in this cluster; nothing to do", "secret", o.SecretName)
		return Result{Outcome: Skipped}
	}

	// The key: the one an earlier run of this very token stored, or a new one stored now, before impact.ai is asked.
	key, resumed := "", false
	if state.Annotations[pendingTokenAnnotation] == fp {
		if stored := string(state.Data[pendingKeyData]); keyOf(environment).MatchString(stored) {
			key, resumed = stored, true
		}
	}
	if !resumed {
		if key, err = o.newKey(environment); err != nil {
			log.Error("could not make a key", "error", err)
			o.say("The install could not make an ingest key (%v). Nothing was sent to impact.ai.", err)
			return Result{Outcome: Failed}
		}
		if err := o.storeAhead(ctx, state, key, fp); err != nil {
			log.Error("could not store the ingest key before asking impact.ai", "secret", o.SecretName, "error", err)
			o.say("The install could not store the ingest key in the Secret %q (%v). Nothing was sent to impact.ai, so the install command is still valid; fix the permission it names and run the command again.", o.SecretName, err)
			return Result{Outcome: Failed}
		}
	}
	log.Info("asking impact.ai to exchange the install token", "resumed", resumed, "secret", o.SecretName)

	payload, err := o.body(environment, key)
	if err != nil {
		log.Error("could not encode the exchange request", "error", err)
		return Result{Outcome: Failed}
	}
	s := sender.Sender{
		Endpoint:  o.Endpoint,
		Client:    o.Client,
		UserAgent: "impact-reporter-exchange",
		Interval:  budget, // a Retry-After beyond the budget ends the run
		Now:       o.Now,
		Sleep:     o.Sleep,
		Jitter:    o.Jitter,
		Log:       log,
	}
	reply := s.PostJSON(ctx, Path, o.Token, payload)

	switch reply.Outcome {
	case sender.Accepted:
		return o.promote(ctx, reply, fp, key)
	case sender.Rejected, sender.KeyRejected:
		res := Result{Outcome: Refused, Code: refusalCode(reply.Body), Status: reply.Status}
		if res.Code == "" {
			res.Code = codeOfStatus(reply.Status)
		}
		o.discardPending(hasWorkingKey)
		return o.refused(res, reply.Body, true)
	default: // GaveUp, Canceled
		res := Result{Outcome: Unavailable, Status: reply.Status, Code: refusalCode(reply.Body)}
		log.Warn("impact.ai could not take the exchange now; the key stays stored so that the same command can be run again",
			"status", reply.Status, "outcome", reply.Outcome.String())
		sentence := ""
		if reply.Status != 0 {
			sentence = o.serverSentence(reply.Body)
		}
		if sentence == "" {
			what := "could not be reached"
			if reply.Status != 0 {
				what = "could not take this install just now (" + statusWords(reply.Status) + ")"
			}
			sentence = "Impact AI " + what + ". Nothing was changed: run the same command again while it is valid (15 minutes), or create a new one in Cover your AI."
		}
		o.say("%s", sentence)
		return res
	}
}

// keyOf is the shape of a key of this environment.
func keyOf(environment string) *regexp.Regexp {
	return regexp.MustCompile(`^impact_` + regexp.QuoteMeta(environment) + `_[A-Za-z0-9_-]{43}$`)
}

func tokenEnvironment(token string) (string, bool) {
	m := tokenShape.FindStringSubmatch(token)
	if m == nil {
		return "", false
	}
	return m[1], true
}

// storeAhead puts the key where the next run finds it, before impact.ai is asked. A key that works stays in
// `ingest-key` until the new one is accepted.
func (o *Options) storeAhead(ctx context.Context, state *kubesecret.Secret, key, fp string) error {
	exchange := "pending"
	if len(state.Data[keyData]) > 0 {
		exchange = "exchanged"
	}
	pendingKey := []byte(key)
	return o.Secrets.Patch(ctx, o.Namespace, o.SecretName, kubesecret.Patch{
		Annotations: map[string]*string{exchangeAnnotation: &exchange, pendingTokenAnnotation: &fp},
		Data:        map[string]*[]byte{pendingKeyData: &pendingKey},
	})
}

// promote makes the accepted key the working one.
func (o *Options) promote(ctx context.Context, reply sender.Reply, fp, key string) Result {
	log := o.log()
	var a answer
	if err := json.Unmarshal(reply.Body, &a); err != nil || a.KeyID == "" || a.InstallID == "" {
		log.Error("impact.ai accepted the exchange but its answer is not the document the chart expects", "status", reply.Status)
		o.say("impact.ai accepted the install but its answer could not be read. The key stays stored; run the same command again to finish.")
		return Result{Outcome: Failed, Status: reply.Status}
	}
	if a.ProductID != o.ProductID {
		log.Error("impact.ai answered for another product than the one asked", "status", reply.Status)
		o.say("impact.ai answered for another product than the one in this command. The key was not installed; create a fresh install command in Cover your AI.")
		return Result{Outcome: Failed, Status: reply.Status}
	}
	exchanged, stamp := "exchanged", fp
	// The context of the run may be nearly spent after retries; storing the result is worth its own time.
	storeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	var err error
	for attempt := 1; attempt <= 3; attempt++ {
		keyBytes := []byte(key)
		err = o.Secrets.Patch(storeCtx, o.Namespace, o.SecretName, kubesecret.Patch{
			Annotations: map[string]*string{exchangeAnnotation: &exchanged, tokenAnnotation: &stamp, pendingTokenAnnotation: nil},
			Data:        map[string]*[]byte{keyData: &keyBytes, pendingKeyData: nil},
		})
		if err == nil {
			break
		}
		if attempt < 3 {
			if sleepErr := o.sleep(storeCtx, time.Duration(attempt)*time.Second); sleepErr != nil {
				break
			}
		}
	}
	if err != nil {
		log.Error("impact.ai accepted the exchange but the key could not be stored as the working key", "secret", o.SecretName, "error", err)
		o.say("The install was accepted but the key could not be stored as the working key in the Secret %q (%v). It stays stored; run the same command again to finish.", o.SecretName, err)
		return Result{Outcome: Failed, Status: reply.Status}
	}
	log.Info("the install token was exchanged for this cluster's own ingest key", "installId", a.InstallID, "keyId", a.KeyID,
		"replayed", a.Replayed, "autoCover", a.AutoCover, "secret", o.SecretName)
	return Result{Outcome: Exchanged, Status: reply.Status, InstallID: a.InstallID, KeyID: a.KeyID, Replayed: a.Replayed}
}

func (o *Options) sleep(ctx context.Context, d time.Duration) error {
	if o.Sleep != nil {
		return o.Sleep(ctx, d)
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

// discardPending removes the key a refused exchange stored. A working key beside it is left exactly as it was; without
// one the Secret holds nothing worth keeping, and goes: a refused first install leaves nothing of the kit.
func (o *Options) discardPending(hasWorkingKey bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	log := o.log()
	if hasWorkingKey {
		err := o.Secrets.Patch(ctx, o.Namespace, o.SecretName, kubesecret.Patch{
			Annotations: map[string]*string{pendingTokenAnnotation: nil},
			Data:        map[string]*[]byte{pendingKeyData: nil},
		})
		if err != nil && !errors.Is(err, kubesecret.ErrNotFound) {
			log.Warn("could not remove the pending key after the refusal", "secret", o.SecretName, "error", err)
		}
		return
	}
	if err := o.Secrets.Delete(ctx, o.Namespace, o.SecretName); err != nil && !errors.Is(err, kubesecret.ErrNotFound) {
		log.Warn("could not delete the pending Secret after the refusal", "secret", o.SecretName, "error", err)
	}
}

// holdsAWorkingKey says whether the Secret already holds an ingest key, for a refusal that comes before the Secret was
// read. A Secret that cannot be read counts as holding one: nothing is deleted on a guess.
func (o *Options) holdsAWorkingKey() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	state, err := o.Secrets.Get(ctx, o.Namespace, o.SecretName)
	if err != nil {
		return true
	}
	return len(state.Data[keyData]) > 0
}

var codePattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

// refusalCode reads the code of an error answer, {"code":"token_used",...}; an answer that has none gives "".
func refusalCode(body []byte) string {
	var e struct {
		Code string `json:"code"`
	}
	if json.Unmarshal(bytes.TrimSpace(body), &e) != nil || !codePattern.MatchString(e.Code) {
		return ""
	}
	return e.Code
}

// codeOfStatus names the refusal of an answer that carried no code.
func codeOfStatus(status int) string {
	switch status {
	case http.StatusBadRequest:
		return "malformed_request"
	case http.StatusUnauthorized:
		return "token_unknown"
	case http.StatusConflict:
		return "token_used"
	case http.StatusGone:
		return "token_expired"
	}
	return ""
}

func statusWords(status int) string {
	if status == 0 {
		return "no answer"
	}
	return fmt.Sprintf("status %d", status)
}

// reasons are the sentences of the refusals, for an answer that came without its own. They are the real exchange's
// messages, word for word, so a refusal reads the same either way.
var reasons = map[string]string{
	"malformed_request":   "The install request is not valid. Update the chart and run the install command again.",
	"token_unknown":       "This install command is not recognised. Create a new one in Cover your AI.",
	"token_revoked":       "This install command was cancelled, or the person who made it can no longer install. Create a new one in Cover your AI.",
	"token_wrong_product": "This install command is for another product. Create a new one in Cover your AI for this product.",
	"token_wrong_cluster": "This install command is for a different cluster. Create a new one in Cover your AI for this cluster.",
	"token_used":          "This install command was already used. Create a new one in Cover your AI.",
	"token_expired":       "This install command expired. Install commands are valid for 15 minutes; create a new one in Cover your AI.",
}

// maxSentenceRunes bounds the message taken from an answer; the longest of the real ones has about 140 characters.
const maxSentenceRunes = 400

// serverSentence is the message that came with an answer, when it is one plain line that is safe to print: valid text, no
// control or format character (nothing that moves the cursor or reorders what is shown), not long, and nothing that
// repeats the token or looks like a credential. Anything else gives "", and the caller uses the sentence of the code.
func (o *Options) serverSentence(body []byte) string {
	var answer struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(bytes.TrimSpace(body), &answer) != nil {
		return ""
	}
	text := strings.TrimSpace(answer.Message)
	if text == "" || utf8.RuneCountInString(text) > maxSentenceRunes {
		return ""
	}
	for _, r := range text {
		if r == utf8.RuneError || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || unicode.Is(unicode.Zl, r) || unicode.Is(unicode.Zp, r) {
			return ""
		}
	}
	lower := strings.ToLower(text)
	for _, secret := range []string{strings.ToLower(o.Token), "impactinstall_", "bearer"} {
		if secret != "" && strings.Contains(lower, secret) {
			return ""
		}
	}
	return text
}

// refused writes the sentence for a refusal and what the customer is left with, and returns the result. The sentence
// is the message impact.ai sent; contacted says whether impact.ai was asked, which only matters for the log.
func (o *Options) refused(res Result, body []byte, contacted bool) Result {
	sentence := o.serverSentence(body)
	if sentence == "" {
		var known bool
		if sentence, known = reasons[res.Code]; !known {
			sentence = fmt.Sprintf("Impact AI refused the install (%s). Create a new install command in Cover your AI and run it again.", statusWords(res.Status))
		}
	}
	o.log().Warn("the install token was refused", "code", res.Code, "status", res.Status, "contacted", contacted)
	o.say("%s", sentence)
	o.say("Nothing was installed or changed. What a first attempt leaves is an empty namespace %q and the Instrumentation resource definition, which the next install uses.", o.Namespace)
	return res
}
