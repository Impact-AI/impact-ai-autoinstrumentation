// SPDX-License-Identifier: Apache-2.0

package relay

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strings"
)

// The default bounds of the copy a decoder is given.
const (
	// MaxBody is the largest body, as it arrived, that is decoded.
	MaxBody = 32 << 20
	// MaxDecodedBody is the largest body once decompressed.
	MaxDecodedBody = 64 << 20
)

// Limits bound the copy a decoder is given. A zero field means its default.
type Limits struct {
	Body    int // bytes of the body as it arrived; MaxBody when zero
	Decoded int // bytes once decompressed; MaxDecodedBody when zero
}

// NewRequest makes the copy of a relayed request that the decoder d reads, with
// the default limits. body is the request's body as it arrived; it is not
// changed, and the Request never shares its memory.
func NewRequest(r *http.Request, body []byte, d Decoder) (Request, error) {
	return Limits{}.NewRequest(r, body, d)
}

// NewRequest makes the copy of a relayed request that the decoder d reads:
// only the headers d declares, never one whose name looks like a credential;
// the query without its credential parameters; the body decompressed. A body
// over the limits, or one that cannot be decompressed, is a *PayloadError.
func (l Limits) NewRequest(r *http.Request, body []byte, d Decoder) (Request, error) {
	if l.Body <= 0 {
		l.Body = MaxBody
	}
	if l.Decoded <= 0 {
		l.Decoded = MaxDecodedBody
	}
	if len(body) > l.Body {
		return Request{}, Unreadable(fmt.Sprintf("the body is larger than %d bytes", l.Body), nil)
	}
	decoded, err := decompress(r.Header.Get("Content-Encoding"), body, l.Decoded)
	if err != nil {
		return Request{}, err
	}
	header := http.Header{}
	for _, name := range d.Headers() {
		key := http.CanonicalHeaderKey(name)
		if CredentialName(key) {
			continue
		}
		if values := r.Header.Values(key); len(values) > 0 {
			header[key] = slices.Clone(values)
		}
	}
	return Request{
		Method:      r.Method,
		Path:        r.URL.Path,
		RawQuery:    withoutCredentials(r.URL.RawQuery),
		Header:      header,
		ContentType: mediaType(r.Header.Get("Content-Type")),
		Body:        decoded,
	}, nil
}

// credentialWords are parts of the names of headers and query parameters that
// carry keys, tokens, passwords or sessions: DD-API-KEY, X-License-Key, Api-Key,
// license_key, api_key, Authorization, Cookie, X-Amz-Security-Token and the like.
var credentialWords = []string{
	"key", "token", "secret", "auth", "cookie", "passw", "pwd", "credential", "signature", "session", "bearer", "jwt",
}

// CredentialName reports whether a header or query parameter name looks like it
// carries a credential. It errs on the side of yes: such a header or parameter
// never reaches a decoder.
func CredentialName(name string) bool {
	lower := strings.ToLower(name)
	for _, word := range credentialWords {
		if strings.Contains(lower, word) {
			return true
		}
	}
	return false
}

// withoutCredentials is the raw query without the parameters whose names look
// like credentials, or cannot be read. The rest keep their order and escaping;
// both '&' and ';' separate parameters.
// CleanQuery is a raw query without the parameters whose names look like a
// credential, as Match and Request.RawQuery see it.
func CleanQuery(rawQuery string) string { return withoutCredentials(rawQuery) }

func withoutCredentials(rawQuery string) string {
	var kept []string
	for _, pair := range strings.FieldsFunc(rawQuery, func(r rune) bool { return r == '&' || r == ';' }) {
		name, _, _ := strings.Cut(pair, "=")
		unescaped, err := url.QueryUnescape(name)
		if err != nil || CredentialName(unescaped) {
			continue
		}
		kept = append(kept, pair)
	}
	return strings.Join(kept, "&")
}

func mediaType(value string) string {
	if value == "" {
		return ""
	}
	if parsed, _, err := mime.ParseMediaType(value); err == nil {
		return parsed
	}
	media, _, _ := strings.Cut(value, ";")
	return strings.ToLower(strings.TrimSpace(media))
}

var codingName = regexp.MustCompile(`^[a-z0-9.+-]{1,20}$`)

// decompress undoes the content codings in the reverse of the order they were
// applied, and always returns memory of its own.
func decompress(encoding string, body []byte, limit int) ([]byte, error) {
	data, own := body, false
	codings := strings.Split(encoding, ",")
	for i := len(codings) - 1; i >= 0 && len(data) > 0; i-- {
		coding := strings.ToLower(strings.TrimSpace(codings[i]))
		var reader io.Reader
		switch coding {
		case "", "identity":
			continue
		case "gzip", "x-gzip":
			zr, err := gzip.NewReader(bytes.NewReader(data))
			if err != nil {
				return nil, Unreadable("gzip", err)
			}
			reader = zr
		case "deflate":
			// HTTP's deflate is a zlib stream; some clients send a raw deflate stream.
			if zr, err := zlib.NewReader(bytes.NewReader(data)); err == nil {
				reader = zr
			} else {
				reader = flate.NewReader(bytes.NewReader(data))
			}
		default:
			if !codingName.MatchString(coding) {
				coding = "unknown"
			}
			return nil, Unreadable("the content encoding "+coding+" is not gzip or deflate", nil)
		}
		out, err := io.ReadAll(io.LimitReader(reader, int64(limit)+1))
		if err != nil {
			return nil, Unreadable(coding, err)
		}
		if len(out) > limit {
			return nil, Unreadable(fmt.Sprintf("the body is larger than %d bytes once decompressed", limit), nil)
		}
		data, own = out, true
	}
	if len(data) == 0 {
		return nil, nil
	}
	if !own {
		return bytes.Clone(data), nil
	}
	return data, nil
}
