// SPDX-License-Identifier: Apache-2.0

// Package forward is the forward-first gateway: it passes a workload's trace export to the backend the workload sent it
// to before, with the workload's own headers, gives the exporter the backend's own answer, and only after the backend
// accepted a batch puts one copy of it in a bounded queue towards impact.ai. A vendor protocol, or a tool's SDK given a
// base URL, is relayed byte for byte on every path; only trace exports are copied, a vendor protocol decoded in the
// background. Every copy names its tool and route in the resource attributes impact.source.id and impact.source.route.
// The gateway holds no credential of the vendor's: a key passes through in memory, and neither a header nor the
// target URL is logged, counted or copied.
package forward

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay/source"
)

// Protocol is how a route's workload exports.
type Protocol string

const (
	// ProtocolOTLPHTTP is OTLP over HTTP. The target is the workload's traces URL, and every request is sent to it as
	// it is, its query included; with Base, the target is the base URL a tool's SDK appends its own paths to (traces,
	// metrics, prompts, scores), and every request keeps its path and query, appended to the target's.
	ProtocolOTLPHTTP Protocol = "otlp-http"
	// ProtocolOTLPGRPC is OTLP over gRPC: every request keeps its path and goes to the target's address over HTTP/2.
	ProtocolOTLPGRPC Protocol = "otlp-grpc"
	// ProtocolRelay is any other protocol over HTTP: every request on every path goes to the target address, its
	// path and query appended to the target's.
	ProtocolRelay Protocol = "relay"
)

// maxRoutes bounds the routes of one gateway.
const maxRoutes = 512

// Route is one workload's switched export: the gateway listens on Listen and sends what arrives there to Target.
// The target can carry a vendor's token, so it is never logged or put in an error; TargetHost names the backend.
type Route struct {
	Name     string   `json:"name"`
	Listen   string   `json:"listen"`
	Protocol Protocol `json:"protocol"`
	Target   string   `json:"target"`
	// Base says an otlp-http target is a base URL: every path is forwarded, and only an OTLP trace export (a POST to
	// a path that ends in /v1/traces: /v1/traces, /api/public/otel/v1/traces, /otel/v1/traces) is copied.
	Base bool `json:"base,omitempty"`
	// Source is the id of the tool whose OTLP the route carries ("traceloop", "langfuse"): every copy of an otlp-http
	// or otlp-grpc route says so in its resource attributes, and a relay's source is its decoder's.
	Source string `json:"source,omitempty"`
	// Decode is the relay source whose decoder reads a copy of what the backend accepted ("zipkin", "datadog",
	// "newrelic"); empty relays without a copy.
	Decode string `json:"decode,omitempty"`

	target  *url.URL
	decoder relay.Decoder
}

// decoders are the relay's sources by id.
var decoders = func() map[string]relay.Decoder {
	m := map[string]relay.Decoder{}
	for _, d := range source.All() {
		m[d.Source()] = d
	}
	return m
}()

// Sources are the ids a relay route may name in Decode.
func Sources() []string { return slices.Sorted(maps.Keys(decoders)) }

// TargetHost is the backend's host and port as the target names it: what a log line or a person may see.
func (r Route) TargetHost() string {
	if u, err := r.targetURL(); err == nil {
		return u.Host
	}
	return ""
}

func (r Route) targetURL() (*url.URL, error) {
	if r.target != nil {
		return r.target, nil
	}
	return parseTarget(r.Target)
}

var (
	namePattern   = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$`)
	sourcePattern = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,62}$`)
)

// errBadTarget never says what the target was: it can hold a token.
var errBadTarget = errors.New("target: want an http or https URL with a host (the value is not shown: it can hold a credential)")

func parseTarget(s string) (*url.URL, error) {
	u, err := url.Parse(s)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.Hostname() == "" || u.Fragment != "" {
		return nil, errBadTarget
	}
	return u, nil
}

