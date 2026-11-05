// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"crypto/tls"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// CertReloader serves the certificate of the webhook from tls.crt and tls.key in a directory (a mounted Secret) and
// reads them again when they change, so that a new certificate needs no restart. A rewrite that cannot be read, such
// as one caught half way, leaves the certificate in use in place.
type CertReloader struct {
	crt, key string

	mu      sync.Mutex
	cert    *tls.Certificate
	crtSeen fileStamp
	keySeen fileStamp
}

type fileStamp struct {
	modified time.Time
	size     int64
}

func stamp(path string) (fileStamp, error) {
	info, err := os.Stat(path) // follows the symlinks of a mounted Secret
	if err != nil {
		return fileStamp{}, err
	}
	return fileStamp{info.ModTime(), info.Size()}, nil
}

// NewCertReloader loads the certificate at once; a directory without a usable pair is an error.
func NewCertReloader(dir string) (*CertReloader, error) {
	r := &CertReloader{crt: filepath.Join(dir, "tls.crt"), key: filepath.Join(dir, "tls.key")}
	if err := r.load(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *CertReloader) load() error {
	crtSeen, err := stamp(r.crt)
	if err != nil {
		return fmt.Errorf("certificate %s: %w", r.crt, err)
	}
	keySeen, err := stamp(r.key)
	if err != nil {
		return fmt.Errorf("key %s: %w", r.key, err)
	}
	cert, err := tls.LoadX509KeyPair(r.crt, r.key)
	if err != nil {
		return fmt.Errorf("certificate %s and key %s: %w", r.crt, r.key, err)
	}
	r.mu.Lock()
	r.cert, r.crtSeen, r.keySeen = &cert, crtSeen, keySeen
	r.mu.Unlock()
	return nil
}

// GetCertificate returns the certificate in use, reading the files again first when either changed.
func (r *CertReloader) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	crtNow, crtErr := stamp(r.crt)
	keyNow, keyErr := stamp(r.key)
	r.mu.Lock()
	changed := crtErr == nil && keyErr == nil && (crtNow != r.crtSeen || keyNow != r.keySeen)
	r.mu.Unlock()
	if changed {
		if err := r.load(); err != nil {
			// Not yet complete, perhaps. Remember what was seen so that the files are not parsed at every handshake
			// until they change again.
			r.mu.Lock()
			r.crtSeen, r.keySeen = crtNow, keyNow
			r.mu.Unlock()
		}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.cert, nil
}

// TLSConfig is the configuration of the webhook's server: TLS 1.2 or better, with the reloading certificate.
func (r *CertReloader) TLSConfig() *tls.Config {
	return &tls.Config{MinVersion: tls.VersionTLS12, GetCertificate: r.GetCertificate}
}
