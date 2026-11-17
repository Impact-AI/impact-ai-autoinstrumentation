// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/forward"
)

const (
	defaultForwardRoutesFile   = "/etc/impact-forward/routes.json"
	defaultForwardHealthListen = ":8082"
	defaultForwardQueueMiB     = forward.DefaultQueueBytes >> 20
	forwardCountsEvery         = time.Minute
)

// forwardConfig is the configuration of the forward command. Every flag has an environment fallback; a flag wins.
type forwardConfig struct {
	RoutesFile   string
	CopyEndpoint string
	CopyKeyFile  string
	HealthListen string
	QueueBytes   int64
	ShowVersion  bool
}

func parseForward(args []string, getenv func(string) string, stderr io.Writer) (forwardConfig, error) {
	str := func(fallback, name string) string {
		if v := getenv(name); v != "" {
			return v
		}
		return fallback
	}
	fs := flag.NewFlagSet("reporter forward", flag.ContinueOnError)
	fs.SetOutput(stderr)
	routes := fs.String("routes-file", str(defaultForwardRoutesFile, "IMPACT_FORWARD_ROUTES_FILE"), "the routes: each switched workload's listen address, protocol and original target (env IMPACT_FORWARD_ROUTES_FILE)")
	endpoint := fs.String("copy-endpoint", str("", "IMPACT_COPY_ENDPOINT"), "base URL of the OTLP/HTTP receiver the copies go to, for example the kit's gateway (env IMPACT_COPY_ENDPOINT)")
	keyFile := fs.String("copy-key-file", str("", "IMPACT_COPY_KEY_FILE"), "file holding the install key sent with each copy, read again at every attempt; empty sends none (env IMPACT_COPY_KEY_FILE)")
	health := fs.String("health-listen", str(defaultForwardHealthListen, "IMPACT_FORWARD_HEALTH_LISTEN"), "address for /healthz, /readyz and /counts, plain HTTP (env IMPACT_FORWARD_HEALTH_LISTEN)")
	queueMiB := fs.String("queue-mib", str(strconv.Itoa(defaultForwardQueueMiB), "IMPACT_FORWARD_QUEUE_MIB"), "the most MiB of batches waiting for impact.ai, the oldest dropped and counted beyond it, and as much again for bodies held while they are forwarded (env IMPACT_FORWARD_QUEUE_MIB)")
	showVersion := fs.Bool("version", false, "print the version and exit")
	allowInsecure := fs.Bool("allow-insecure-copy", false, "send the install key to a plain http copy endpoint that is not on this machine (tests and development only)")
	if err := fs.Parse(args); err != nil {
		return forwardConfig{}, err
	}
	cfg := forwardConfig{RoutesFile: *routes, CopyEndpoint: *endpoint, CopyKeyFile: *keyFile, HealthListen: *health, ShowVersion: *showVersion}
	if cfg.ShowVersion {
		return cfg, nil
	}
	var problems []error
	u, err := url.Parse(cfg.CopyEndpoint)
	switch {
	case err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "":
		problems = append(problems, fmt.Errorf("copy-endpoint: want http(s)://host[:port], got %q (set --copy-endpoint or IMPACT_COPY_ENDPOINT)", cfg.CopyEndpoint))
	case u.Scheme == "http" && cfg.CopyKeyFile != "" && !*allowInsecure && !isLoopback(u.Hostname()):
		problems = append(problems, fmt.Errorf("copy-endpoint: %q is plain http, which would send the install key in clear; use https, or no key for the kit's own gateway", cfg.CopyEndpoint))
	}
	mib, err := strconv.Atoi(*queueMiB)
	if err != nil || mib < 1 || mib > 4096 {
		problems = append(problems, fmt.Errorf("queue-mib: want a whole number from 1 to 4096, got %q", *queueMiB))
	}
	cfg.QueueBytes = int64(mib) << 20
	if len(problems) > 0 {
		return forwardConfig{}, errors.Join(problems...)
	}
	return cfg, nil
}

