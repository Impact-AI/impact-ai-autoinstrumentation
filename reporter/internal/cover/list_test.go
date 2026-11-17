// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
)

const goodList = `{"schema":"impact.cover.v1","version":17,"automatic":true,"workloads":[
	{"namespace":"shop","kind":"Deployment","name":"api","lang":"python","container":"app","musl":true},
	{"namespace":"shop","kind":"StatefulSet","name":"db","lang":"node"},
	{"namespace":"edge","kind":"CronJob","name":"nightly.report","lang":"python"}
]}`

func TestParseList(t *testing.T) {
	list, err := ParseList([]byte(goodList))
	if err != nil {
		t.Fatal(err)
	}
	if list.Schema != SchemaV1 || list.Version != 17 || !list.Automatic || len(list.Workloads) != 3 {
		t.Fatalf("list = %+v", list)
	}
	first := list.Workloads[0]
	if first != (Entry{Namespace: "shop", Kind: "Deployment", Name: "api", Lang: "python", Container: "app", Musl: true}) {
		t.Errorf("first entry = %+v", first)
	}
}

func TestParseListIgnoresFieldsItDoesNotKnow(t *testing.T) {
	in := `{"schema":"impact.cover.v1","version":2,"automatic":true,"generatedAt":"x","workloads":[
		{"namespace":"shop","kind":"Job","name":"j","lang":"python","reason":"strong evidence","extra":{"a":1}}]}`
	list, err := ParseList([]byte(in))
	if err != nil || len(list.Workloads) != 1 {
		t.Fatalf("list = %+v, err = %v", list, err)
	}
}

func TestParseListRefusesTheWholeListForOneBadThing(t *testing.T) {
	wrap := func(workloads string) string {
		return `{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[` + workloads + `]}`
	}
	good := `{"namespace":"shop","kind":"Deployment","name":"api","lang":"python"}`
	cases := map[string]string{
		"not JSON":                      `{"schema":`,
		"an array":                      `[]`,
		"no schema":                     `{"version":1,"automatic":true,"workloads":[]}`,
		"another schema":                `{"schema":"impact.cover.v2","version":1,"automatic":true,"workloads":[]}`,
		"a negative version":            `{"schema":"impact.cover.v1","version":-1,"automatic":true,"workloads":[]}`,
		"a version that is text":        `{"schema":"impact.cover.v1","version":"17","automatic":true,"workloads":[]}`,
		"automatic that is text":        `{"schema":"impact.cover.v1","version":1,"automatic":"yes","workloads":[]}`,
		"an unknown kind":               wrap(good + `,{"namespace":"shop","kind":"Pod","name":"p","lang":"python"}`),
		"a lowercase kind":              wrap(`{"namespace":"shop","kind":"deployment","name":"p","lang":"python"}`),
		"a language that is not a name": wrap(`{"namespace":"shop","kind":"Deployment","name":"p","lang":"Java 21"}`),
		"a language that is a number":   wrap(`{"namespace":"shop","kind":"Deployment","name":"p","lang":21}`),
		"an unknown language, bad name": wrap(`{"namespace":"shop","kind":"Deployment","name":"a/b","lang":"java"}`),
		"no language":                   wrap(`{"namespace":"shop","kind":"Deployment","name":"p"}`),
		"an empty namespace":            wrap(`{"namespace":"","kind":"Deployment","name":"p","lang":"python"}`),
		"an uppercase namespace":        wrap(`{"namespace":"Shop","kind":"Deployment","name":"p","lang":"python"}`),
		"a namespace with a dot":        wrap(`{"namespace":"a.b","kind":"Deployment","name":"p","lang":"python"}`),
		"a namespace of 64 letters":     wrap(`{"namespace":"` + strings.Repeat("a", 64) + `","kind":"Deployment","name":"p","lang":"python"}`),
		"an empty name":                 wrap(`{"namespace":"shop","kind":"Deployment","name":"","lang":"python"}`),
		"a name with a slash":           wrap(`{"namespace":"shop","kind":"Deployment","name":"a/b","lang":"python"}`),
		"a name with a space":           wrap(`{"namespace":"shop","kind":"Deployment","name":"a b","lang":"python"}`),
		"a name of 254 letters":         wrap(`{"namespace":"shop","kind":"Deployment","name":"` + strings.Repeat("a", 254) + `","lang":"python"}`),
		"a bad container name":          wrap(`{"namespace":"shop","kind":"Deployment","name":"p","lang":"python","container":"App_1"}`),
		"a container of 64 letters":     wrap(`{"namespace":"shop","kind":"Deployment","name":"p","lang":"python","container":"` + strings.Repeat("a", 64) + `"}`),
		"musl that is text":             wrap(`{"namespace":"shop","kind":"Deployment","name":"p","lang":"python","musl":"yes"}`),
		"an entry that is not object":   wrap(good + `,"shop/api"`),
		"workloads that is not a list":  `{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":{}}`,
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseList([]byte(in)); err == nil {
				t.Errorf("ParseList accepted %s", in)
			}
		})
	}
	// ... and the same list without the bad entry is fine.
	if _, err := ParseList([]byte(wrap(good))); err != nil {
		t.Errorf("the good entry alone: %v", err)
	}
}

