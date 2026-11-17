// SPDX-License-Identifier: Apache-2.0

package exchange

import (
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"regexp"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"
)

// The shared vectors of the exchange: a byte-for-byte copy of the test vectors of the
// impact.ai service, whose intake reads the same file (the kit's mock ingest reads it too, chart/tests/test_mock_ingest.py).
// It lists every way the body of POST /v1/installs/exchange may be wrong. The kit keeps one copy, next to the mock.
const vectorsFile = "../../../testing/mock/exchange-vectors.json"

type vectorStep struct {
	Set    string          `json:"set"`
	Remove string          `json:"remove"`
	Value  json.RawMessage `json:"value"`
}

type vectorCase struct {
	Name  string       `json:"name"`
	Valid bool         `json:"valid"`
	Patch []vectorStep `json:"patch"`
}

type vectors struct {
	Base  json.RawMessage `json:"base"`
	Cases []vectorCase    `json:"cases"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile(vectorsFile)
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

// body applies the steps of a case to the base body.
func (v vectors) body(t *testing.T, c vectorCase) []byte {
	t.Helper()
	var doc any
	if err := json.Unmarshal(v.Base, &doc); err != nil {
		t.Fatal(err)
	}
	for _, step := range c.Patch {
		path := step.Set
		if path == "" {
			path = step.Remove
		}
		parts := strings.Split(path, ".")
		node := doc
		for _, part := range parts[:len(parts)-1] {
			node = node.(map[string]any)[part]
		}
		last := parts[len(parts)-1]
		object := node.(map[string]any)
		if step.Set != "" {
			var value any
			if err := json.Unmarshal(step.Value, &value); err != nil {
				t.Fatal(err)
			}
			object[last] = value
		} else {
			delete(object, last)
		}
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// The rules of the real exchange of the impact.ai service, as far as the body goes. They
// are ported here to hold the reporter's request to them; TestThePortReadsTheSharedVectorsAsTheFileSays keeps the port
// honest against the file the real decoder is held to.
var (
	rePortProduct = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$`)
	rePortUID     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$`)
	rePortARN     = regexp.MustCompile(`^[A-Za-z0-9/][A-Za-z0-9._:/@+=,-]{0,511}$`)
	rePortHash    = regexp.MustCompile(`^[a-f0-9]{64}$`)
	rePortPrefix  = regexp.MustCompile(`^impact_([a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?)_[A-Za-z0-9_-]{8}$`)
	rePortVersion = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`)
)

func portPlain(re *regexp.Regexp, value string) bool {
	return re.MatchString(value) && !credentialShaped(value)
}

