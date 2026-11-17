// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"
	"github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay/otlp"
)

// maxDecodedBytes bounds what a decompressed batch may grow to.
const maxDecodedBytes = 64 << 20

// payload is a copy ready to post to impact.ai's OTLP/HTTP traces route.
type payload struct {
	body        []byte
	contentType string
	encoding    string // Content-Encoding of body: "" or gzip
}

// errUnreadable marks a batch that was forwarded but cannot be copied.
var errUnreadable = errors.New("unreadable")

func unreadable(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errUnreadable, fmt.Sprintf(format, args...))
}

// decode turns a queued batch into its OTLP copy. It runs in the copier, in the background of the forwarding. A nil
// payload with a nil error means there is nothing to copy.
func decode(it Item) (*payload, error) {
	switch it.Kind {
	case KindOTLP:
		if it.ContentType != "application/x-protobuf" && it.ContentType != "application/json" {
			return nil, unreadable("content type %q is not OTLP", it.ContentType)
		}
		body := it.Body
		switch strings.ToLower(strings.TrimSpace(it.Encoding)) {
		case "", "identity":
		case "gzip":
			var err error
			if body, err = gunzip(body); err != nil {
				return nil, err
			}
		default:
			return nil, unreadable("content encoding %q", it.Encoding)
		}
		return stamp(body, it.ContentType, copyProvenance(it))
	case KindGRPC:
		msg, err := unframeGRPC(it.Body, it.Encoding)
		if err != nil {
			return nil, err
		}
		return stamp(msg, "application/x-protobuf", copyProvenance(it))
	case KindRelay:
		return decodeRelay(it)
	}
	return nil, unreadable("kind %d", it.Kind)
}

// copyProvenance is what every resource of an OTLP copy says of where its spans came from: the route's tool, by copy.
func copyProvenance(it Item) otlp.Provenance {
	return otlp.Provenance{Source: it.Source, Route: otlp.RouteCopy}
}

// stamp sets the provenance on an OTLP export request, replacing what the workload's own resource attributes claimed.
// A request without spans is nothing to copy.
func stamp(body []byte, contentType string, p otlp.Provenance) (*payload, error) {
	var (
		out   []byte
		spans bool
		err   error
	)
	if contentType == "application/json" {
		out, spans, err = stampJSON(body, p)
	} else {
		out, spans, err = stampProtobuf(body, p)
	}
	if err != nil {
		return nil, err
	}
	if !spans {
		return nil, nil
	}
	return &payload{body: out, contentType: contentType}, nil
}

// unframeGRPC reads the length-prefixed messages of a gRPC request body and joins them. An ExportTraceServiceRequest
// has only a repeated field, so joined messages are one message holding all their spans.
func unframeGRPC(b []byte, encoding string) ([]byte, error) {
	var out []byte
	for len(b) > 0 {
		if len(b) < 5 {
			return nil, unreadable("a gRPC frame is cut short")
		}
		flag, n := b[0], binary.BigEndian.Uint32(b[1:5])
		if uint64(len(b)-5) < uint64(n) {
			return nil, unreadable("a gRPC frame is cut short")
		}
		msg := b[5 : 5+int(n)]
		b = b[5+int(n):]
		switch flag {
		case 0:
		case 1:
			if !strings.EqualFold(strings.TrimSpace(encoding), "gzip") {
				return nil, unreadable("gRPC compression %q", encoding)
			}
			var err error
			if msg, err = gunzip(msg); err != nil {
				return nil, err
			}
		default:
			return nil, unreadable("gRPC frame flag %d", flag)
		}
		if len(out)+len(msg) > maxDecodedBytes {
			return nil, unreadable("more than %d bytes", maxDecodedBytes)
		}
		out = append(out, msg...)
	}
	return out, nil
}

func gunzip(b []byte) ([]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, unreadable("not gzip")
	}
	out, err := io.ReadAll(io.LimitReader(zr, maxDecodedBytes+1))
	if err != nil {
		return nil, unreadable("broken gzip")
	}
	if len(out) > maxDecodedBytes {
		return nil, unreadable("more than %d bytes once decompressed", maxDecodedBytes)
	}
	return out, nil
}

// decodeRelay decodes a relayed request with its source's decoder, the way the relay contract says: a copy made by
// relay.NewRequest (credentials removed, the body decompressed and bounded), then relay.Decode, which sets the relay's
// provenance and turns a decoder's panic into an error.
func decodeRelay(it Item) (*payload, error) {
	call := it.Relay
	if call == nil || decoders[call.Source] == nil {
		return nil, unreadable("no decoder")
	}
	d := decoders[call.Source]
	header := call.Header.Clone()
	if header == nil {
		header = http.Header{}
	}
	if it.ContentType != "" {
		header.Set("Content-Type", it.ContentType)
	}
	if it.Encoding != "" {
		header.Set("Content-Encoding", it.Encoding)
	}
	req, err := relay.NewRequest(&http.Request{
		Method: call.Method,
		URL:    &url.URL{Path: call.Path, RawQuery: call.Query},
		Header: header,
	}, it.Body, d)
	if err != nil {
		return nil, unreadable("%s", relayReason(err))
	}
	traces, err := relay.Decode(d, req)
	if err != nil {
		return nil, unreadable("%s", relayReason(err))
	}
	if len(traces.ResourceSpans) == 0 {
		return nil, nil
	}
	body, err := otlp.Marshal(traces)
	if err != nil {
		return nil, unreadable("the decoded spans cannot be written as OTLP JSON")
	}
	return &payload{body: body, contentType: "application/json"}, nil
}

// relayReason says why a relayed request was not copied, in words the decoder chose and never the payload's.
func relayReason(err error) string {
	var payloadErr *relay.PayloadError
	if errors.As(err, &payloadErr) {
		return payloadErr.Reason
	}
	return "the decoder failed"
}
