// SPDX-License-Identifier: Apache-2.0

// Command reporter lists a cluster's workloads by name and sends the inventory
// to impact.ai. It runs in the customer's cluster with read-only access and
// never sends values: see internal/inventory.
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/injector"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/inventory"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kube"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kubesecret"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/loop"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/notice"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/sender"
)

// version is set at build time: -ldflags "-X main.version=v0.1.0".
var version = "dev"

// deps are the parts of the environment tests replace. Nil means the real one.
type deps struct {
	now    func() time.Time
	sleep  func(ctx context.Context, d time.Duration) error
	jitter func() float64
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Getenv, os.Stdout, os.Stderr, deps{now: time.Now})
	stop()
	os.Exit(code)
}

// readiness becomes true once the first snapshot has been built.
type readiness struct{ v atomic.Bool }

func (r *readiness) set()        { r.v.Store(true) }
func (r *readiness) isSet() bool { return r.v.Load() }

// healthHandler serves /healthz (always 200) and /readyz (200 once a snapshot
// was built, so a pod without working Kubernetes access is not ready).
func healthHandler(ready *readiness) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !ready.isSet() {
			http.Error(w, "no snapshot built yet", http.StatusServiceUnavailable)
			return
		}
		_, _ = io.WriteString(w, "ok\n")
	})
	return mux
}

// newID returns a random UUID (version 4).
func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("reporter-%d", time.Now().UnixNano())
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func kubeConfig(cfg Config, getenv func(string) string) (kube.Config, error) {
	var kc kube.Config
	if cfg.APIServer != "" {
		kc.Host = cfg.APIServer
	} else {
		var err error
		if kc, err = kube.InClusterConfig(getenv); err != nil {
			return kc, err
		}
	}
	if cfg.TokenFile != "" {
		kc.TokenFile = cfg.TokenFile
	}
	if cfg.CAFile != "" {
		kc.CAFile = cfg.CAFile
	}
	kc.Insecure = cfg.InsecureSkipTLSVerify
	kc.UserAgent = "impact-reporter/" + version
	return kc, nil
}

// run is main without the process: it returns the exit code. 0 is a clean exit,
// 1 a runtime failure and 2 a usage error.
func run(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer, d deps) int {
	// Three more commands share the binary and the image: the install token exchange (a Helm pre-install and pre-upgrade
	// hook), the cover pod (automatic coverage) and the forward-first gateway of switched workloads. Everything else is
	// the reporter itself.
	if len(args) > 0 {
		switch args[0] {
		case "exchange":
			return runExchange(ctx, args[1:], getenv, stdout, stderr, d)
		case "cover":
			return runCover(ctx, args[1:], getenv, stdout, stderr, d)
		case "forward":
			return runForward(ctx, args[1:], getenv, stdout, stderr, d)
		}
	}
	cfg, err := ParseConfig(args, getenv, stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(stderr, "reporter:", err)
		return 2
	}
	if cfg.ShowVersion {
		fmt.Fprintf(stdout, "impact-reporter %s\n", version)
		return 0
	}
	if d.now == nil {
		d.now = time.Now
	}
	log := slog.New(slog.NewJSONHandler(stderr, nil))

	// The uninstall notice runs in a Helm hook and must never block the uninstall,
	// so for it an operational failure ends with exit code 0. Only usage errors,
	// which were reported above, exit with 2.
	failed := 1
	if cfg.UninstallNotice && !cfg.DryRun {
		failed = 0
	}

	kc, err := kubeConfig(cfg, getenv)
	if err != nil {
		log.Error("cannot reach the Kubernetes API", "error", err)
		return failed
	}
	client, err := kube.New(kc)
	if err != nil {
		log.Error("cannot set up the Kubernetes client", "error", err)
		return failed
	}
	instance := cfg.PodUID
	if instance == "" {
		instance = newID()
	}
	collector := inventory.NewCollector(client, inventory.Options{
		ClusterName: cfg.ClusterName,
		Cloud: inventory.Cloud{
			Provider:  cfg.CloudProvider,
			AccountID: cfg.CloudAccountID,
			Region:    cfg.CloudRegion,
			ClusterID: cfg.ClusterARN,
		},
		Include:           cfg.IncludeNamespaces,
		Exclude:           cfg.ExcludeNamespaces,
		OwnNamespace:      cfg.OwnNamespace,
		Release:           injector.Release{Name: cfg.ReleaseName, Namespace: cfg.ReleaseNamespace},
		InjectionDisabled: cfg.InjectionDisabled,
		CoverFields:       cfg.CoverFields,
		Version:           version,
		Instance:          instance,
		Now:               d.now,
		Log:               log,
	})

	if cfg.UninstallNotice {
		return uninstallNotice(ctx, cfg, kc, client, collector, stdout, log, d)
	}
	if cfg.DryRun {
		return dryRun(ctx, collector, stdout, log, d)
	}

	var ready readiness
	if cfg.Listen != "" {
		listener, err := net.Listen("tcp", cfg.Listen)
		if err != nil {
			log.Error("cannot listen for health probes", "address", cfg.Listen, "error", err)
			return 1
		}
		srv := &http.Server{Handler: healthHandler(&ready), ReadHeaderTimeout: 5 * time.Second}
		go func() {
			if err := srv.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Error("health server stopped", "error", err)
			}
		}()
		defer func() {
			shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = srv.Shutdown(shutdown)
		}()
	}

	rep := loop.New(loop.Reporter{
		Snapshots: collector,
		Sender: &sender.Sender{
			Endpoint:  cfg.IngestEndpoint,
			UserAgent: "impact-reporter/" + version,
			Interval:  cfg.Interval,
			Sleep:     d.sleep,
			Jitter:    d.jitter,
			Log:       log,
		},
		KeyFile:  cfg.IngestKeyFile,
		Interval: cfg.Interval,
		OnBuilt:  ready.set,
		Now:      d.now,
		Sleep:    d.sleep,
		Log:      log,
	})
	log.Info("reporter started", "version", version, "interval", cfg.Interval.String(), "ingest", cfg.IngestEndpoint,
		"injection", !cfg.InjectionDisabled, "release", cfg.ReleaseName)
	if err := rep.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		log.Error("reporter stopped", "error", err)
		return 1
	}
	log.Info("reporter stopped")
	return 0
}

