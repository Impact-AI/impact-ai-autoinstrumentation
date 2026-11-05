// SPDX-License-Identifier: Apache-2.0

package kubesecret

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The reporter reads. Writing to the cluster is allowed in exactly these places, each for one purpose:
//
//	internal/notice    the uninstall hook: scale the reporter and the cover pod to zero, take the injection annotations
//	                   off the pods the cover pod marked, delete the key Secret
//	internal/exchange  the install hook: the key goes into the Secret Helm made (a patch; it makes and deletes none)
//	internal/kubesecret  how both reach Secrets
//
// The reporter's loop and the cover pod have no write at all. internal/notice/readonly_test.go keeps MergePatch where it
// is; this test keeps the Secret writes where they are.
func TestSecretsAreWrittenOnlyByTheTwoHooks(t *testing.T) {
	root := filepath.Join("..", "..")
	allowed := map[string]bool{
		"internal/kubesecret": true, "internal/exchange": true, "internal/notice": true,
		"cmd/reporter": true, // wires the hooks
	}
	var importers []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		src, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if strings.Contains(string(src), "reporter/internal/kubesecret") {
			rel, _ := filepath.Rel(root, filepath.Dir(path))
			importers = append(importers, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(importers) == 0 {
		t.Fatal("nothing imports kubesecret; did the scan break?")
	}
	for _, dir := range importers {
		if !allowed[dir] {
			t.Errorf("%s imports kubesecret; only the install and uninstall hooks may write Secrets", dir)
		}
	}
}

// The package builds one kind of path, and only to a Secret of a namespace.
func TestOnlySecretsOfANamespaceAreAddressed(t *testing.T) {
	src, err := os.ReadFile("kubesecret.go")
	if err != nil {
		t.Fatal(err)
	}
	text := string(src)
	if n := strings.Count(text, "/api/v1/namespaces/"); n != 1 {
		t.Errorf("kubesecret.go builds %d /api/v1/namespaces/ paths, want exactly one", n)
	}
	for _, forbidden := range []string{"/apis/", "configmaps", "/pods", "/nodes", "/clusterroles"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("kubesecret.go mentions %s", forbidden)
		}
	}
}