// ParseRoutes reads and checks a routes document: {"routes": [Route, ...]}.
func ParseRoutes(data []byte) ([]Route, error) {
	var doc struct {
		Routes []Route `json:"routes"`
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("routes: not a routes document: %w", jsonProblem(err))
	}
	if len(doc.Routes) == 0 {
		return nil, errors.New("routes: no routes")
	}
	if len(doc.Routes) > maxRoutes {
		return nil, fmt.Errorf("routes: %d routes, at most %d", len(doc.Routes), maxRoutes)
	}
	names := map[string]bool{}
	listens := map[string]bool{}
	var problems []error
	for i := range doc.Routes {
		r := &doc.Routes[i]
		if err := r.check(); err != nil {
			problems = append(problems, err)
			continue
		}
		if names[r.Name] {
			problems = append(problems, fmt.Errorf("route %q: the name is used twice", r.Name))
		}
		if listens[r.Listen] {
			problems = append(problems, fmt.Errorf("route %q: listen %q is used twice", r.Name, r.Listen))
		}
		names[r.Name], listens[r.Listen] = true, true
	}
	if len(problems) > 0 {
		return nil, errors.Join(problems...)
	}
	return doc.Routes, nil
}

// jsonProblem keeps the kind and place of a JSON error and drops anything it quotes from the document.
func jsonProblem(err error) error {
	var syntax *json.SyntaxError
	var typeErr *json.UnmarshalTypeError
	switch {
	case errors.As(err, &syntax):
		return fmt.Errorf("invalid JSON at byte %d", syntax.Offset)
	case errors.As(err, &typeErr):
		return fmt.Errorf("field %q has the wrong type", typeErr.Field)
	case strings.HasPrefix(err.Error(), "json: unknown field "):
		return err // the field's name, from the document's keys, never a value
	default:
		return errors.New("invalid JSON")
	}
}

func (r *Route) check() error {
	if len(r.Name) > 253 || !namePattern.MatchString(r.Name) {
		return fmt.Errorf("route %q: name: want lower-case letters, digits, '-' and '.'", r.Name)
	}
	if _, port, err := net.SplitHostPort(r.Listen); err != nil || port == "" {
		return fmt.Errorf("route %q: listen: want [host]:port, got %q", r.Name, r.Listen)
	}
	return r.checkForward()
}

// checkForward checks what forwarding needs: the protocol, the target and the decoder.
func (r *Route) checkForward() error {
	switch r.Protocol {
	case ProtocolOTLPHTTP, ProtocolOTLPGRPC, ProtocolRelay:
	default:
		return fmt.Errorf("route %q: protocol: want otlp-http, otlp-grpc or relay, got %q", r.Name, r.Protocol)
	}
	u, err := parseTarget(r.Target)
	if err != nil {
		return fmt.Errorf("route %q: %w", r.Name, err)
	}
	if r.Protocol == ProtocolOTLPGRPC && ((u.Path != "" && u.Path != "/") || u.RawQuery != "") {
		return fmt.Errorf("route %q: target: an otlp-grpc target is an address, with no path or query (the value is not shown)", r.Name)
	}
	if r.Base && r.Protocol != ProtocolOTLPHTTP {
		return fmt.Errorf("route %q: base: only an otlp-http target is a base URL; a relay always appends the path", r.Name)
	}
	switch {
	case r.Protocol == ProtocolRelay && r.Source != "":
		return fmt.Errorf("route %q: source: a relay's source is its decoder (decode)", r.Name)
	case r.Protocol != ProtocolRelay && !sourcePattern.MatchString(r.Source):
		return fmt.Errorf("route %q: source: want the id of the tool whose OTLP the route carries (lower-case letters, digits, '-' and '_'), got %q", r.Name, r.Source)
	}
	switch {
	case r.Decode == "":
	case r.Protocol != ProtocolRelay:
		return fmt.Errorf("route %q: decode: only a relay decodes; OTLP is copied as it is", r.Name)
	case decoders[r.Decode] == nil:
		return fmt.Errorf("route %q: decode: want one of %s, got %q", r.Name, strings.Join(Sources(), ", "), r.Decode)
	}
	r.target, r.decoder = u, decoders[r.Decode]
	return nil
}

// LoadRoutes reads a routes file.
func LoadRoutes(path string) ([]Route, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("routes: %w", err)
	}
	return ParseRoutes(data)
}
