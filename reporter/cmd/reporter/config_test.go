// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"reflect"
	"strings"
	"testing"
	"time"
)

func env(kv ...string) func(string) string {
	m := map[string]string{}
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i]] = kv[i+1]
	}
	return func(k string) string { return m[k] }
}

func parse(t *testing.T, args []string, getenv func(string) string) (Config, error) {
	t.Helper()
	var stderr bytes.Buffer
	return ParseConfig(args, getenv, &stderr)
}

func TestConfigDefaults(t *testing.T) {
	cfg, err := parse(t, []string{"--ingest-endpoint=https://ingest.example.com"}, env())
	if err != nil {
		t.Fatal(err)
	}
	want := Config{
		IngestEndpoint: "https://ingest.example.com",
		IngestKeyFile:  "/var/run/secrets/impact/ingest-key",
		Interval:       5 * time.Minute,
		Listen:         ":8080",
		StopTimeout:    60 * time.Second,
		NoticeTimeout:  60 * time.Second,
	}
	if !reflect.DeepEqual(cfg, want) {
		t.Errorf("defaults =\n%+v\nwant\n%+v", cfg, want)
	}
	if cfg.InjectionDisabled {
		t.Error("injection must default to enabled")
	}
}

func TestConfigEnvironmentFallbacks(t *testing.T) {
	cfg, err := parse(t, nil, env(
		"IMPACT_INGEST_ENDPOINT", "https://ingest.example.com/",
		"IMPACT_INGEST_KEY_FILE", "/keys/k",
		"IMPACT_INTERVAL", "90s",
		"IMPACT_CLUSTER_NAME", "prod-eks",
		"IMPACT_INCLUDE_NAMESPACES", "shop, data ,",
		"IMPACT_EXCLUDE_NAMESPACES", "batch",
		"POD_NAMESPACE", "impact-system",
		"IMPACT_RELEASE_NAME", "impact-cluster-kit",
		"IMPACT_RELEASE_NAMESPACE", "releases",
		"IMPACT_INJECTION", "disabled",
		"IMPACT_LISTEN", "127.0.0.1:9090",
		"POD_UID", "uid-123",
		"IMPACT_API_SERVER", "http://127.0.0.1:8001",
		"IMPACT_TOKEN_FILE", "/tok",
		"IMPACT_CA_FILE", "/ca",
		"IMPACT_INSECURE_SKIP_TLS_VERIFY", "true",
		"IMPACT_UNINSTALL_NOTICE", "true",
		"IMPACT_STOP_DEPLOYMENT", "impact-ai-reporter",
		"IMPACT_STOP_NAMESPACE", "releases",
		"IMPACT_STOP_POD_SELECTOR", "app.kubernetes.io/name=reporter",
		"IMPACT_STOP_TIMEOUT", "45s",
		"IMPACT_NOTICE_TIMEOUT", "30s",
		"IMPACT_CLOUD_PROVIDER", "aws",
		"IMPACT_CLOUD_ACCOUNT_ID", "012345678901",
		"IMPACT_CLOUD_REGION", "eu-west-1",
		"IMPACT_CLUSTER_ARN", "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks",
	))
	if err != nil {
		t.Fatal(err)
	}
	want := Config{
		IngestEndpoint: "https://ingest.example.com/", IngestKeyFile: "/keys/k", Interval: 90 * time.Second,
		ClusterName: "prod-eks", IncludeNamespaces: []string{"shop", "data"}, ExcludeNamespaces: []string{"batch"},
		OwnNamespace: "impact-system", ReleaseName: "impact-cluster-kit", ReleaseNamespace: "releases",
		InjectionDisabled: true, Listen: "127.0.0.1:9090", PodUID: "uid-123",
		APIServer: "http://127.0.0.1:8001", TokenFile: "/tok", CAFile: "/ca", InsecureSkipTLSVerify: true,
		UninstallNotice: true, StopDeployment: "impact-ai-reporter", StopNamespace: "releases",
		StopPodSelector: "app.kubernetes.io/name=reporter", StopTimeout: 45 * time.Second, NoticeTimeout: 30 * time.Second,
		CloudProvider: "aws", CloudAccountID: "012345678901", CloudRegion: "eu-west-1",
		ClusterARN: "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks",
	}
	if !reflect.DeepEqual(cfg, want) {
		t.Errorf("config =\n%+v\nwant\n%+v", cfg, want)
	}
}

