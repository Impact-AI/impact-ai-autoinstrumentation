// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"testing"
	"time"
)

// FuzzParseList feeds the parser what impact.ai sends and anything else that could arrive in its place. ParseList never
// panics, and a list it accepts holds at most MaxEntries entries, each valid and in a language this kit injects. The
// patch made for any accepted entry, on a pod with or without annotations and labels, touches nothing ValidatePatch
// does not allow: whatever the list says, the cover pod adds the kit's annotations and its label, and nothing else.
func FuzzParseList(f *testing.F) {
	for _, seed := range []string{
		`{"schema":"impact.cover.v1","version":3,"automatic":true,"workloads":[` +
			`{"namespace":"shop","kind":"Deployment","name":"returns","lang":"python","container":"app","musl":true}]}`,
		`{"schema":"impact.cover.v1","version":1,"workloads":[{"namespace":"a","kind":"CronJob","name":"b.c","lang":"node"},` +
			`{"namespace":"a","kind":"Job","name":"d","lang":"java"}]}`,
		`{"schema":"impact.cover.v1","version":0,"workloads":[{"namespace":"a~1b","kind":"Deployment","name":"x/y","lang":"python"}]}`,
		`{"schema":"impact.cover.v1","version":-1}`,
		`{"schema":"impact.cover.v2","workloads":[]}`,
		`not json`,
	} {
		f.Add([]byte(seed))
	}
	decider := &Decider{Instrumentation: "impact-ai/impact", Now: func() time.Time { return time.Unix(0, 0) }}
	pods := []Pod{
		{Namespace: "shop"},
		{Namespace: "shop", Annotations: map[string]string{"team": "returns"}, Labels: map[string]string{"app": "returns"}},
		{Namespace: "shop", Annotations: map[string]string{ContainerNames: "web", PythonPlatform: "glibc"}},
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		list, err := ParseList(data)
		if err != nil {
			return
		}
		if len(list.Workloads) > MaxEntries {
			t.Fatalf("accepted %d workloads, over the limit of %d", len(list.Workloads), MaxEntries)
		}
		for _, entry := range list.Workloads {
			if err := entry.validate(); err != nil {
				t.Fatalf("accepted an invalid entry %+v: %v", entry, err)
			}
			if !languages[entry.Lang] {
				t.Fatalf("accepted an entry in a language this kit does not inject: %q", entry.Lang)
			}
			for _, pod := range pods {
				if err := ValidatePatch(decider.patch(pod, entry)); err != nil {
					t.Fatalf("the patch for %+v on %+v is refused: %v", entry, pod, err)
				}
			}
		}
	})
}
