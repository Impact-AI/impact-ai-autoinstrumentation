// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// A vendor's key in the headers and a token in the target pass to the backend and are found nowhere else: not in what
// impact.ai receives, not in the gateway's logs, not in its counts.
func TestNoVendorSecretLeavesForAnywhereButTheBackend(t *testing.T) {
	backend := newRecorder(t, nil)
	copyTo := newRecorder(t, nil)
	grpcVendor := grpcBackend(t, "0")
	routes := []Route{
		{Name: "http-app", Protocol: ProtocolOTLPHTTP, Source: "vendor", Target: backend.srv.URL + "/v1/traces?token=" + plantedToken},
		{Name: "grpc-app", Protocol: ProtocolOTLPGRPC, Source: "vendor", Target: grpcVendor.srv.URL},
		{Name: "zipkin-app", Protocol: ProtocolRelay, Target: backend.srv.URL + "/?token=" + plantedToken, Decode: "zipkin"},
	}
	var logs strings.Builder
	var counts []byte
	for _, rt := range routes {
		g := startGateway(t, rt, copyTo, gatewayOptions{})
		switch rt.Protocol {
		case ProtocolOTLPHTTP:
			do(t, http.DefaultClient, exportRequest(t, g.url+"/v1/traces", gzipped(t, otlpBatch), vendorHeaders()))
		case ProtocolOTLPGRPC:
			do(t, h2cClient(), grpcExport(t, g.url, grpcFrame(otlpBatch, false), http.Header{"Authorization": {"Api-Key " + plantedKey}}))
		case ProtocolRelay:
			do(t, http.DefaultClient, exportRequest(t, g.url+"/api/v2/spans", []byte(zipkinSpans), http.Header{
				"Content-Type": {"application/json"}, "X-Vendor-Key": {plantedKey}, "Authorization": {"Api-Key " + plantedKey},
			}))
		}
		eventually(t, rt.Name+" copied", func() bool { return g.stats.Snapshot()[rt.Name].Copied == 1 })
		logs.WriteString(g.logs.String())
		snap, _ := json.Marshal(g.stats.Snapshot())
		counts = append(counts, snap...)
	}

	// The backends got the key.
	for _, r := range append(backend.requests(), grpcVendor.requests()...) {
		if !strings.Contains(fmt.Sprint(r.Header), plantedKey) {
			t.Errorf("the backend did not get the workload's key: %v", r.Header)
		}
	}
	// impact.ai got none of it.
	copies := copyTo.requests()
	if len(copies) != 3 {
		t.Fatalf("%d copies, want 3", len(copies))
	}
	for _, c := range copies {
		assertNoSecret(t, "a copy's headers", fmt.Sprint(c.Header))
		assertNoSecret(t, "a copy's URL", c.Path+"?"+c.Query)
		assertNoSecret(t, "a copy's body", string(c.Body))
		if c.Header.Get("Authorization") != "" {
			t.Errorf("a copy carries an Authorization header without a key file: %q", c.Header.Get("Authorization"))
		}
	}
	assertNoSecret(t, "the gateway's logs", logs.String())
	assertNoSecret(t, "the gateway's counts", string(counts))
	if logs.Len() == 0 {
		t.Errorf("nothing was logged at debug level, so the scan proves nothing")
	}
}

func TestARouteFileErrorNeverRepeatsTheTarget(t *testing.T) {
	bad := []string{
		`{"routes":[{"name":"a","listen":":4318","protocol":"otlp-http","target":"ftp://vendor.example/?token=` + plantedToken + `"}]}`,
		`{"routes":[{"name":"a","listen":":4318","protocol":"otlp-grpc","target":"http://vendor.example:4317/path?token=` + plantedToken + `"}]}`,
		`{"routes":[{"name":"a","listen":":4318","protocol":"otlp-http","target":"http://` + plantedToken + `@/"}]}`,
		`{"routes":[{"name":"a","listen":":4318","protocol":"otlp-http","target":"http://vendor.example/` + plantedToken + "\x7f" + `"}]}`,
	}
	for _, doc := range bad {
		_, err := ParseRoutes([]byte(doc))
		if err == nil {
			t.Errorf("accepted %s", doc)
			continue
		}
		assertNoSecret(t, "the error", err.Error())
	}
	if _, err := ParseRoutes(bytes.Repeat([]byte("{"), 3)); err == nil {
		t.Errorf("accepted a broken document")
	}
}