func TestParseListSkipsAnEntryInALanguageItDoesNotKnow(t *testing.T) {
	// A newer impact.ai lists languages this kit does not inject. Refusing the whole list would keep the last one for
	// ever and stop automatic coverage of the languages it does know, so such an entry is skipped and counted.
	in := `{"schema":"impact.cover.v1","version":9,"automatic":true,"workloads":[
		{"namespace":"shop","kind":"Deployment","name":"api","lang":"python"},
		{"namespace":"shop","kind":"Deployment","name":"orders","lang":"java"},
		{"namespace":"shop","kind":"StatefulSet","name":"ledger","lang":"java","container":"app"},
		{"namespace":"edge","kind":"Deployment","name":"billing","lang":"dotnet"},
		{"namespace":"edge","kind":"Deployment","name":"web","lang":"node"}
	]}`
	list, err := ParseList([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Workloads) != 2 || list.Workloads[0].Name != "api" || list.Workloads[1].Name != "web" {
		t.Errorf("workloads = %+v, want api and web", list.Workloads)
	}
	if list.Skipped != 3 || list.SkippedLanguages["java"] != 2 || list.SkippedLanguages["dotnet"] != 1 || len(list.SkippedLanguages) != 2 {
		t.Errorf("skipped = %d %v, want 3: java 2, dotnet 1", list.Skipped, list.SkippedLanguages)
	}

	var s Store
	s.Set(list)
	if _, ok := s.Lookup("shop", "Deployment", "orders"); ok {
		t.Error("a skipped entry is covered")
	}
	if _, ok := s.Lookup("shop", "Deployment", "api"); !ok {
		t.Error("a known entry beside skipped ones is not covered")
	}
	if info := s.Info(); info.Workloads != 2 || info.Skipped != 3 {
		t.Errorf("Info = %+v, want 2 workloads and 3 skipped", info)
	}

	// A list of only unknown languages is a valid, empty list.
	only := `{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[{"namespace":"shop","kind":"Job","name":"j","lang":"ruby"}]}`
	if list, err := ParseList([]byte(only)); err != nil || len(list.Workloads) != 0 || list.Skipped != 1 {
		t.Errorf("only unknown languages: %+v, %v", list, err)
	}
	// A list with nothing skipped says so.
	if list, err := ParseList([]byte(goodList)); err != nil || list.Skipped != 0 || len(list.SkippedLanguages) != 0 {
		t.Errorf("nothing to skip: %+v, %v", list, err)
	}
}

func TestSkippedLanguagesAreBounded(t *testing.T) {
	var b strings.Builder
	b.WriteString(`{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[`)
	for i := 0; i < 40; i++ {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, `{"namespace":"shop","kind":"Deployment","name":"app-%d","lang":"lang%d"}`, i, i)
	}
	b.WriteString(`]}`)
	list, err := ParseList([]byte(b.String()))
	if err != nil {
		t.Fatal(err)
	}
	if list.Skipped != 40 || len(list.SkippedLanguages) > maxSkippedLanguages+1 {
		t.Errorf("skipped = %d, %d languages named, want 40 and at most %d", list.Skipped, len(list.SkippedLanguages), maxSkippedLanguages+1)
	}
	total := 0
	for _, n := range list.SkippedLanguages {
		total += n
	}
	if total != 40 || list.SkippedLanguages[otherLanguages] == 0 {
		t.Errorf("languages = %v, want every entry counted and the rest under %q", list.SkippedLanguages, otherLanguages)
	}
}