func TestFlagsOverrideTheEnvironment(t *testing.T) {
	cfg, err := parse(t, []string{
		"--ingest-endpoint=https://flag.example.com", "--interval=30s", "--cluster-name=from-flag",
		"--include-namespaces=a,b", "--exclude-namespaces=c", "--own-namespace=own", "--injection=enabled",
		"--ingest-key-file=/flag/key", "--listen=:9999",
		"--stop-deployment=from-flag", "--stop-namespace=flag-ns", "--stop-pod-selector=a=b", "--stop-timeout=20s", "--notice-timeout=15s",
	}, env(
		"IMPACT_INGEST_ENDPOINT", "https://env.example.com", "IMPACT_INTERVAL", "10m", "IMPACT_CLUSTER_NAME", "from-env",
		"IMPACT_INCLUDE_NAMESPACES", "x", "IMPACT_EXCLUDE_NAMESPACES", "y", "POD_NAMESPACE", "env-ns", "IMPACT_INJECTION", "disabled",
		"IMPACT_INGEST_KEY_FILE", "/env/key", "IMPACT_LISTEN", ":1",
		"IMPACT_STOP_DEPLOYMENT", "from-env", "IMPACT_STOP_NAMESPACE", "env-ns", "IMPACT_STOP_POD_SELECTOR", "c=d",
		"IMPACT_STOP_TIMEOUT", "90s", "IMPACT_NOTICE_TIMEOUT", "80s", "IMPACT_UNINSTALL_NOTICE", "false",
	))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.IngestEndpoint != "https://flag.example.com" || cfg.Interval != 30*time.Second || cfg.ClusterName != "from-flag" ||
		!reflect.DeepEqual(cfg.IncludeNamespaces, []string{"a", "b"}) || !reflect.DeepEqual(cfg.ExcludeNamespaces, []string{"c"}) ||
		cfg.OwnNamespace != "own" || cfg.InjectionDisabled || cfg.IngestKeyFile != "/flag/key" || cfg.Listen != ":9999" {
		t.Errorf("flags did not win: %+v", cfg)
	}
	if cfg.StopDeployment != "from-flag" || cfg.StopNamespace != "flag-ns" || cfg.StopPodSelector != "a=b" ||
		cfg.StopTimeout != 20*time.Second || cfg.NoticeTimeout != 15*time.Second || cfg.UninstallNotice {
		t.Errorf("uninstall notice flags did not win: %+v", cfg)
	}
}

func TestReleaseNamespaceDefaultsToTheOwnNamespace(t *testing.T) {
	cfg, err := parse(t, []string{"--ingest-endpoint=https://i", "--own-namespace=impact-system", "--release-name=kit"}, env())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ReleaseNamespace != "impact-system" {
		t.Errorf("release namespace = %q", cfg.ReleaseNamespace)
	}
}