// runForward is the `forward` command: the forward-first gateway of switched workloads. Each route listens on its own
// address and passes what arrives to the workload's original target; what the backend accepted is copied to impact.ai
// in the background. Nothing runs it until a workload is switched to it. It returns the exit code: 0 after a clean
// shutdown, 1 when it cannot start (routes that cannot be read, a port that is taken), 2 for a usage error.
func runForward(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer, d deps) int {
	cfg, err := parseForward(args, getenv, stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(stderr, "reporter forward:", err)
		return 2
	}
	if cfg.ShowVersion {
		fmt.Fprintf(stdout, "impact-forward %s\n", version)
		return 0
	}
	log := slog.New(slog.NewJSONHandler(stderr, nil))
	routes, err := forward.LoadRoutes(cfg.RoutesFile)
	if err != nil {
		log.Error("cannot read the routes", "error", err)
		return 1
	}

	stats := forward.NewStats()
	queue := forward.NewQueue(cfg.QueueBytes, stats)
	// The bodies held for copies while they are forwarded are bounded as the queue is, across every route.
	captures := forward.NewCaptureBudget(cfg.QueueBytes)
	var servers []*http.Server
	closeAll := func() {
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, s := range servers {
			_ = s.Shutdown(shutdown)
		}
	}
	failed := make(chan error, len(routes)+1)
	serve := func(name string, srv *http.Server, ln net.Listener) {
		servers = append(servers, srv)
		go func() {
			if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
				failed <- fmt.Errorf("%s: %w", name, err)
			}
		}()
	}

	var ready readiness
	healthLn, err := net.Listen("tcp", cfg.HealthListen)
	if err != nil {
		log.Error("cannot listen for health probes", "address", cfg.HealthListen, "error", err)
		return 1
	}
	serve("health server", &http.Server{Handler: forwardHealth(&ready, stats, queue), ReadHeaderTimeout: 5 * time.Second}, healthLn)

	for _, rt := range routes {
		h, err := forward.NewHandler(rt, forward.Options{Queue: queue, Stats: stats, Captures: captures, Log: log})
		if err == nil {
			var ln net.Listener
			if ln, err = net.Listen("tcp", rt.Listen); err == nil {
				serve("route "+rt.Name, forward.NewServer(h, log), ln)
				// The address is logged: it is the only way to learn a port chosen with ":0". The backend is named by
				// its host alone, because the target can hold a token.
				log.Info("route started", "route", rt.Name, "listen", ln.Addr().String(), "protocol", string(rt.Protocol), "backend", rt.TargetHost())
				continue
			}
		}
		log.Error("cannot start a route", "route", rt.Name, "listen", rt.Listen, "error", err)
		closeAll()
		return 1
	}

	copier := &forward.Copier{
		Endpoint:  cfg.CopyEndpoint,
		KeyFile:   cfg.CopyKeyFile,
		Queue:     queue,
		Stats:     stats,
		UserAgent: "impact-forward/" + version,
		Sleep:     d.sleep,
		Log:       log,
	}
	copyCtx, stopCopying := context.WithCancel(ctx)
	defer stopCopying()
	go func() {
		if err := copier.Run(copyCtx); err != nil && !errors.Is(err, context.Canceled) {
			failed <- fmt.Errorf("copier: %w", err)
		}
	}()
	log.Info("forward started", "version", version, "routes", len(routes), "health", healthLn.Addr().String(),
		"copyTo", copyHost(cfg.CopyEndpoint), "queueBytes", cfg.QueueBytes)
	ready.set()

	ticker := time.NewTicker(forwardCountsEvery)
	defer ticker.Stop()
	var last map[string]forward.Counts
	code := 0
loop:
	for {
		select {
		case <-ctx.Done():
			break loop
		case err := <-failed:
			log.Error("forward stopped", "error", err)
			code = 1
			break loop
		case <-ticker.C:
			if now := stats.Snapshot(); !maps.Equal(now, last) {
				log.Info("forward counts", "routes", now, "queueBytes", queue.Bytes())
				last = now
			}
		}
	}
	// The exporters' requests in flight finish before the gateway goes: a rollout of the gateway loses no span.
	closeAll()
	stopCopying()
	log.Info("forward stopped", "routes", stats.Snapshot(), "queuedBatchesLeft", queue.Len())
	return code
}

func copyHost(endpoint string) string {
	if u, err := url.Parse(endpoint); err == nil {
		return u.Host
	}
	return ""
}

// forwardHealth serves /healthz, /readyz (once every route listens) and /counts: per route, numbers only.
func forwardHealth(ready *readiness, stats *forward.Stats, queue *forward.Queue) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/", healthHandler(ready))
	mux.HandleFunc("/counts", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"routes":       stats.Snapshot(),
			"queueBytes":   queue.Bytes(),
			"queueBatches": queue.Len(),
		})
	})
	return mux
}
