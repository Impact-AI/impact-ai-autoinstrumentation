// SPDX-License-Identifier: Apache-2.0

package notice

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The reporter reads. The one thing it may write is the scale of its own
// Deployment during an uninstall, so MergePatch must not be called from
// anywhere but this package.
func TestOnlyTheUninstallNoticeCanWrite(t *testing.T) {
	root := filepath.Join("..", "..")
	var callers []string
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
		if strings.Contains(string(src), "MergePatch(") {
			rel, _ := filepath.Rel(root, path)
			callers = append(callers, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(callers) == 0 {
		t.Fatal("found no MergePatch at all; did the scan break?")
	}
	for _, rel := range callers {
		if rel != "internal/kube/client.go" && !strings.HasPrefix(rel, "internal/notice/") {
			t.Errorf("%s writes to the cluster; only the uninstall notice may", rel)
		}
	}
}

// The Kubernetes client has exactly one non-GET entry point.
func TestTheClientHasNoOtherWrites(t *testing.T) {
	src, err := os.ReadFile(filepath.Join("..", "kube", "client.go"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(src)
	for _, forbidden := range []string{"http.MethodPost", "http.MethodPut", "http.MethodDelete"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("the Kubernetes client mentions %s", forbidden)
		}
	}
	if n := strings.Count(text, "http.MethodPatch"); n != 1 {
		t.Errorf("http.MethodPatch appears %d times, want once (MergePatch)", n)
	}
}
