// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/exchange"
	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kubesecret"
)

const (
	defaultInstallTokenFile = "/var/run/secrets/impact-install/token"
	defaultExchangeTimeout  = 2 * time.Minute

	// Exit codes of the exchange. 1 is a refusal or a failure that running the same command again will not fix; 3 is
	// impact.ai out of reach or busy, which it may.
	exitRefused     = 1
	exitUsage       = 2
	exitUnavailable = 3
)

var (
	secretNamePattern    = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$`)
	namespaceNamePattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)
)

// exchangeConfig is the configuration of the exchange command. Every flag has an environment fallback; a flag wins.
type exchangeConfig struct {
	IngestEndpoint string
	ProductID      string
	TokenFile      string
	SecretName     string
	Namespace      string
	ClusterUID     string
	ClusterName    string
	ClusterARN     string
	ChartVersion   string
	Timeout        time.Duration
	ShowVersion    bool

	APIServer             string
	APITokenFile          string
	CAFile                string
	InsecureSkipTLSVerify bool
}

func parseExchange(args []string, getenv func(string) string, stderr io.Writer) (exchangeConfig, error) {
	var problems []error
	str := func(fallback string, names ...string) string {
		for _, n := range names {
			if v := getenv(n); v != "" {
				return v
			}
		}
		return fallback
	}
	fs := flag.NewFlagSet("reporter exchange", flag.ContinueOnError)
	fs.SetOutput(stderr)
	endpoint := fs.String("ingest-endpoint", str("", "IMPACT_INGEST_ENDPOINT"), "base URL of ingest, for example https://ingest.impact.ai (env IMPACT_INGEST_ENDPOINT)")
	product := fs.String("product-id", str("", "IMPACT_PRODUCT_ID"), "the impact.ai product the install is for (env IMPACT_PRODUCT_ID)")
	tokenFile := fs.String("install-token-file", str(defaultInstallTokenFile, "IMPACT_INSTALL_TOKEN_FILE"), "file holding the install token of the command (env IMPACT_INSTALL_TOKEN_FILE)")
	secretName := fs.String("secret-name", str("", "IMPACT_KEY_SECRET"), "the Secret that keeps the ingest key this run makes: Helm made it empty before this step (env IMPACT_KEY_SECRET)")
	namespace := fs.String("namespace", str("", "IMPACT_OWN_NAMESPACE", "POD_NAMESPACE"), "namespace of that Secret (env POD_NAMESPACE)")
	clusterUID := fs.String("cluster-uid", str("", "IMPACT_CLUSTER_UID"), "the cluster's ID: the UID of its kube-system namespace (env IMPACT_CLUSTER_UID)")
	clusterName := fs.String("cluster-name", str("", "IMPACT_CLUSTER_NAME"), "optional cluster name (env IMPACT_CLUSTER_NAME)")
	clusterARN := fs.String("cluster-arn", str("", "IMPACT_CLUSTER_ARN"), "optional provider ID of the cluster, such as the EKS ARN (env IMPACT_CLUSTER_ARN)")
	chartVersion := fs.String("chart-version", str("", "IMPACT_CHART_VERSION"), "version of the chart that runs the exchange; defaults to this binary's (env IMPACT_CHART_VERSION)")
	timeout := fs.String("timeout", str(defaultExchangeTimeout.String(), "IMPACT_EXCHANGE_TIMEOUT"), "how long the whole run may take, retries included (env IMPACT_EXCHANGE_TIMEOUT)")
	showVersion := fs.Bool("version", false, "print the version and exit")
	allowInsecureIngest := fs.Bool("allow-insecure-ingest", false, "accept a plain http ingest endpoint that is not on this machine: the install token would travel in clear (tests and development only)")
	apiServer := fs.String("api-server", str("", "IMPACT_API_SERVER"), "API server URL; default is the in-cluster address (env IMPACT_API_SERVER)")
	apiToken := fs.String("token-file", str("", "IMPACT_TOKEN_FILE"), "bearer token file for the API server; default is the service account token (env IMPACT_TOKEN_FILE)")
	caFile := fs.String("ca-file", str("", "IMPACT_CA_FILE"), "CA bundle for the API server; default is the service account CA (env IMPACT_CA_FILE)")
	insecure := fs.Bool("insecure-skip-tls-verify", false, "do not verify the API server certificate (tests and development only)")
	if err := fs.Parse(args); err != nil {
		return exchangeConfig{}, err
	}

	cfg := exchangeConfig{
		IngestEndpoint: strings.TrimSpace(*endpoint), ProductID: strings.TrimSpace(*product), TokenFile: *tokenFile,
		SecretName: *secretName, Namespace: *namespace, ClusterUID: strings.TrimSpace(*clusterUID),
		ClusterName: strings.TrimSpace(*clusterName), ClusterARN: strings.TrimSpace(*clusterARN),
		ChartVersion: strings.TrimSpace(*chartVersion), ShowVersion: *showVersion,
		APIServer: *apiServer, APITokenFile: *apiToken, CAFile: *caFile, InsecureSkipTLSVerify: *insecure,
	}
	if cfg.ShowVersion {
		return cfg, nil
	}
	if cfg.ChartVersion == "" {
		cfg.ChartVersion = version
	}
	if err := checkIngestEndpoint(cfg.IngestEndpoint, *allowInsecureIngest); err != nil {
		problems = append(problems, err)
	}
	if cfg.ProductID == "" {
		problems = append(problems, errors.New("product id: --product-id is required"))
	}
	if !secretNamePattern.MatchString(cfg.SecretName) {
		problems = append(problems, fmt.Errorf("secret name: want a Secret name, got %q (set --secret-name)", cfg.SecretName))
	}
	if !namespaceNamePattern.MatchString(cfg.Namespace) {
		problems = append(problems, fmt.Errorf("namespace: want a namespace name, got %q (set --namespace or POD_NAMESPACE)", cfg.Namespace))
	}
	if cfg.ClusterUID == "" {
		problems = append(problems, errors.New("cluster uid: --cluster-uid is required"))
	}
	d, err := time.ParseDuration(*timeout)
	if err != nil || d <= 0 {
		problems = append(problems, fmt.Errorf("timeout: %q is not a positive duration such as 2m", *timeout))
	}
	cfg.Timeout = d
	if len(problems) > 0 {
		return exchangeConfig{}, errors.Join(problems...)
	}
	return cfg, nil
}

// runExchange is the `exchange` command, run by the Helm pre-install and pre-upgrade hook: it trades the install token
// for this cluster's own ingest key and keeps the key in a Secret. It returns 0 when the key is in place (or already
// was), 1 when impact.ai refused the token or the cluster would not take the key, 3 when impact.ai could not be
// reached (the same command can be run again), 2 for a usage error. The sentences for the customer go to stderr, which
// is what Helm prints of a failed hook.
func runExchange(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer, d deps) int {
	cfg, err := parseExchange(args, getenv, stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(stderr, "reporter exchange:", err)
		return exitUsage
	}
	if cfg.ShowVersion {
		fmt.Fprintf(stdout, "impact-reporter %s\n", version)
		return 0
	}
	log := slog.New(slog.NewJSONHandler(stderr, nil))

	// The token is read before anything is touched; a command cut short has none worth sending.
	raw, err := os.ReadFile(cfg.TokenFile)
	if err != nil {
		fmt.Fprintf(stderr, "reporter exchange: cannot read the install token: %v\n", err)
		return exitUsage
	}
	token := strings.TrimSpace(string(raw))

	kc, err := kubeConfig(Config{APIServer: cfg.APIServer, TokenFile: cfg.APITokenFile, CAFile: cfg.CAFile, InsecureSkipTLSVerify: cfg.InsecureSkipTLSVerify}, getenv)
	if err != nil {
		log.Error("cannot reach the Kubernetes API", "error", err)
		return exitRefused
	}
	store, err := kubesecret.New(kc)
	if err != nil {
		log.Error("cannot set up the Kubernetes client", "error", err)
		return exitRefused
	}
	res := exchange.Run(ctx, exchange.Options{
		Endpoint:     cfg.IngestEndpoint,
		ProductID:    cfg.ProductID,
		Token:        token,
		ClusterUID:   cfg.ClusterUID,
		ClusterName:  cfg.ClusterName,
		ClusterARN:   cfg.ClusterARN,
		ChartVersion: cfg.ChartVersion,
		Namespace:    cfg.Namespace,
		SecretName:   cfg.SecretName,
		Secrets:      store,
		Budget:       cfg.Timeout,
		Out:          stderr,
		Log:          log,
		Now:          d.now,
		Sleep:        d.sleep,
		Jitter:       d.jitter,
	})
	switch res.Outcome {
	case exchange.Exchanged, exchange.Skipped:
		return 0
	case exchange.Unavailable:
		return exitUnavailable
	}
	return exitRefused
}