func TestStopNamespaceDefaultsToTheOwnNamespace(t *testing.T) {
	cfg, err := parse(t, []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--own-namespace=impact-system", "--stop-deployment=reporter"}, env())
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.UninstallNotice || cfg.StopNamespace != "impact-system" {
		t.Errorf("config = %+v", cfg)
	}
}

func TestConfigValidation(t *testing.T) {
	cases := []struct {
		name string
		args []string
		env  func(string) string
		want string // substring of the error; empty means valid
	}{
		{"endpoint is required", nil, env(), "ingest endpoint"},
		{"endpoint must be a URL", []string{"--ingest-endpoint=ingest.example.com"}, env(), "ingest endpoint"},
		{"endpoint must be http or https", []string{"--ingest-endpoint=ftp://x"}, env(), "ingest endpoint"},
		{"dry run needs no endpoint", []string{"--dry-run"}, env(), ""},
		{"version needs no endpoint", []string{"--version"}, env(), ""},
		{"interval has a floor", []string{"--ingest-endpoint=https://i", "--interval=4s"}, env(), "interval"},
		{"interval of exactly five seconds is fine", []string{"--ingest-endpoint=https://i", "--interval=5s"}, env(), ""},
		{"interval must parse", []string{"--ingest-endpoint=https://i", "--interval=soon"}, env(), "interval"},
		{"interval in the environment is checked too", nil, env("IMPACT_INGEST_ENDPOINT", "https://i", "IMPACT_INTERVAL", "1s"), "interval"},
		{"injection must be enabled or disabled", []string{"--ingest-endpoint=https://i", "--injection=maybe"}, env(), "injection"},
		{"bad boolean in the environment", nil, env("IMPACT_INGEST_ENDPOINT", "https://i", "IMPACT_INSECURE_SKIP_TLS_VERIFY", "perhaps"), "IMPACT_INSECURE_SKIP_TLS_VERIFY"},
		{"unknown flags are errors", []string{"--ingest-endpoint=https://i", "--nope"}, env(), "nope"},

		{"the uninstall notice needs an endpoint", []string{"--uninstall-notice"}, env(), "ingest endpoint"},
		{"the uninstall notice dry run does not", []string{"--uninstall-notice", "--dry-run"}, env(), ""},
		{"stop timeout must be positive", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-timeout=0s"}, env(), "stop-timeout"},
		{"stop timeout must parse", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-timeout=soon"}, env(), "stop-timeout"},
		{"notice timeout must be positive", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--notice-timeout=-1s"}, env(), "notice-timeout"},
		{"notice timeout must parse", nil, env("IMPACT_UNINSTALL_NOTICE", "true", "IMPACT_INGEST_ENDPOINT", "https://i", "IMPACT_NOTICE_TIMEOUT", "later"), "notice-timeout"},
		{"a deployment to stop needs a namespace", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-deployment=reporter"}, env(), "stop namespace"},
		{"the stop namespace may come from the own namespace", []string{"--uninstall-notice", "--ingest-endpoint=https://i", "--stop-deployment=reporter", "--own-namespace=ns"}, env(), ""},
		{"a notice with nothing to stop is fine", []string{"--uninstall-notice", "--ingest-endpoint=https://i"}, env(), ""},
		{"the cloud provider is aws, azure or gcp", []string{"--ingest-endpoint=https://i", "--cloud-provider=azure"}, env(), ""},
		{"any other cloud provider is refused", []string{"--ingest-endpoint=https://i", "--cloud-provider=oracle"}, env(), "cloud-provider"},
		{"the cloud provider in the environment is checked too", nil, env("IMPACT_INGEST_ENDPOINT", "https://i", "IMPACT_CLOUD_PROVIDER", "ibm"), "cloud-provider"},
		{"stop settings are ignored outside the notice", []string{"--ingest-endpoint=https://i", "--stop-timeout=0s", "--stop-deployment=reporter"}, env(), ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parse(t, tc.args, tc.env)
			switch {
			case tc.want == "" && err != nil:
				t.Errorf("unexpected error: %v", err)
			case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
				t.Errorf("error = %v, want it to mention %q", err, tc.want)
			}
		})
	}
}

func TestCommaListsAreTrimmedAndEmptyEntriesDropped(t *testing.T) {
	cfg, err := parse(t, []string{"--ingest-endpoint=https://i", "--include-namespaces= a , ,b,,"}, env())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(cfg.IncludeNamespaces, []string{"a", "b"}) {
		t.Errorf("include = %q", cfg.IncludeNamespaces)
	}
}

func TestCloudIdentityIsEmptyUntilGiven(t *testing.T) {
	cfg, err := parse(t, []string{"--ingest-endpoint=https://i"}, env())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.CloudProvider != "" || cfg.CloudAccountID != "" || cfg.CloudRegion != "" || cfg.ClusterARN != "" {
		t.Errorf("cloud identity = %+v, want none", cfg)
	}
}

func TestCloudIdentityFlagsWinAndAreTidied(t *testing.T) {
	cfg, err := parse(t, []string{
		"--ingest-endpoint=https://i",
		"--cloud-provider= GCP ", "--cloud-account-id= my-project ", "--cloud-region=europe-west1 ",
		"--cluster-arn= //container.googleapis.com/projects/my-project/locations/europe-west1/clusters/my-cluster ",
	}, env(
		"IMPACT_CLOUD_PROVIDER", "aws", "IMPACT_CLOUD_ACCOUNT_ID", "1", "IMPACT_CLOUD_REGION", "us-east-1", "IMPACT_CLUSTER_ARN", "arn:aws:eks:x",
	))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.CloudProvider != "gcp" || cfg.CloudAccountID != "my-project" || cfg.CloudRegion != "europe-west1" ||
		cfg.ClusterARN != "//container.googleapis.com/projects/my-project/locations/europe-west1/clusters/my-cluster" {
		t.Errorf("cloud identity = %q %q %q %q", cfg.CloudProvider, cfg.CloudAccountID, cfg.CloudRegion, cfg.ClusterARN)
	}
}