func TestParseListLimits(t *testing.T) {
	build := func(n int) string {
		var b strings.Builder
		b.WriteString(`{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[`)
		for i := 0; i < n; i++ {
			if i > 0 {
				b.WriteByte(',')
			}
			fmt.Fprintf(&b, `{"namespace":"shop","kind":"Deployment","name":"app-%d","lang":"python"}`, i)
		}
		b.WriteString(`]}`)
		return b.String()
	}
	if list, err := ParseList([]byte(build(MaxEntries))); err != nil || len(list.Workloads) != MaxEntries {
		t.Errorf("%d entries: %v", MaxEntries, err)
	}
	if _, err := ParseList([]byte(build(MaxEntries + 1))); err == nil {
		t.Errorf("%d entries were accepted", MaxEntries+1)
	}
	huge := `{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[],"padding":"` + strings.Repeat("x", MaxListBytes) + `"}`
	if _, err := ParseList([]byte(huge)); err == nil {
		t.Error("a body over 1 MiB was accepted")
	}
	// An empty list is a valid list: automatic coverage is on and nothing qualifies yet.
	if list, err := ParseList([]byte(`{"schema":"impact.cover.v1","version":1,"automatic":true,"workloads":[]}`)); err != nil || len(list.Workloads) != 0 {
		t.Errorf("empty list: %+v, %v", list, err)
	}
	if list, err := ParseList([]byte(`{"schema":"impact.cover.v1","version":1,"automatic":true}`)); err != nil || len(list.Workloads) != 0 {
		t.Errorf("list without workloads: %+v, %v", list, err)
	}
}

func TestStore(t *testing.T) {
	var s Store
	if s.Active() {
		t.Error("an empty store is active")
	}
	if _, ok := s.Lookup("shop", "Deployment", "api"); ok {
		t.Error("an empty store found something")
	}
	list, _ := ParseList([]byte(goodList))
	s.Set(list)
	if !s.Active() {
		t.Error("a loaded, automatic store is not active")
	}
	e, ok := s.Lookup("shop", "Deployment", "api")
	if !ok || e.Container != "app" || !e.Musl || e.Lang != "python" {
		t.Errorf("Lookup = %+v, %v", e, ok)
	}
	for _, miss := range [][3]string{
		{"shop", "StatefulSet", "api"}, {"other", "Deployment", "api"}, {"shop", "Deployment", "API"}, {"shop", "deployment", "api"},
	} {
		if _, ok := s.Lookup(miss[0], miss[1], miss[2]); ok {
			t.Errorf("Lookup(%v) found an entry", miss)
		}
	}
	if info := s.Info(); !info.Loaded || info.Version != 17 || !info.Automatic || info.Workloads != 3 {
		t.Errorf("Info = %+v", info)
	}

	// Automatic coverage off: the list stays known (its version), but nothing is found.
	list.Automatic = false
	list.Version = 18
	s.Set(list)
	if s.Active() {
		t.Error("the store is active with automatic coverage off")
	}
	if _, ok := s.Lookup("shop", "Deployment", "api"); ok {
		t.Error("an entry is found with automatic coverage off")
	}
	if info := s.Info(); !info.Loaded || info.Version != 18 || info.Automatic || info.Workloads != 0 {
		t.Errorf("Info with automatic off = %+v", info)
	}

	s.Clear()
	if s.Active() || s.Info().Loaded {
		t.Error("a cleared store is still loaded")
	}
}

func TestStoreIsSafeToReadWhileItIsReplaced(t *testing.T) {
	var s Store
	list, _ := ParseList([]byte(goodList))
	s.Set(list)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					s.Lookup("shop", "Deployment", "api")
					s.Active()
					s.Info()
				}
			}
		}()
	}
	for i := 0; i < 500; i++ {
		list.Version = int64(i)
		s.Set(list)
		if i%7 == 0 {
			s.Clear()
		}
	}
	close(stop)
	wg.Wait()
}

func TestSetDoesNotKeepAReferenceToTheCallersList(t *testing.T) {
	var s Store
	list := List{Schema: SchemaV1, Version: 1, Automatic: true, Workloads: []Entry{entry("Deployment", "api")}}
	s.Set(list)
	list.Workloads[0].Name = "changed"
	if _, ok := s.Lookup("shop", "Deployment", "api"); !ok {
		t.Error("the store followed a change made by the caller")
	}
}

func TestEntryJSONNames(t *testing.T) {
	b, err := json.Marshal(Entry{Namespace: "shop", Kind: "Deployment", Name: "api", Lang: "python", Container: "app", Musl: true})
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != `{"namespace":"shop","kind":"Deployment","name":"api","lang":"python","container":"app","musl":true}` {
		t.Errorf("entry on the wire: %s", b)
	}
}
