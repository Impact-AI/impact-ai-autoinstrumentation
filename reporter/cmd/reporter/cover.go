// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/cover"
)

const (
	defaultCoverListen       = ":9443"
	defaultCoverHealthListen = ":8081"
	defaultCoverCertDir      = "/var/run/secrets/impact-cover-tls"
	defaultCoverStateDir     = "/var/lib/impact-cover"
	defaultCoverPollInterval = time.Minute
	minCoverPollInterval     = 5 * time.Second
)

var (
	namespacePattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	resourcePattern  = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)
)

// coverConfig is the configuration of the cover command. Every flag has an environment fallback; a flag wins.
type coverConfig struct {
	IngestEndpoint  string
	IngestKeyFile   string
	Instrumentation string // <namespace>/<name> of the Instrumentation resource the Operator is asked to use
	Listen          string // the TLS webhook
	HealthListen    string // plain HTTP: /healthz and /readyz
	CertDir         string
	StateDir        string
	PollInterval    time.Duration
	ShowVersion     bool
}

func parseCover(args []string, getenv func(string) string, stderr io.Writer) (coverConfig, error) {
	var problems []error
	str := func(fallback string, names ...string) string {
		for _, n := range names {
			if v := getenv(n); v != "" {
				return v
			}
		}
		return fallback
	}
	fs := flag.NewFlagSet("reporter cover", flag.ContinueOnError)
	fs.SetOutput(stderr)
	endpoint := fs.String("ingest-endpoint", str("", "IMPACT_INGEST_ENDPOINT"), "base URL of ingest, for example https://ingest.impact.ai (env IMPACT_INGEST_ENDPOINT)")
	keyFile := fs.String("ingest-key-file", str(defaultKeyFile, "IMPACT_INGEST_KEY_FILE"), "file holding the install key, read again at every poll (env IMPACT_INGEST_KEY_FILE)")
	instrumentation := fs.String("instrumentation", str("", "IMPACT_INSTRUMENTATION"), "the Instrumentation resource to ask the Operator for, <namespace>/<name>, for example impact-ai/impact (env IMPACT_INSTRUMENTATION)")
	listen := fs.String("listen", str(defaultCoverListen, "IMPACT_COVER_LISTEN"), "address of the TLS webhook (env IMPACT_COVER_LISTEN)")
	healthListen := fs.String("health-listen", str(defaultCoverHealthListen, "IMPACT_COVER_HEALTH_LISTEN"), "address for /healthz and /readyz, plain HTTP (env IMPACT_COVER_HEALTH_LISTEN)")
	certDir := fs.String("cert-dir", str(defaultCoverCertDir, "IMPACT_COVER_CERT_DIR"), "directory with tls.crt and tls.key of the webhook, read again when they change (env IMPACT_COVER_CERT_DIR)")
	stateDir := fs.String("state-dir", str(defaultCoverStateDir, "IMPACT_COVER_STATE_DIR"), "directory where the last list is kept for a restarted container (env IMPACT_COVER_STATE_DIR)")
	interval := fs.String("poll-interval", str(defaultCoverPollInterval.String(), "IMPACT_COVER_POLL_INTERVAL"), "time between polls of the list, at least 5s (env IMPACT_COVER_POLL_INTERVAL)")
	showVersion := fs.Bool("version", false, "print the version and exit")
	allowInsecureIngest := fs.Bool("allow-insecure-ingest", false, "accept a plain http ingest endpoint that is not on this machine: the key would travel in clear (tests and development only)")
	if err := fs.Parse(args); err != nil {
		return coverConfig{}, err
	}

	cfg := coverConfig{
		IngestEndpoint:  *endpoint,
		IngestKeyFile:   *keyFile,
		Instrumentation: *instrumentation,
		Listen:          *listen,
		HealthListen:    *healthListen,
		CertDir:         *certDir,
		StateDir:        *stateDir,
		ShowVersion:     *showVersion,
	}
	if cfg.ShowVersion {
		return cfg, nil
	}
	if err := checkIngestEndpoint(cfg.IngestEndpoint, *allowInsecureIngest); err != nil {
		problems = append(problems, err)
	}
	namespace, name, found := strings.Cut(cfg.Instrumentation, "/")
	if !found || len(namespace) > 63 || !namespacePattern.MatchString(namespace) || len(name) > 253 || !resourcePattern.MatchString(name) {
		problems = append(problems, fmt.Errorf("instrumentation: want <namespace>/<name>, got %q (set --instrumentation or IMPACT_INSTRUMENTATION)", cfg.Instrumentation))
	}
	d, err := time.ParseDuration(*interval)
	switch {
	case err != nil:
		problems = append(problems, fmt.Errorf("poll-interval: %q is not a duration such as 1m", *interval))
	case d < minCoverPollInterval:
		problems = append(problems, fmt.Errorf("poll-interval: %v is shorter than the minimum %v", d, minCoverPollInterval))
	}
	cfg.PollInterval = d
	if len(problems) > 0 {
		return coverConfig{}, errors.Join(problems...)
	}
	return cfg, nil
}

