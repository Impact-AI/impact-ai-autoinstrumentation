// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const forwardPlantedKey = "planted-vendor-key-c0ffee"

func TestForwardUsageErrors(t *testing.T) {
	routes := filepath.Join(t.TempDir(), "routes.json")
	cases := map[string]struct {
		args []string
		want string
	}{
		"no copy endpoint":        {[]string{"--routes-file=" + routes}, "copy-endpoint"},
		"copy endpoint not a URL": {[]string{"--copy-endpoint=ingest", "--routes-file=" + routes}, "copy-endpoint"},
		"key in clear": {[]string{"--copy-endpoint=http://gateway.example:4318", "--copy-key-file=/k", "--routes-file=" + routes},
			"plain http"},
		"bad queue": {[]string{"--copy-endpoint=http://gateway.example:4318", "--queue-mib=0"}, "queue-mib"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			var stderr bytes.Buffer
			code := run(context.Background(), append([]string{"forward"}, tc.args...), func(string) string { return "" }, io.Discard, &stderr, deps{})
			if code != 2 || !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("code %d, stderr %q; want 2 and %q", code, stderr.String(), tc.want)
			}
		})
	}
	// The kit's own gateway in the cluster takes copies in clear without a key.
	if _, err := parseForward([]string{"--copy-endpoint=http://gateway.example:4318"}, func(string) string { return "" }, io.Discard); err != nil {
		t.Errorf("plain http without a key refused: %v", err)
	}
}

func TestForwardStopsWithOneWhenTheRoutesCannotBeRead(t *testing.T) {
	routes := filepath.Join(t.TempDir(), "routes.json")
	if err := os.WriteFile(routes, []byte(`{"routes":[{"name":"a","listen":":0","protocol":"otlp-http","target":"ftp://x/?token=`+forwardPlantedKey+`"}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	code := run(context.Background(), []string{"forward", "--copy-endpoint=http://127.0.0.1:1", "--routes-file=" + routes, "--health-listen=127.0.0.1:0"},
		func(string) string { return "" }, io.Discard, &stderr, deps{})
	if code != 1 || !strings.Contains(stderr.String(), "cannot read the routes") {
		t.Errorf("code %d, stderr %s", code, stderr.String())
	}
	if strings.Contains(stderr.String(), forwardPlantedKey) {
		t.Errorf("the target's token reached the log")
	}
}

func TestForwardCommandForwardsFirstAndCopiesAfter(t *testing.T) {
	var mu sync.Mutex
	var vendorSaw, copies []http.Header
	vendor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.ReadAll(r.Body)
		mu.Lock()
		vendorSaw = append(vendorSaw, r.Header.Clone())
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer vendor.Close()
	impact := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.ReadAll(r.Body)
		mu.Lock()
		copies = append(copies, r.Header.Clone())
		mu.Unlock()
	}))
	defer impact.Close()

	dir := t.TempDir()
	routes := filepath.Join(dir, "routes.json")
	doc := fmt.Sprintf(`{"routes":[{"name":"checkout","listen":"127.0.0.1:0","protocol":"otlp-http","source":"vendor","target":%q}]}`, vendor.URL+"/v1/traces?token="+forwardPlantedKey)
	if err := os.WriteFile(routes, []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	stderr := &syncBuffer{}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() {
		done <- run(ctx, []string{"forward", "--copy-endpoint=" + impact.URL, "--routes-file=" + routes, "--health-listen=127.0.0.1:0"},
			func(string) string { return "" }, io.Discard, stderr, deps{})
	}()

	var listen, health string
	deadline := time.Now().Add(5 * time.Second)
	for listen == "" || health == "" {
		if time.Now().After(deadline) {
			cancel()
			t.Fatalf("forward did not start: %s", stderr.String())
		}
		for line := range strings.SplitSeq(stderr.String(), "\n") {
			var rec struct {
				Msg, Listen, Health string
			}
			if json.Unmarshal([]byte(line), &rec) == nil {
				switch rec.Msg {
				case "route started":
					listen = rec.Listen
				case "forward started":
					health = rec.Health
				}
			}
		}
		time.Sleep(5 * time.Millisecond)
	}

	req, _ := http.NewRequest(http.MethodPost, "http://"+listen+"/v1/traces", strings.NewReader(`{"resourceSpans":[{"resource":{},"scopeSpans":[]}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Api-Key "+forwardPlantedKey)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}

	var counts struct {
		Routes map[string]struct{ Copied int } `json:"routes"`
	}
	deadline = time.Now().Add(5 * time.Second)
	for counts.Routes["checkout"].Copied != 1 {
		if time.Now().After(deadline) {
			t.Fatalf("no copy counted: %+v", counts)
		}
		time.Sleep(10 * time.Millisecond)
		if r, err := http.Get("http://" + health + "/counts"); err == nil {
			_ = json.NewDecoder(r.Body).Decode(&counts)
			_ = r.Body.Close()
		}
	}
	if r, err := http.Get("http://" + health + "/readyz"); err != nil || r.StatusCode != http.StatusOK {
		t.Errorf("not ready")
	}

	cancel()
	if code := <-done; code != 0 {
		t.Errorf("exit code %d", code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(vendorSaw) != 1 || vendorSaw[0].Get("Authorization") != "Api-Key "+forwardPlantedKey {
		t.Errorf("the vendor did not get the workload's key: %v", vendorSaw)
	}
	if len(copies) != 1 || copies[0].Get("Authorization") != "" || !strings.HasPrefix(copies[0].Get("User-Agent"), "impact-forward/") {
		t.Errorf("copies %v", copies)
	}
	if strings.Contains(stderr.String(), forwardPlantedKey) {
		t.Errorf("the planted key or token reached the log")
	}
}
