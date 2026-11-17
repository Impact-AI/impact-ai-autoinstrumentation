// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"net/http"
	"net/url"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/relaytest"
)

// Every relay source's recorded requests, sent through a relay route: each reaches the backend byte for byte with its
// credentials, and the copies impact.ai gets are exactly what the source's decoder makes of the requests it claims,
// with none of the recorded credentials in them.
func TestEveryRelaySourcesRecordingsAreRelayedUnchangedAndCopiedAsTheirDecoderReadsThem(t *testing.T) {
	recordings, err := filepath.Glob("../relay/source/testdata/*/*.recording.json")
	if err != nil || len(recordings) == 0 {
		t.Fatalf("no recordings: %v", err)
	}
	for _, path := range recordings {
		id := filepath.Base(filepath.Dir(path))
		if decoders[id] == nil {
			t.Errorf("%s: no relay source %q", path, id)
			continue
		}
		t.Run(id+"/"+strings.TrimSuffix(filepath.Base(path), ".recording.json"), func(t *testing.T) {
			rec, err := relaytest.ReadRecording(path)
			if err != nil {
				t.Fatal(err)
			}
			backend := newRecorder(t, func(w http.ResponseWriter, _ *http.Request, _ []byte) {
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"return_value":{}}`))
			})
			copyTo := newRecorder(t, nil)
			g := startGateway(t, Route{Name: "vendor", Protocol: ProtocolRelay, Target: backend.srv.URL, Decode: id}, copyTo, gatewayOptions{})

			var want [][]byte
			var credentials []string
			for i, recorded := range rec.Requests {
				r, body, err := recorded.HTTPRequest()
				if err != nil {
					t.Fatal(err)
				}
				credentials = append(credentials, recordedCredentials(recorded)...)
				out, err := http.NewRequest(recorded.Method, g.url+recorded.Path+"?"+recorded.Query, bytes.NewReader(body))
				if err != nil {
					t.Fatal(err)
				}
				out.Header = r.Header.Clone()
				out.Header.Del("Content-Length")
				out.Header.Del("Host")
				out.Header.Del("Connection")
				do(t, http.DefaultClient, out)

				got := backend.requests()
				if len(got) != i+1 {
					t.Fatalf("request %d: the backend saw %d requests", i, len(got))
				}
				seen := got[i]
				if !bytes.Equal(seen.Body, body) || seen.Method != recorded.Method || seen.Path != recorded.Path || seen.Query != recorded.Query {
					t.Errorf("request %d reached the backend changed: %s %s?%s", i, seen.Method, seen.Path, seen.Query)
				}
				// Every header the tracer sent reaches the backend with its values, credentials included; only the
				// hop-by-hop headers and Host belong to the connection.
				for name, values := range r.Header {
					switch http.CanonicalHeaderKey(name) {
					case "Connection", "Transfer-Encoding", "Content-Length", "Host", "Keep-Alive", "Te", "Trailer", "Upgrade", "Proxy-Connection":
						continue
					}
					if !slices.Equal(seen.Header.Values(name), values) {
						t.Errorf("request %d: the header %s reached the backend changed", i, name)
					}
				}

				single := relaytest.Recording{How: rec.How, Packages: rec.Packages, Recorded: rec.Recorded, Requests: rec.Requests[i : i+1]}
				if traces, err := relaytest.Decode(decoders[id], single); err == nil && len(traces.ResourceSpans) > 0 {
					want = append(want, canonicalTraces(t, traces))
				}
			}
			if len(want) == 0 {
				t.Fatalf("the decoder copies nothing of %s", path)
			}
			eventually(t, "every copy", func() bool { return len(copyTo.requests()) >= len(want) })
			never(t, "more copies", func() bool { return len(copyTo.requests()) > len(want) })

			var got [][]byte
			for _, c := range copyTo.requests() {
				if c.Header.Get("Content-Type") != "application/json" {
					t.Errorf("copy content type %q", c.Header.Get("Content-Type"))
				}
				canonical, err := relaytest.Canonical(c.Body)
				if err != nil {
					t.Fatalf("a copy is not JSON: %v", err)
				}
				for _, secret := range credentials {
					if bytes.Contains(c.Body, []byte(secret)) || strings.Contains(c.Query, secret) || strings.Contains(fmtHeader(c.Header), secret) {
						t.Errorf("a recorded credential reached impact.ai")
					}
				}
				got = append(got, canonical)
			}
			slices.SortFunc(want, bytes.Compare)
			slices.SortFunc(got, bytes.Compare)
			if !slices.EqualFunc(want, got, bytes.Equal) {
				t.Errorf("the copies differ from what the decoder makes of the recording")
			}
			if s := g.stats.Snapshot()["vendor"]; s.Partial != 0 || s.Copied != uint64(len(want)) {
				t.Errorf("stats %+v", s)
			}
		})
	}
}

func canonicalTraces(t *testing.T, traces otlp.Traces) []byte {
	t.Helper()
	data, err := otlp.Marshal(traces)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := relaytest.Canonical(data)
	if err != nil {
		t.Fatal(err)
	}
	return canonical
}

// recordedCredentials are the values of a recorded request's headers and query parameters whose names look like
// credentials.
func recordedCredentials(r relaytest.RecordedRequest) []string {
	var out []string
	for name, value := range r.Headers {
		if relay.CredentialName(name) && len(value) >= 8 {
			out = append(out, value)
		}
	}
	query, _ := url.ParseQuery(r.Query)
	for name, values := range query {
		if relay.CredentialName(name) {
			for _, v := range values {
				if len(v) >= 8 {
					out = append(out, v)
				}
			}
		}
	}
	return out
}

func fmtHeader(h http.Header) string {
	var b strings.Builder
	for k, v := range h {
		b.WriteString(k + ": " + strings.Join(v, ",") + "\n")
	}
	return b.String()
}
