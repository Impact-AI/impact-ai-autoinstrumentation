// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sync/atomic"
)

// The list impact.ai publishes (contract impact.cover.v1).
const (
	// Path is the ingest route that serves the list, with the install key as bearer, and supports ETag.
	Path = "/v1/inventory/cover"
	// SchemaV1 is the schema name of the list.
	SchemaV1 = "impact.cover.v1"
	// MaxEntries and MaxListBytes bound a list; a bigger one is refused whole.
	MaxEntries   = 2000
	MaxListBytes = 1 << 20
)

// Entry is one workload to cover. Lang says which instrumentation to ask for ("python" or "node"); Container limits
// it to one container of the pod; Musl says the image is Alpine (Python only).
type Entry struct {
	Namespace string `json:"namespace"`
	Kind      string `json:"kind"`
	Name      string `json:"name"`
	Lang      string `json:"lang"`
	Container string `json:"container,omitempty"`
	Musl      bool   `json:"musl,omitempty"`
}

// List is the document of contract impact.cover.v1. Automatic is the cluster's setting: when it is off, nothing is
// covered, whatever the workloads say.
type List struct {
	Schema    string  `json:"schema"`
	Version   int64   `json:"version"`
	Automatic bool    `json:"automatic"`
	Workloads []Entry `json:"workloads"`

	// Skipped counts the entries ParseList left out because this kit does not inject their language;
	// SkippedLanguages counts them by language. Neither is on the wire.
	Skipped          int            `json:"-"`
	SkippedLanguages map[string]int `json:"-"`
}

// A language this kit does not know is named in SkippedLanguages when it looks like a language name; the first
// maxSkippedLanguages such names are kept and the rest are counted under otherLanguages, so the log stays small.
const (
	maxSkippedLanguages = 8
	otherLanguages      = "other"
)

var (
	kinds        = map[string]bool{"Deployment": true, "StatefulSet": true, "DaemonSet": true, "CronJob": true, "Job": true}
	languages    = map[string]bool{"python": true, "node": true}
	languageName = regexp.MustCompile(`^[a-z][a-z0-9]{0,15}$`)
	dnsLabel     = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	dnsSubdomain = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)
)

func (e Entry) validate() error {
	switch {
	case len(e.Namespace) > 63 || !dnsLabel.MatchString(e.Namespace):
		return errors.New("namespace is not a DNS label")
	case !kinds[e.Kind]:
		return fmt.Errorf("kind %q is not Deployment, StatefulSet, DaemonSet, CronJob or Job", e.Kind)
	case len(e.Name) > 253 || !dnsSubdomain.MatchString(e.Name):
		return errors.New("name is not a DNS subdomain")
	case !languageName.MatchString(e.Lang):
		return errors.New("lang is not a language name")
	case e.Container != "" && (len(e.Container) > 63 || !dnsLabel.MatchString(e.Container)):
		return errors.New("container is not a DNS label")
	}
	return nil
}

// ParseList reads and checks a list. One bad thing refuses the whole list, so that the caller keeps the last good one:
// a body over 1 MiB, anything but the schema impact.cover.v1, a negative version, more than 2,000 entries, or an entry
// that is not a workload kind, a language name and valid names. Fields it does not know are ignored.
//
// An entry in a language this kit does not inject (a newer impact.ai may list java or dotnet) is valid but left out
// and counted in Skipped, so that a newer list never stops the coverage of the languages this kit knows.
func ParseList(data []byte) (List, error) {
	if len(data) > MaxListBytes {
		return List{}, fmt.Errorf("the list is %d bytes, over the limit of %d", len(data), MaxListBytes)
	}
	var list List
	if err := json.Unmarshal(data, &list); err != nil {
		return List{}, fmt.Errorf("the list is not valid: %w", err)
	}
	if list.Schema != SchemaV1 {
		return List{}, fmt.Errorf("the list has schema %q, want %s", list.Schema, SchemaV1)
	}
	if list.Version < 0 {
		return List{}, errors.New("the list has a negative version")
	}
	if len(list.Workloads) > MaxEntries {
		return List{}, fmt.Errorf("the list has %d workloads, over the limit of %d", len(list.Workloads), MaxEntries)
	}
	known := list.Workloads[:0:0]
	for i, e := range list.Workloads {
		if err := e.validate(); err != nil {
			return List{}, fmt.Errorf("workload %d: %w", i, err)
		}
		if languages[e.Lang] {
			known = append(known, e)
			continue
		}
		list.skip(e.Lang)
	}
	if list.Skipped > 0 {
		list.Workloads = known
	}
	return list, nil
}

func (l *List) skip(lang string) {
	if l.SkippedLanguages == nil {
		l.SkippedLanguages = map[string]int{}
	}
	l.Skipped++
	if _, named := l.SkippedLanguages[lang]; !named && len(l.SkippedLanguages) >= maxSkippedLanguages {
		lang = otherLanguages
	}
	l.SkippedLanguages[lang]++
}

type workloadKey struct{ namespace, kind, name string }

// index is what the Store serves: immutable once built.
type index struct {
	version   int64
	automatic bool
	skipped   int
	entries   map[workloadKey]Entry
}

// Store holds the current list for the webhook. Reads take no lock: the list is replaced as a whole.
type Store struct {
	current atomic.Pointer[index]
}

// Info says what the Store holds, for logs.
type Info struct {
	Loaded    bool
	Version   int64
	Automatic bool
	// Workloads is how many workloads are covered: none while automatic coverage is off.
	Workloads int
	// Skipped is how many entries the list had in languages this kit does not inject.
	Skipped int
}

// Set replaces the list. With automatic coverage off the Store keeps the version and finds nothing.
func (s *Store) Set(list List) {
	idx := &index{version: list.Version, automatic: list.Automatic, skipped: list.Skipped, entries: map[workloadKey]Entry{}}
	if list.Automatic {
		for _, e := range list.Workloads {
			idx.entries[workloadKey{e.Namespace, e.Kind, e.Name}] = e
		}
	}
	s.current.Store(idx)
}

// Clear forgets the list: nothing is covered until a new one arrives.
func (s *Store) Clear() { s.current.Store(nil) }

// Active reports whether a list is loaded and automatic coverage is on.
func (s *Store) Active() bool {
	idx := s.current.Load()
	return idx != nil && idx.automatic
}

// Lookup finds the entry for a workload.
func (s *Store) Lookup(namespace, kind, name string) (Entry, bool) {
	idx := s.current.Load()
	if idx == nil {
		return Entry{}, false
	}
	e, ok := idx.entries[workloadKey{namespace, kind, name}]
	return e, ok
}

// Info describes the current list.
func (s *Store) Info() Info {
	idx := s.current.Load()
	if idx == nil {
		return Info{}
	}
	return Info{Loaded: true, Version: idx.version, Automatic: idx.automatic, Workloads: len(idx.entries), Skipped: idx.skipped}
}
