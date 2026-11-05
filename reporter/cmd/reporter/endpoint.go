// SPDX-License-Identifier: Apache-2.0

package main

import (
	"fmt"
	"net"
	"net/url"
	"strings"
)

// checkIngestEndpoint says what is wrong with an ingest endpoint, or returns nil. The ingest key goes to it as a bearer
// token, and the install token of the exchange does too, so the scheme is https. Plain http is taken for a loopback
// address (a test server on the same machine), and anywhere else only when allowInsecure says so on purpose, as the
// end-to-end suite does for its mock ingest; the chart sets the flag only for ingest.allowInsecureHttp.
func checkIngestEndpoint(endpoint string, allowInsecure bool) error {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return fmt.Errorf("ingest endpoint: want https://host[:port], got %q (set --ingest-endpoint or IMPACT_INGEST_ENDPOINT)", endpoint)
	}
	if u.Scheme == "http" && !allowInsecure && !isLoopback(u.Hostname()) {
		return fmt.Errorf("ingest endpoint: %q is plain http, which would send the ingest key (or the install token) in clear; use https (a test server is allowed with --allow-insecure-ingest)", endpoint)
	}
	return nil
}

func isLoopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
