// SPDX-License-Identifier: Apache-2.0

// Package kubesecret reads, patches and deletes Secrets of one namespace through the Kubernetes API. It is how the
// install hook keeps the ingest key it makes in the Secret Helm made for it (internal/exchange) and how the uninstall
// hook removes that Secret (internal/notice), and it is the only place the reporter writes a Secret. It has no way to
// create one: the install hook's role holds no create. guard_test.go keeps it that way: nothing else may import it, and
// it addresses nothing but a Secret of a namespace.
//
// It reuses the TLS and token handling of internal/kube but none of its methods: the reporter's own client has no
// write besides the uninstall notice's merge patches (internal/notice/readonly_test.go).
package kubesecret

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/kube"
)

// ErrNotFound is the one answer a caller acts on; every other failure is a *kube.StatusError, which names the method,
// the path and the status and never the answer's body.
var ErrNotFound = errors.New("secret not found")

const (
	defaultTimeout = 30 * time.Second
	// A Secret is small here (a key and two annotations); anything bigger is not ours.
	maxAnswerBytes = 1 << 20
)

// A namespace is a DNS label and a Secret a DNS subdomain. Anything else could change the path.
var (
	dnsLabel     = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)
	dnsSubdomain = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$`)
)

// Secret is the part of a Secret the hooks read and write.
type Secret struct {
	Labels      map[string]string
	Annotations map[string]string
	// Data holds the decoded values; they are base64 on the wire.
	Data map[string][]byte
}

// Patch is a JSON merge patch (RFC 7386) of a Secret's annotations and data. A nil value removes the entry.
type Patch struct {
	Annotations map[string]*string
	Data        map[string]*[]byte
}

// Store talks to the API server as the pod it runs in.
type Store struct {
	host      string
	tokenFile string
	userAgent string
	timeout   time.Duration
	http      *http.Client
}

// New builds a Store for the API server cfg describes.
func New(cfg kube.Config) (*Store, error) {
	u, err := url.Parse(cfg.Host)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("invalid API server address %q: want http(s)://host[:port]", cfg.Host)
	}
	client, err := kube.HTTPClient(cfg)
	if err != nil {
		return nil, err
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = defaultTimeout
	}
	ua := cfg.UserAgent
	if ua == "" {
		ua = "impact-reporter"
	}
	return &Store{host: strings.TrimRight(cfg.Host, "/"), tokenFile: cfg.TokenFile, userAgent: ua, timeout: timeout, http: client}, nil
}

func path(namespace, name string) (string, error) {
	if !dnsLabel.MatchString(namespace) {
		return "", fmt.Errorf("%q is not a namespace name", namespace)
	}
	collection := "/api/v1/namespaces/" + namespace + "/secrets"
	if name == "" {
		return collection, nil
	}
	if !dnsSubdomain.MatchString(name) {
		return "", fmt.Errorf("%q is not a Secret name", name)
	}
	return collection + "/" + name, nil
}

// do sends one request. A 2xx answer is decoded into out (nil discards it); 404 is ErrNotFound.
func (s *Store) do(ctx context.Context, method, apiPath, contentType string, body []byte, out any) error {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, s.host+apiPath, reader)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, apiPath, err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", s.userAgent)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if err := kube.SetBearer(req, s.tokenFile); err != nil {
		return err
	}
	resp, err := s.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, apiPath, err)
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		if out == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxAnswerBytes))
			return nil
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, maxAnswerBytes)).Decode(out); err != nil {
			return fmt.Errorf("%s %s: decode: %w", method, apiPath, err)
		}
		return nil
	case resp.StatusCode == http.StatusNotFound:
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return ErrNotFound
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	return &kube.StatusError{Code: resp.StatusCode, Path: apiPath, Method: method}
}

// Get reads a Secret. A Secret that is not there is ErrNotFound.
func (s *Store) Get(ctx context.Context, namespace, name string) (*Secret, error) {
	apiPath, err := path(namespace, name)
	if err != nil || name == "" {
		return nil, errors.Join(err, errors.New("a Secret name is required"))
	}
	var wire struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
			Labels      map[string]string `json:"labels"`
		} `json:"metadata"`
		Data map[string][]byte `json:"data"`
	}
	if err := s.do(ctx, http.MethodGet, apiPath, "", nil, &wire); err != nil {
		return nil, err
	}
	return &Secret{Labels: wire.Metadata.Labels, Annotations: wire.Metadata.Annotations, Data: wire.Data}, nil
}

// Patch applies a JSON merge patch to a Secret. A Secret that is not there is ErrNotFound.
func (s *Store) Patch(ctx context.Context, namespace, name string, p Patch) error {
	apiPath, err := path(namespace, name)
	if err != nil || name == "" {
		return errors.Join(err, errors.New("a Secret name is required"))
	}
	doc := map[string]any{}
	if len(p.Annotations) > 0 {
		doc["metadata"] = map[string]any{"annotations": p.Annotations}
	}
	if len(p.Data) > 0 {
		data := make(map[string]any, len(p.Data))
		for key, value := range p.Data {
			if value == nil {
				data[key] = nil
			} else {
				data[key] = *value // []byte: encoding/json writes base64
			}
		}
		doc["data"] = data
	}
	body, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	return s.do(ctx, http.MethodPatch, apiPath, "application/merge-patch+json", body, nil)
}

// Delete deletes a Secret. One that is not there is ErrNotFound, which the caller may take as done.
func (s *Store) Delete(ctx context.Context, namespace, name string) error {
	apiPath, err := path(namespace, name)
	if err != nil || name == "" {
		return errors.Join(err, errors.New("a Secret name is required"))
	}
	return s.do(ctx, http.MethodDelete, apiPath, "", nil, nil)
}
