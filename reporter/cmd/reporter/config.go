// SPDX-License-Identifier: Apache-2.0

package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"
)

const (
	defaultKeyFile  = "/var/run/secrets/impact/ingest-key"
	defaultInterval = 5 * time.Minute
	minInterval     = 5 * time.Second
	defaultListen   = ":8080"
	// defaultStopTimeout and defaultNoticeTimeout bound the uninstall notice.
	defaultStopTimeout   = 60 * time.Second
	defaultNoticeTimeout = 60 * time.Second
)

// Config is the reporter's configuration. Every flag has an environment
// fallback; a flag on the command line wins.
type Config struct {
	IngestEndpoint string
	IngestKeyFile  string
	Interval       time.Duration
	ClusterName    string
	// The cloud identity of the cluster, as the chart was given it; all optional.
	CloudProvider     string // aws, azure or gcp
	CloudAccountID    string
	CloudRegion       string
	ClusterARN        string // the provider's own ID of the cluster, such as the EKS ARN
	IncludeNamespaces []string
	ExcludeNamespaces []string
	OwnNamespace      string
	ReleaseName       string
	ReleaseNamespace  string
	// InjectionDisabled is set when the chart did not install the Operator.
	InjectionDisabled bool
	// CoverFields adds the auto-cover marker and the impact.ai/* labels to the snapshot. The chart sets it only when the
	// cover pod is installed or an install token was used, so a server that does not know the fields never sees them.
	CoverFields bool
	Listen      string
	DryRun      bool
	ShowVersion bool

	APIServer             string
	TokenFile             string
	CAFile                string
	InsecureSkipTLSVerify bool

	PodUID string

	// The uninstall notice, run by the Helm pre-delete hook instead of the loop:
	// stop the reporter, wait for its pods, send one final snapshot.
	UninstallNotice bool
	StopDeployment  string
	StopNamespace   string // defaults to the own namespace
	StopPodSelector string
	StopTimeout     time.Duration
	NoticeTimeout   time.Duration
	// The rest of the uninstall hook: stop the cover pod too, and delete the key Secret the install hook made.
	CoverDeployment  string
	CoverPodSelector string
	KeySecret        string
}

