// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// selfSigned writes a certificate and key valid for 127.0.0.1 and "localhost" into dir and returns the certificate.
func selfSigned(t *testing.T, dir, commonName string) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, _ := rand.Int(rand.Reader, big.NewInt(1<<62))
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: commonName},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		DNSNames:              []string{"localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	write := func(name, kind string, der []byte) {
		if err := os.WriteFile(filepath.Join(dir, name), pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der}), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("tls.crt", "CERTIFICATE", der)
	write("tls.key", "EC PRIVATE KEY", keyDER)
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

// later makes the files look newer, as the kubelet's rewrite of a mounted Secret does.
func later(t *testing.T, dir string, by time.Duration) {
	t.Helper()
	at := time.Now().Add(by)
	for _, name := range []string{"tls.crt", "tls.key"} {
		if err := os.Chtimes(filepath.Join(dir, name), at, at); err != nil {
			t.Fatal(err)
		}
	}
}

func served(t *testing.T, r *CertReloader) string {
	t.Helper()
	cert, err := r.GetCertificate(&tls.ClientHelloInfo{})
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	return leaf.Subject.CommonName
}

func TestTheCertificateIsLoadedAtStartAndServedWithTLS12OrBetter(t *testing.T) {
	dir := t.TempDir()
	selfSigned(t, dir, "first")
	r, err := NewCertReloader(dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := served(t, r); got != "first" {
		t.Errorf("served %q", got)
	}
	cfg := r.TLSConfig()
	if cfg.MinVersion != tls.VersionTLS12 || cfg.GetCertificate == nil || len(cfg.Certificates) != 0 {
		t.Errorf("TLS config = %+v", cfg)
	}
}

func TestACertificateThatIsNotThereOrNotValidStopsTheStart(t *testing.T) {
	if _, err := NewCertReloader(t.TempDir()); err == nil || !strings.Contains(err.Error(), "tls.crt") {
		t.Errorf("an empty directory: %v", err)
	}
	dir := t.TempDir()
	selfSigned(t, dir, "x")
	if err := os.WriteFile(filepath.Join(dir, "tls.key"), []byte("not a key"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewCertReloader(dir); err == nil {
		t.Error("a key that is not a key was accepted")
	}
	if _, err := NewCertReloader(filepath.Join(dir, "missing")); err == nil {
		t.Error("a missing directory was accepted")
	}
}

func TestANewCertificateIsServedOnceTheFilesChange(t *testing.T) {
	dir := t.TempDir()
	selfSigned(t, dir, "first")
	r, err := NewCertReloader(dir)
	if err != nil {
		t.Fatal(err)
	}
	selfSigned(t, dir, "second")
	later(t, dir, time.Minute)
	if got := served(t, r); got != "second" {
		t.Errorf("served %q after the files changed, want second", got)
	}
	// Unchanged files are not read again: the same certificate comes back.
	a, _ := r.GetCertificate(&tls.ClientHelloInfo{})
	b, _ := r.GetCertificate(&tls.ClientHelloInfo{})
	if a != b {
		t.Error("the certificate was parsed again although the files did not change")
	}
}

func TestABrokenReplacementKeepsTheCertificateInUse(t *testing.T) {
	dir := t.TempDir()
	selfSigned(t, dir, "first")
	r, err := NewCertReloader(dir)
	if err != nil {
		t.Fatal(err)
	}
	// A rewrite caught half way: the key is there, the certificate is cut off.
	if err := os.WriteFile(filepath.Join(dir, "tls.crt"), []byte("-----BEGIN CERT"), 0o600); err != nil {
		t.Fatal(err)
	}
	later(t, dir, time.Minute)
	if got := served(t, r); got != "first" {
		t.Errorf("served %q after a broken rewrite, want the old certificate", got)
	}
	// Once the rewrite is complete it is picked up.
	selfSigned(t, dir, "third")
	later(t, dir, 2*time.Minute)
	if got := served(t, r); got != "third" {
		t.Errorf("served %q, want third", got)
	}
}
