// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// The uninstall script (scripts/uninstall-cluster-kit.sh) takes the marks off the pods the cover pod marked, under the
// operator's own rights. What it removes must be exactly what the cover pod may add: fewer would leave the kit's marks
// behind, more would take away what is the customer's. And it finds the pods by the label the webhook puts on them.
func TestTheUninstallScriptTakesOffExactlyWhatTheCoverPodAddsAndFindsThePodsByItsLabel(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "scripts", "uninstall-cluster-kit.sh"))
	if err != nil {
		t.Fatal(err)
	}
	script := string(raw)

	match := regexp.MustCompile(`(?m)^STRIP='(\{.*\})'$`).FindStringSubmatch(script)
	if match == nil {
		t.Fatal("the script has no STRIP patch")
	}
	var patch struct {
		Metadata struct {
			Annotations map[string]*string `json:"annotations"`
			Labels      map[string]*string `json:"labels"`
		} `json:"metadata"`
	}
	if err := json.Unmarshal([]byte(match[1]), &patch); err != nil {
		t.Fatalf("the STRIP patch is not JSON: %v", err)
	}
	for section, keys := range map[string]map[string]*string{"annotations": patch.Metadata.Annotations, "labels": patch.Metadata.Labels} {
		for key, value := range keys {
			if value != nil {
				t.Errorf("%s %s = %q: the strip only removes", section, key, *value)
			}
		}
	}

	var annotations []string
	for key := range patch.Metadata.Annotations {
		annotations = append(annotations, key)
	}
	sort.Strings(annotations)
	want := append([]string(nil), allowedKeys...)
	sort.Strings(want)
	if !reflect.DeepEqual(annotations, want) {
		t.Errorf("the script removes the annotations\n%v\nthe cover pod may add\n%v", annotations, want)
	}
	if len(patch.Metadata.Labels) != 1 {
		t.Errorf("the script removes the labels %v, want only the marker", patch.Metadata.Labels)
	}
	if _, ok := patch.Metadata.Labels[MarkerLabel]; !ok {
		t.Errorf("the script does not remove the label %s", MarkerLabel)
	}
	if selector := "--selector " + MarkerLabel + "=" + MarkerLabelValue; !strings.Contains(script, selector) {
		t.Errorf("the script does not select the pods with %q", selector)
	}
}