// splitList splits a comma separated list, trimming blanks and dropping empty
// entries. It returns nil for an empty list.
func splitList(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

// ParseConfig reads the flags in args, falling back to the environment.
func ParseConfig(args []string, getenv func(string) string, stderr io.Writer) (Config, error) {
	var problems []error
	str := func(names ...string) string {
		for _, n := range names {
			if v := getenv(n); v != "" {
				return v
			}
		}
		return ""
	}
	strOr := func(fallback string, names ...string) string {
		if v := str(names...); v != "" {
			return v
		}
		return fallback
	}
	boolean := func(name string) bool {
		v := getenv(name)
		if v == "" {
			return false
		}
		b, err := strconv.ParseBool(v)
		if err != nil {
			problems = append(problems, fmt.Errorf("%s: %q is not a boolean", name, v))
		}
		return b
	}

	var cfg Config
	fs := flag.NewFlagSet("reporter", flag.ContinueOnError)
	fs.SetOutput(stderr)
	endpoint := fs.String("ingest-endpoint", str("IMPACT_INGEST_ENDPOINT"), "base URL of ingest, for example https://ingest.impact.ai (env IMPACT_INGEST_ENDPOINT)")
	keyFile := fs.String("ingest-key-file", strOr(defaultKeyFile, "IMPACT_INGEST_KEY_FILE"), "file holding the ingest key, read again every cycle (env IMPACT_INGEST_KEY_FILE)")
	interval := fs.String("interval", strOr(defaultInterval.String(), "IMPACT_INTERVAL"), "time between snapshots, at least 5s (env IMPACT_INTERVAL)")
	clusterName := fs.String("cluster-name", str("IMPACT_CLUSTER_NAME"), "optional cluster name shown next to the cluster ID (env IMPACT_CLUSTER_NAME)")
	cloudProvider := fs.String("cloud-provider", str("IMPACT_CLOUD_PROVIDER"), "optional cloud the cluster runs in: aws, azure or gcp (env IMPACT_CLOUD_PROVIDER)")
	cloudAccountID := fs.String("cloud-account-id", str("IMPACT_CLOUD_ACCOUNT_ID"), "optional AWS account, Azure subscription or Google Cloud project (env IMPACT_CLOUD_ACCOUNT_ID)")
	cloudRegion := fs.String("cloud-region", str("IMPACT_CLOUD_REGION"), "optional cloud region; replaces the one read from the nodes (env IMPACT_CLOUD_REGION)")
	clusterARN := fs.String("cluster-arn", str("IMPACT_CLUSTER_ARN"), "optional provider ID of the cluster, such as the EKS ARN (env IMPACT_CLUSTER_ARN)")
	include := fs.String("include-namespaces", str("IMPACT_INCLUDE_NAMESPACES"), "comma separated namespaces to list; empty means all (env IMPACT_INCLUDE_NAMESPACES)")
	exclude := fs.String("exclude-namespaces", str("IMPACT_EXCLUDE_NAMESPACES"), "comma separated namespaces to leave out, added to kube-system, kube-public, kube-node-lease and our own (env IMPACT_EXCLUDE_NAMESPACES)")
	ownNamespace := fs.String("own-namespace", str("IMPACT_OWN_NAMESPACE", "POD_NAMESPACE"), "namespace of the reporter, never listed (env POD_NAMESPACE)")
	releaseName := fs.String("release-name", str("IMPACT_RELEASE_NAME"), "Helm release name, to tell our own webhooks from another injector's (env IMPACT_RELEASE_NAME)")
	releaseNamespace := fs.String("release-namespace", str("IMPACT_RELEASE_NAMESPACE"), "Helm release namespace; defaults to the own namespace (env IMPACT_RELEASE_NAMESPACE)")
	injection := fs.String("injection", strOr("enabled", "IMPACT_INJECTION"), "enabled when the chart installed the Operator, disabled otherwise (env IMPACT_INJECTION)")
	coverFields := fs.Bool("cover-fields", boolean("IMPACT_COVER_FIELDS"), "add the auto-cover marker and the impact.ai/* labels of workloads and namespaces to the snapshot (env IMPACT_COVER_FIELDS)")
	listen := fs.String("listen", strOr(defaultListen, "IMPACT_LISTEN"), "address for /healthz and /readyz (env IMPACT_LISTEN)")
	dryRun := fs.Bool("dry-run", false, "build one snapshot, print it as JSON and exit without sending")
	showVersion := fs.Bool("version", false, "print the version and exit")
	apiServer := fs.String("api-server", str("IMPACT_API_SERVER"), "API server URL; default is the in-cluster address (env IMPACT_API_SERVER)")
	tokenFile := fs.String("token-file", str("IMPACT_TOKEN_FILE"), "bearer token file for the API server; default is the service account token (env IMPACT_TOKEN_FILE)")
	caFile := fs.String("ca-file", str("IMPACT_CA_FILE"), "CA bundle for the API server; default is the service account CA (env IMPACT_CA_FILE)")
	insecure := fs.Bool("insecure-skip-tls-verify", boolean("IMPACT_INSECURE_SKIP_TLS_VERIFY"), "do not verify the API server certificate (tests and development only)")
	allowInsecureIngest := fs.Bool("allow-insecure-ingest", boolean("IMPACT_ALLOW_INSECURE_INGEST"), "accept a plain http ingest endpoint that is not on this machine: the key would travel in clear (tests and development only) (env IMPACT_ALLOW_INSECURE_INGEST)")
	uninstallNotice := fs.Bool("uninstall-notice", boolean("IMPACT_UNINSTALL_NOTICE"), "run the uninstall notice instead of the reporter: stop the reporter, wait for its pods, send one final snapshot, always exit 0 (env IMPACT_UNINSTALL_NOTICE)")
	stopDeployment := fs.String("stop-deployment", str("IMPACT_STOP_DEPLOYMENT"), "uninstall notice: the reporter Deployment to scale to zero (env IMPACT_STOP_DEPLOYMENT)")
	stopNamespace := fs.String("stop-namespace", str("IMPACT_STOP_NAMESPACE"), "uninstall notice: namespace of that Deployment and its pods; defaults to the own namespace (env IMPACT_STOP_NAMESPACE)")
	stopSelector := fs.String("stop-pod-selector", str("IMPACT_STOP_POD_SELECTOR"), "uninstall notice: label selector of the reporter's pods, which are waited for (env IMPACT_STOP_POD_SELECTOR)")
	stopTimeout := fs.String("stop-timeout", strOr(defaultStopTimeout.String(), "IMPACT_STOP_TIMEOUT"), "uninstall notice: how long to wait for the pods to exit (env IMPACT_STOP_TIMEOUT)")
	coverDeployment := fs.String("cover-deployment", str("IMPACT_COVER_DEPLOYMENT"), "uninstall notice: the cover pod's Deployment, scaled to zero beside the reporter (env IMPACT_COVER_DEPLOYMENT)")
	coverSelector := fs.String("cover-pod-selector", str("IMPACT_COVER_POD_SELECTOR"), "uninstall notice: label selector of the cover pod's pods, which are waited for (env IMPACT_COVER_POD_SELECTOR)")
	keySecret := fs.String("key-secret", str("IMPACT_KEY_SECRET"), "uninstall notice: the Secret that holds the ingest key, deleted after the notice (env IMPACT_KEY_SECRET)")
	noticeTimeout := fs.String("notice-timeout", strOr(defaultNoticeTimeout.String(), "IMPACT_NOTICE_TIMEOUT"), "uninstall notice: total time to build and send the notice, retries included (env IMPACT_NOTICE_TIMEOUT)")

	if err := fs.Parse(args); err != nil {
		return Config{}, err
	}
	if len(problems) > 0 {
		return Config{}, errors.Join(problems...)
	}

	cfg = Config{
		IngestEndpoint:        *endpoint,
		IngestKeyFile:         *keyFile,
		ClusterName:           *clusterName,
		CloudProvider:         strings.ToLower(strings.TrimSpace(*cloudProvider)),
		CloudAccountID:        strings.TrimSpace(*cloudAccountID),
		CloudRegion:           strings.TrimSpace(*cloudRegion),
		ClusterARN:            strings.TrimSpace(*clusterARN),
		IncludeNamespaces:     splitList(*include),
		ExcludeNamespaces:     splitList(*exclude),
		OwnNamespace:          *ownNamespace,
		ReleaseName:           *releaseName,
		ReleaseNamespace:      *releaseNamespace,
		CoverFields:           *coverFields,
		Listen:                *listen,
		DryRun:                *dryRun,
		ShowVersion:           *showVersion,
		APIServer:             *apiServer,
		TokenFile:             *tokenFile,
		CAFile:                *caFile,
		InsecureSkipTLSVerify: *insecure,
		PodUID:                str("POD_UID"),
		UninstallNotice:       *uninstallNotice,
		StopDeployment:        *stopDeployment,
		StopNamespace:         *stopNamespace,
		StopPodSelector:       *stopSelector,
		CoverDeployment:       *coverDeployment,
		CoverPodSelector:      *coverSelector,
		KeySecret:             *keySecret,
	}
	if cfg.ReleaseNamespace == "" {
		cfg.ReleaseNamespace = cfg.OwnNamespace
	}
	if cfg.StopNamespace == "" {
		cfg.StopNamespace = cfg.OwnNamespace
	}

	switch strings.ToLower(strings.TrimSpace(*injection)) {
	case "enabled":
	case "disabled":
		cfg.InjectionDisabled = true
	default:
		problems = append(problems, fmt.Errorf("injection: %q is neither enabled nor disabled", *injection))
	}

	switch cfg.CloudProvider {
	case "", "aws", "azure", "gcp":
	default:
		problems = append(problems, fmt.Errorf("cloud-provider: %q is not aws, azure or gcp", *cloudProvider))
	}

	d, err := time.ParseDuration(*interval)
	switch {
	case err != nil:
		problems = append(problems, fmt.Errorf("interval: %q is not a duration such as 5m", *interval))
	case d < minInterval:
		problems = append(problems, fmt.Errorf("interval: %v is shorter than the minimum %v", d, minInterval))
	}
	cfg.Interval = d

	// The timeouts only matter for the notice; outside it a bad value is ignored.
	timeout := func(name, value string, fallback time.Duration) time.Duration {
		d, err := time.ParseDuration(value)
		if err != nil || d <= 0 {
			if cfg.UninstallNotice {
				problems = append(problems, fmt.Errorf("%s: %q is not a positive duration such as 60s", name, value))
			}
			return fallback
		}
		return d
	}
	cfg.StopTimeout = timeout("stop-timeout", *stopTimeout, defaultStopTimeout)
	cfg.NoticeTimeout = timeout("notice-timeout", *noticeTimeout, defaultNoticeTimeout)
	if cfg.UninstallNotice && (cfg.StopDeployment != "" || cfg.StopPodSelector != "" || cfg.CoverDeployment != "" || cfg.CoverPodSelector != "" || cfg.KeySecret != "") && cfg.StopNamespace == "" {
		problems = append(problems, errors.New("stop namespace: the Deployments, their pods and the key Secret need a namespace (set --stop-namespace or --own-namespace)"))
	}

	if !cfg.ShowVersion && !cfg.DryRun {
		if err := checkIngestEndpoint(cfg.IngestEndpoint, *allowInsecureIngest); err != nil {
			problems = append(problems, err)
		}
	}
	if len(problems) > 0 {
		return Config{}, errors.Join(problems...)
	}
	return cfg, nil
}
