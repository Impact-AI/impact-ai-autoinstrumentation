// SPDX-License-Identifier: Apache-2.0

// Package relay is the contract between the kit's relay and the decoders of the
// sources it relays: tools whose own protocol is passed to their vendor byte for
// byte while a copy is decoded into OTLP spans.
//
// The relay forwards a request first and unchanged. Only when the vendor's
// backend accepted it, and only when a decoder's Match says the request carries
// spans, does the relay make a Request with NewRequest (credentials removed, the
// body decompressed and bounded) and call Decode. Whatever a decoder does, the
// vendor has already received everything: an error is reported as a partial
// copy and never fails the forward. README.md is the contract in prose.
package relay

import (
	"fmt"
	"net/http"
	"regexp"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// Request is the copy of a relayed request that a decoder reads. It never holds
// a credential: only the headers the decoder declares are kept, and never one
// whose name looks like a credential; query parameters whose names look like a
// key or a token are removed.
type Request struct {
	Method string
	Path   string
	// RawQuery is the query without its credential parameters, still escaped.
	RawQuery string
	// Header holds the decoder's declared headers that the request carried, by
	// their canonical names.
	Header http.Header
	// ContentType is the media type of the body, lower case and without
	// parameters (application/msgpack, application/json).
	ContentType string
	// Body is a copy of the body, decompressed from gzip or deflate, at most
	// MaxDecodedBody bytes.
	Body []byte
}

// Decoder turns one source's wire format into OTLP spans. A source has one
// decoder, in its own file of the source package.
type Decoder interface {
	// Source is the source id, such as "datadog".
	Source() string
	// Name is the decoder and its version, "<decoder>/<version>", written into
	// every span it decodes (impact.source.decoder).
	Name() string
	// Headers are the names of the request headers the decoder may read. Any
	// other header, and any whose name looks like a credential, never reaches it.
	Headers() []string
	// Match reports whether a request with this method, path and query carries
	// spans to decode once the vendor's backend accepted it. The query has its
	// credential parameters removed (CleanQuery): a protocol such as New Relic's
	// names each call only in its query (?method=span_event_data).
	Match(method, path, query string) bool
	// Decode turns a request into OTLP spans. It returns a *PayloadError when
	// the payload cannot be read. It never reaches out to the network.
	Decode(Request) (otlp.Traces, error)
}

// PayloadError says that a request's payload could not be read: too large, an
// unknown or broken compression, or a format the decoder does not understand.
// The relay reports the copy as partial; the forward is not affected. Reason
// names what went wrong and never quotes the payload.
type PayloadError struct {
	Reason string
	Err    error
}

func (e *PayloadError) Error() string {
	if e.Err != nil {
		return "relay: unreadable payload: " + e.Reason + ": " + e.Err.Error()
	}
	return "relay: unreadable payload: " + e.Reason
}

func (e *PayloadError) Unwrap() error { return e.Err }

// Unreadable is a *PayloadError with this reason and cause (which may be nil).
func Unreadable(reason string, err error) error { return &PayloadError{Reason: reason, Err: err} }

var (
	sourceID    = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)
	decoderName = regexp.MustCompile(`^[a-z][a-z0-9.-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$`)
)

// Validate checks what a decoder declares: a source id of lower-case letters,
// digits and hyphens, a name "<decoder>/<version>", and headers none of which
// looks like a credential.
func Validate(d Decoder) error {
	if !sourceID.MatchString(d.Source()) {
		return fmt.Errorf("relay: source id %q is not lower-case letters, digits and hyphens", d.Source())
	}
	if !decoderName.MatchString(d.Name()) {
		return fmt.Errorf("relay: decoder %s: name %q is not <decoder>/<version>", d.Source(), d.Name())
	}
	for _, name := range d.Headers() {
		if name == "" || CredentialName(name) {
			return fmt.Errorf("relay: decoder %s declares the header %q, which may carry a credential", d.Source(), name)
		}
	}
	return nil
}

// Decode runs the decoder on the request and sets the provenance of the relay
// route on every resource of the result. A decoder that panics returns an error
// instead, so that one bad payload cannot stop the relay.
func Decode(d Decoder, req Request) (traces otlp.Traces, err error) {
	defer func() {
		if r := recover(); r != nil {
			traces, err = otlp.Traces{}, fmt.Errorf("relay: decoder %s panicked: %v", d.Name(), r)
		}
	}()
	traces, err = d.Decode(req)
	if err != nil {
		return otlp.Traces{}, err
	}
	otlp.Provenance{Source: d.Source(), Route: otlp.RouteRelay, Decoder: d.Name()}.Apply(&traces)
	return traces, nil
}
