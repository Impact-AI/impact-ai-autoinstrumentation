// SPDX-License-Identifier: Apache-2.0

package kube

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// In-cluster defaults.
const (
	DefaultTokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	DefaultCAFile    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	DefaultTimeout   = 30 * time.Second

	// maxResponseBytes bounds the memory one response can take.
	maxResponseBytes = 256 << 20
)

// Config tells the client where the API server is and how to authenticate.
type Config struct {
	// Host is the base URL of the API server, such as https://10.96.0.1:443.
	Host string
	// TokenFile holds the bearer token. It is read again for every request so a
	// rotated projected token is picked up. Empty means no Authorization header.
	TokenFile string
	// CAFile is a PEM bundle that signed the server certificate. Empty means the
	// system roots.
	CAFile string
	// Insecure disables certificate verification (tests and development only).
	Insecure bool
	// Timeout applies to each request. Zero means DefaultTimeout.
	Timeout   time.Duration
	UserAgent string
}

// InClusterConfig builds the configuration of a pod from its environment.
func InClusterConfig(getenv func(string) string) (Config, error) {
	host, port := getenv("KUBERNETES_SERVICE_HOST"), getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return Config{}, errors.New("not running in a cluster: KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT are not set (use --api-server)")
	}
	return Config{
		Host:      "https://" + net.JoinHostPort(host, port),
		TokenFile: DefaultTokenFile,
		CAFile:    DefaultCAFile,
	}, nil
}

// Client is a Kubernetes API client. It reads: GET requests only, apart from
// MergePatch, which only the uninstall notice uses.
type Client struct {
	host      string
	tokenFile string
	timeout   time.Duration
	userAgent string
	http      *http.Client
}

// HTTPClient builds the HTTP client for the API server cfg describes: TLS 1.2 or later, trusting cfg.CAFile when it is
// set (the system roots otherwise), and never the pod's HTTP proxy. It does not authenticate; see SetBearer. It fails
// when the CA file cannot be used.
func HTTPClient(cfg Config) (*http.Client, error) {
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: cfg.Insecure} //nolint:gosec // opt-in for tests and development
	if cfg.CAFile != "" {
		pemBytes, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("read CA file: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pemBytes) {
			return nil, fmt.Errorf("CA file %s holds no certificate", cfg.CAFile)
		}
		tlsConfig.RootCAs = pool
	}
	return &http.Client{Transport: &http.Transport{
		// The API server is reached directly, never through the pod's HTTP proxy.
		Proxy:               nil,
		TLSClientConfig:     tlsConfig,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        4,
		IdleConnTimeout:     60 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}}, nil
}

// SetBearer authenticates req with the token in tokenFile, read now so that a rotated projected token is picked up.
// An empty tokenFile sets nothing.
func SetBearer(req *http.Request, tokenFile string) error {
	if tokenFile == "" {
		return nil
	}
	token, err := os.ReadFile(tokenFile)
	if err != nil {
		return fmt.Errorf("read service account token: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	return nil
}

// New builds a client. It fails when the CA file cannot be used.
func New(cfg Config) (*Client, error) {
	u, err := url.Parse(cfg.Host)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("invalid API server address %q: want http(s)://host[:port]", cfg.Host)
	}
	httpClient, err := HTTPClient(cfg)
	if err != nil {
		return nil, err
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	ua := cfg.UserAgent
	if ua == "" {
		ua = "impact-reporter"
	}
	return &Client{
		host:      strings.TrimRight(cfg.Host, "/"),
		tokenFile: cfg.TokenFile,
		timeout:   timeout,
		userAgent: ua,
		http:      httpClient,
	}, nil
}

// StatusError is returned for any answer other than 200. It names the method,
// path and status, never the body.
type StatusError struct {
	Code   int
	Path   string
	Method string
}

func (e *StatusError) Error() string {
	method := e.Method
	if method == "" {
		method = http.MethodGet
	}
	return fmt.Sprintf("%s %s: %d %s", method, e.Path, e.Code, http.StatusText(e.Code))
}

// do sends one request and decodes the JSON answer into out (nil discards it).
func (c *Client) do(ctx context.Context, method, path string, query url.Values, contentType string, body []byte, out any) error {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	target := c.host + path
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", c.userAgent)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if err := SetBearer(req, c.tokenFile); err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return &StatusError{Code: resp.StatusCode, Path: path, Method: method}
	}
	if out == nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return nil
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxResponseBytes)).Decode(out); err != nil {
		return fmt.Errorf("%s %s: decode: %w", method, path, err)
	}
	return nil
}

// GetJSON issues one GET request and decodes the JSON answer into out.
func (c *Client) GetJSON(ctx context.Context, path string, query url.Values, out any) error {
	return c.do(ctx, http.MethodGet, path, query, "", nil, out)
}

// MergePatch sends one JSON merge patch (RFC 7386) and decodes the answer into
// out; nil discards it. It exists for the uninstall notice, which scales the
// reporter's own Deployment to zero. Nothing else in the reporter writes to the
// cluster, and a test keeps it that way.
func (c *Client) MergePatch(ctx context.Context, path string, patch []byte, out any) error {
	return c.do(ctx, http.MethodPatch, path, nil, "application/merge-patch+json", patch, out)
}

type listPage[T any] struct {
	Metadata struct {
		Continue string `json:"continue"`
	} `json:"metadata"`
	Items []T `json:"items"`
}

// ListQuery shapes a list request.
type ListQuery struct {
	// Limit is the page size the client asks for; the server may return less.
	Limit int
	// FieldSelector narrows the list on the server, such as status.phase!=Failed.
	// It is sent with every page, as the API requires.
	FieldSelector string
	// LabelSelector narrows the list to objects with matching labels. It is also
	// sent with every page.
	LabelSelector string
}

func (q ListQuery) values(token string) url.Values {
	v := url.Values{}
	if q.Limit > 0 {
		v.Set("limit", strconv.Itoa(q.Limit))
	}
	if q.FieldSelector != "" {
		v.Set("fieldSelector", q.FieldSelector)
	}
	if q.LabelSelector != "" {
		v.Set("labelSelector", q.LabelSelector)
	}
	if token != "" {
		v.Set("continue", token)
	}
	return v
}

// List reads every item of a list endpoint page by page and passes each item to
// fn. A page is decoded at a time, so memory stays bounded by the page size.
// Listing stops at the first error from fn.
func List[T any](ctx context.Context, c *Client, path string, limit int, fn func(*T) error) error {
	return ListWith(ctx, c, path, ListQuery{Limit: limit}, fn)
}

// ListWith is List with a field selector.
func ListWith[T any](ctx context.Context, c *Client, path string, q ListQuery, fn func(*T) error) error {
	token := ""
	for {
		var page listPage[T]
		if err := c.GetJSON(ctx, path, q.values(token), &page); err != nil {
			return err
		}
		for i := range page.Items {
			if err := fn(&page.Items[i]); err != nil {
				return err
			}
		}
		if page.Metadata.Continue == "" {
			return nil
		}
		token = page.Metadata.Continue
	}
}

// FirstPage reads one page of a list endpoint and ignores any continue token.
func FirstPage[T any](ctx context.Context, c *Client, path string, limit int) ([]T, error) {
	var page listPage[T]
	if err := c.GetJSON(ctx, path, ListQuery{Limit: limit}.values(""), &page); err != nil {
		return nil, err
	}
	return page.Items, nil
}
