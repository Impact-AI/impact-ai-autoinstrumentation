// SPDX-License-Identifier: Apache-2.0

package source

import (
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/impact-ai/impact-autoinstrumentation/reporter/internal/relay"
)

// Every registered source declares a valid id, name and headers, has the file of
// its id, and comes in the order of the ids.
func TestEverySourceIsRegisteredOnceInItsOwnFile(t *testing.T) {
	seen := map[string]bool{}
	var ids []string
	for _, d := range All() {
		id := d.Source()
		if err := relay.Validate(d); err != nil {
			t.Errorf("%s: %v", id, err)
		}
		if seen[id] {
			t.Errorf("%s is registered twice", id)
		}
		seen[id] = true
		ids = append(ids, id)
		if _, err := os.Stat(id + ".go"); err != nil {
			t.Errorf("%s: its decoder is not in %s.go: %v", id, id, err)
		}
	}
	if len(ids) == 0 {
		t.Fatal("no source is registered")
	}
	if !slices.IsSorted(ids) {
		t.Errorf("the sources are not in the order of their ids: %v", ids)
	}

	// The other way round: every file of the package belongs to a registered source (<id>.go, <id>_<what>.go).
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasPrefix(name, "registry") {
			continue
		}
		base := strings.TrimSuffix(strings.TrimSuffix(name, ".go"), "_test")
		id, _, _ := strings.Cut(base, "_")
		if !seen[id] {
			t.Errorf("%s belongs to no registered source", name)
		}
	}
}
