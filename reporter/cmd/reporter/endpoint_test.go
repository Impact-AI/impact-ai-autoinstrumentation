// SPDX-License-Identifier: Apache-2.0

package main

import (
	"io"
	"strings"
	"testing"
)

func TestPlainHTTPIngestIsRefusedAnywhereButOnThisMachineUnlessAllowed(t *testing.T) {
	cases := []struct {
		endpoint string
		allow    bool
		want     string // a substring of the error; empty means accepted
	}{
		{"https://ingest.impact.ai", false, ""},
		{"https://ingest.example.test:8443", false, ""},
		{"http://ingest.impact.ai", false, "plain http"},
		{"http://mock.impact-mock.svc:8080", false, "plain http"},
		{"http://203.0.113.5:8080", false, "plain http"},
		{"http://mock.impact-mock.svc:8080", true, ""},
		{"http://127.0.0.1:8080", false, ""},
		{"http://localhost:8080", false, ""},
		{"http://[::1]:8080", false, ""},
		// a name that only begins like a loopback address is an address somewhere else
		{"http://127.0.0.1.attacker.example", false, "plain http"},
		{"http://localhost.attacker.example", false, "plain http"},
		{"ftp://ingest.impact.ai", false, "ingest endpoint"},
		{"https://", false, "ingest endpoint"},
		{"ingest.impact.ai", false, "ingest endpoint"},
		{"", false, "ingest endpoint"},
	}
	for _, tc := range cases {
		err := checkIngestEndpoint(tc.endpoint, tc.allow)
		switch {
		case tc.want == "" && err != nil:
			t.Errorf("%q (allow %v): %v", tc.endpoint, tc.allow, err)
		case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
			t.Errorf("%q (allow %v): error %v, want one with %q", tc.endpoint, tc.allow, err, tc.want)
		}
	}
}

func TestEveryCommandThatSendsAKeyOrATokenRefusesPlainHTTPUnlessAllowedOnPurpose(t *testing.T) {
	const plain = "http://mock.impact-mock.svc:8080"
	noEnv := func(string) string { return "" }
	allowedByEnv := func(name string) string {
		if name == "IMPACT_ALLOW_INSECURE_INGEST" {
			return "true"
		}
		return ""
	}
	commands := map[string]func(endpoint string, allow bool) error{
		"the reporter": func(endpoint string, allow bool) error {
			args := []string{"--ingest-endpoint=" + endpoint}
			if allow {
				args = append(args, "--allow-insecure-ingest")
			}
			_, err := ParseConfig(args, noEnv, io.Discard)
			return err
		},
		"the reporter, allowed by its environment": func(endpoint string, allow bool) error {
			getenv := noEnv
			if allow {
				getenv = allowedByEnv
			}
			_, err := ParseConfig([]string{"--ingest-endpoint=" + endpoint}, getenv, io.Discard)
			return err
		},
		"the uninstall notice": func(endpoint string, allow bool) error {
			args := []string{"--uninstall-notice", "--ingest-endpoint=" + endpoint}
			if allow {
				args = append(args, "--allow-insecure-ingest")
			}
			_, err := ParseConfig(args, noEnv, io.Discard)
			return err
		},
		"the exchange": func(endpoint string, allow bool) error {
			args := []string{
				"--ingest-endpoint=" + endpoint, "--product-id=prod_e2e", "--secret-name=impact-ai-install-key",
				"--namespace=impact-ai", "--cluster-uid=4e5622e7-f420-499c-9662-50af7f8a64b9",
			}
			if allow {
				args = append(args, "--allow-insecure-ingest")
			}
			_, err := parseExchange(args, noEnv, io.Discard)
			return err
		},
		"the cover pod": func(endpoint string, allow bool) error {
			args := []string{"--ingest-endpoint=" + endpoint, "--instrumentation=impact-ai/impact"}
			if allow {
				args = append(args, "--allow-insecure-ingest")
			}
			_, err := parseCover(args, noEnv, io.Discard)
			return err
		},
	}
	for name, parse := range commands {
		t.Run(name, func(t *testing.T) {
			if err := parse("https://ingest.example.test", false); err != nil {
				t.Errorf("https was refused: %v", err)
			}
			if err := parse("http://127.0.0.1:8080", false); err != nil {
				t.Errorf("a server on this machine was refused: %v", err)
			}
			err := parse(plain, false)
			if err == nil || !strings.Contains(err.Error(), "plain http") || !strings.Contains(err.Error(), "ingest endpoint") {
				t.Errorf("plain http to %s: error %v, want it refused and said so", plain, err)
			}
			if err := parse(plain, true); err != nil {
				t.Errorf("plain http was refused although it was allowed on purpose: %v", err)
			}
		})
	}
}