// runCover is the `cover` command: a mutating admission webhook that adds the injection annotation to the pods of the
// workloads impact.ai lists, and the poller that keeps that list. It has no Kubernetes client at all. It returns the
// exit code: 0 after a clean shutdown, 1 when it cannot start (a certificate that is not there, a port that is taken),
// 2 for a usage error.
func runCover(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer, d deps) int {
	cfg, err := parseCover(args, getenv, stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(stderr, "reporter cover:", err)
		return 2
	}
	if cfg.ShowVersion {
		fmt.Fprintf(stdout, "impact-cover %s\n", version)
		return 0
	}
	if d.now == nil {
		d.now = time.Now
	}
	log := slog.New(slog.NewJSONHandler(stderr, nil))

	certs, err := cover.NewCertReloader(cfg.CertDir)
	if err != nil {
		log.Error("cannot load the webhook certificate", "error", err)
		return 1
	}
	webhookListener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		log.Error("cannot listen for webhook requests", "address", cfg.Listen, "error", err)
		return 1
	}
	healthListener, err := net.Listen("tcp", cfg.HealthListen)
	if err != nil {
		_ = webhookListener.Close()
		log.Error("cannot listen for health probes", "address", cfg.HealthListen, "error", err)
		return 1
	}

	store := &cover.Store{}
	decider := &cover.Decider{Store: store, Instrumentation: cfg.Instrumentation, Now: d.now, Log: log}
	poller := &cover.Poller{
		Endpoint:  cfg.IngestEndpoint,
		KeyFile:   cfg.IngestKeyFile,
		StateDir:  cfg.StateDir,
		Interval:  cfg.PollInterval,
		UserAgent: "impact-cover/" + version,
		Store:     store,
		Now:       d.now,
		Sleep:     d.sleep,
		Jitter:    d.jitter,
		Log:       log,
	}

	var ready readiness
	webhook := &http.Server{
		Handler:           cover.NewHandler(decider, log),
		TLSConfig:         certs.TLSConfig(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
		// A failed TLS handshake is the API server's business, not an error of ours.
		ErrorLog: slog.NewLogLogger(log.Handler(), slog.LevelDebug),
	}
	health := &http.Server{Handler: healthHandler(&ready), ReadHeaderTimeout: 5 * time.Second}

	failed := make(chan error, 3)
	serve := func(name string, run func() error) {
		go func() {
			if err := run(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				failed <- fmt.Errorf("%s: %w", name, err)
			}
		}()
	}
	// The addresses are logged before anything runs: they are the only way to learn a port chosen with ":0".
	log.Info("cover started", "version", version, "webhook", webhookListener.Addr().String(), "health", healthListener.Addr().String(),
		"instrumentation", cfg.Instrumentation, "ingest", cfg.IngestEndpoint, "pollInterval", cfg.PollInterval.String())
	serve("webhook server", func() error { return webhook.ServeTLS(webhookListener, "", "") })
	serve("health server", func() error { return health.Serve(healthListener) })
	pollCtx, stopPolling := context.WithCancel(ctx)
	defer stopPolling()
	go func() {
		if err := poller.Run(pollCtx); err != nil && !errors.Is(err, context.Canceled) {
			failed <- fmt.Errorf("list poller: %w", err)
		}
	}()
	ready.set()

	code := 0
	select {
	case <-ctx.Done():
	case err := <-failed:
		log.Error("cover stopped", "error", err)
		code = 1
	}
	stopPolling()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = webhook.Shutdown(shutdown)
	_ = health.Shutdown(shutdown)
	log.Info("cover stopped")
	return code
}