// uninstallNotice stops the reporter and sends the final snapshot, or with
// --dry-run prints that snapshot and changes nothing. The real run always
// returns 0: whatever fails is logged, and the uninstall goes on.
func uninstallNotice(ctx context.Context, cfg Config, kc kube.Config, client *kube.Client, collector *inventory.Collector, stdout io.Writer, log *slog.Logger, d deps) int {
	if cfg.DryRun {
		ctx, cancel := context.WithTimeout(ctx, cfg.NoticeTimeout)
		defer cancel()
		snap, err := collector.UninstallNotice(ctx, d.now().Unix())
		if err != nil {
			log.Error("could not build the uninstall notice", "error", err)
			return 1
		}
		out, err := json.MarshalIndent(snap, "", "  ")
		if err != nil {
			log.Error("could not print the uninstall notice", "error", err)
			return 1
		}
		fmt.Fprintf(stdout, "%s\n", out)
		return 0
	}
	runner := &notice.Runner{
		Kube:    client,
		Builder: collector,
		Ingest: sender.Sender{
			Endpoint:  cfg.IngestEndpoint,
			UserAgent: "impact-reporter/" + version,
			Jitter:    d.jitter,
			Log:       log,
		},
		KeyFile: cfg.IngestKeyFile,
		Options: notice.Options{
			StopDeployment: cfg.StopDeployment,
			StopNamespace:  cfg.StopNamespace,
			PodSelector:    cfg.StopPodSelector,
			StopTimeout:    cfg.StopTimeout,
			NoticeTimeout:  cfg.NoticeTimeout,
			SelfUID:        cfg.PodUID,

			CoverDeployment:  cfg.CoverDeployment,
			CoverPodSelector: cfg.CoverPodSelector,
			KeySecret:        cfg.KeySecret,
		},
		Now:   d.now,
		Sleep: d.sleep,
		Log:   log,
	}
	if cfg.KeySecret != "" {
		// Not being able to delete the Secret is logged by the runner; not being able to even try must not stop the rest.
		if store, err := kubesecret.New(kc); err != nil {
			log.Warn("cannot set up the Kubernetes client for the key Secret; it stays", "error", err)
		} else {
			runner.Secrets = store
		}
	}
	res := runner.Run(ctx)
	log.Info("uninstall notice finished", "scale", res.Scale.String(), "wait", res.Wait.String(),
		"cover", res.Cover.String(), "keyDeleted", res.KeyDeleted,
		"sent", res.Attempted && res.Sent.Outcome == sender.Accepted)
	return 0
}

// dryRun builds one snapshot, prints what would be sent and exits.
func dryRun(ctx context.Context, collector *inventory.Collector, stdout io.Writer, log *slog.Logger, d deps) int {
	ctx, cancel := context.WithTimeout(ctx, loop.DefaultBuildTimeout)
	defer cancel()
	snap, err := collector.Snapshot(ctx, d.now().Unix())
	if err != nil {
		log.Error("could not build the inventory snapshot", "error", err)
		return 1
	}
	enc, err := inventory.Encode(snap)
	if err != nil {
		log.Error("could not encode the inventory snapshot", "error", err)
		return 1
	}
	if enc.Dropped > 0 {
		log.Warn("the snapshot was cut to the ingest limits (5,000 workloads, 2 MiB gzip)",
			"workloads", enc.Workloads, "dropped", enc.Dropped, "gzipBytes", len(enc.Body))
	}
	out, err := json.MarshalIndent(enc.Snapshot, "", "  ")
	if err != nil {
		log.Error("could not print the inventory snapshot", "error", err)
		return 1
	}
	fmt.Fprintf(stdout, "%s\n", out)
	return 0
}

// Compile-time check that the sender satisfies the loop's interface.
var _ loop.Poster = (*sender.Sender)(nil)