func portText(value string, runes int) bool {
	if value == "" || utf8.RuneCountInString(value) > runes || credentialShaped(value) {
		return false
	}
	for _, r := range value {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return true
}

type portBody struct {
	Schema    string `json:"schema"`
	ProductID string `json:"productId"`
	Cluster   *struct {
		UID  string  `json:"uid"`
		ARN  *string `json:"arn"`
		Name *string `json:"name"`
	} `json:"cluster"`
	Key *struct {
		LookupHash    string `json:"lookupHash"`
		DisplayPrefix string `json:"displayPrefix"`
	} `json:"key"`
	Chart *struct {
		Version string `json:"version"`
	} `json:"chart"`
}

// readsLikeTheRealExchange is whether the real decoder accepts the body.
func readsLikeTheRealExchange(body []byte) bool {
	var w portBody
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if dec.Decode(&w) != nil || w.Schema != Schema || w.Cluster == nil || w.Key == nil || w.Chart == nil {
		return false
	}
	prefix := rePortPrefix.FindStringSubmatch(w.Key.DisplayPrefix)
	switch {
	case !portPlain(rePortProduct, w.ProductID),
		!portPlain(rePortUID, w.Cluster.UID),
		w.Cluster.ARN != nil && !portPlain(rePortARN, *w.Cluster.ARN),
		w.Cluster.Name != nil && !portText(*w.Cluster.Name, 128),
		!rePortHash.MatchString(w.Key.LookupHash),
		prefix == nil || credentialShaped(w.Key.DisplayPrefix),
		!portPlain(rePortVersion, w.Chart.Version):
		return false
	}
	return true
}

func TestThePortReadsTheSharedVectorsAsTheFileSays(t *testing.T) {
	v := loadVectors(t)
	if len(v.Cases) < 40 {
		t.Fatalf("%d cases: did the file change shape?", len(v.Cases))
	}
	for _, c := range v.Cases {
		if got := readsLikeTheRealExchange(v.body(t, c)); got != c.Valid {
			t.Errorf("%s: the port says valid=%v, the file says %v", c.Name, got, c.Valid)
		}
	}
}

func TestTheRequestWeSendIsOneTheRealExchangeReads(t *testing.T) {
	v := loadVectors(t)
	var base struct {
		ProductID string `json:"productId"`
		Cluster   struct {
			UID  string `json:"uid"`
			ARN  string `json:"arn"`
			Name string `json:"name"`
		} `json:"cluster"`
	}
	if err := json.Unmarshal(v.Base, &base); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name, clusterName, arn, version string
	}{
		{"the base cluster", base.Cluster.Name, base.Cluster.ARN, "0.3.0"},
		{"no name and no ARN", "", "", "0.3.0"},
		{"a pre-release", "kind-e2e", "", "0.3.0-rc.1+build5"},
		{"the longest name", strings.Repeat("a", 128), base.Cluster.ARN, "0.3.0"},
		{"a name of 253 characters", strings.Repeat("n", 253), "", "0.3.0"},
		{"a name of 200 multibyte characters", strings.Repeat("é", 200), "", "0.3.0"},
		{"a name with a new line", "has\nnewline", "", "0.3.0"},
		{"a name that looks like a key", "sk-" + strings.Repeat("a", 26), "", "0.3.0"}, // built, so that the source holds no key-shaped text
		{"an ARN that is a token", "", "eyJhbGciOiJIUzI1NiJ9.e30.c2ln", "0.3.0"},
		{"an ARN of 600 characters", "", "arn:" + strings.Repeat("a", 596), "0.3.0"},
		{"an ARN with a space", "", "arn:aws:eks:x y", "0.3.0"},
	}
	for _, tc := range cases {
		f := newFixture(t)
		f.opts.ProductID, f.opts.ClusterUID = base.ProductID, base.Cluster.UID
		f.opts.ClusterName, f.opts.ClusterARN, f.opts.ChartVersion = tc.clusterName, tc.arn, tc.version
		f.ingest.script = []func(w http.ResponseWriter, r seen){reply(200, strings.Replace(success(false), productID, base.ProductID, 1))}
		if res := f.run(); res.Outcome != Exchanged {
			t.Errorf("%s: result = %+v", tc.name, res)
			continue
		}
		sent := f.ingest.seen()[0].Body
		if !readsLikeTheRealExchange(sent) {
			t.Errorf("%s: the real exchange would refuse the request: %s", tc.name, sent)
		}
	}
}

func TestOurRequestHasExactlyTheMembersOfTheBaseBody(t *testing.T) {
	v := loadVectors(t)
	f := newFixture(t)
	f.opts.ProductID = "prod_0123456789abcdef01234567"
	f.ingest.script = []func(w http.ResponseWriter, r seen){reply(200, strings.Replace(success(false), productID, f.opts.ProductID, 1))}
	f.run()
	var base, sent map[string]any
	if err := json.Unmarshal(v.Base, &base); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(f.ingest.seen()[0].Body, &sent); err != nil {
		t.Fatal(err)
	}
	if got, want := shape(sent), shape(base); got != want {
		t.Errorf("members\n%s\nwant\n%s", got, want)
	}
}

// shape lists the member paths of a document.
func shape(doc any) string {
	var paths []string
	var walk func(prefix string, node any)
	walk = func(prefix string, node any) {
		object, ok := node.(map[string]any)
		if !ok {
			return
		}
		for key, child := range object {
			paths = append(paths, prefix+key)
			walk(prefix+key+".", child)
		}
	}
	walk("", doc)
	slices.Sort(paths)
	return strings.Join(paths, "\n")
}
