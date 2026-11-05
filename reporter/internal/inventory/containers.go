// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"maps"
	"path"
	"slices"
	"unicode"
	"unicode/utf8"

	"github.com/impact-ai/impact-ai-k8s/reporter/internal/kube"
)

const (
	maxEnvNames         = 500
	maxNameLength       = 253
	maxExecutableLength = 64
	maxImageLength      = 1024
)

// usable reports whether s is a plain name that may leave the cluster: not
// empty, not too long, valid UTF-8, and free of whitespace, control characters
// and '='. Anything else could be a value or a fragment of one.
func usable(s string, limit int) bool {
	if s == "" || len(s) > limit || !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if r == '=' || unicode.IsSpace(r) || unicode.IsControl(r) {
			return false
		}
	}
	return true
}

// executables returns the base name of the container's command[0], or none.
// Arguments never leave the cluster, and a first element that is more than a
// path (a shell line, an option with a value) is dropped whole.
func executables(first string) []string {
	out := []string{}
	if !usable(first, 4096) {
		return out
	}
	base := path.Base(first)
	if base == "." || base == "/" || len(base) > maxExecutableLength {
		return out
	}
	return append(out, base)
}

// containerFacts reduces a container to names.
func containerFacts(c kube.Container) Container {
	names := map[string]struct{}{}
	refs := map[SecretRef]struct{}{}
	for _, e := range c.Env {
		if !usable(e.Name, maxNameLength) {
			continue
		}
		names[e.Name] = struct{}{}
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			ref := e.ValueFrom.SecretKeyRef
			if usable(ref.Name, maxNameLength) && usable(ref.Key, maxNameLength) {
				refs[SecretRef{EnvName: e.Name, Secret: ref.Name, Key: ref.Key}] = struct{}{}
			}
		}
	}
	from := map[EnvFromRef]struct{}{}
	for _, src := range c.EnvFrom {
		if src.SecretRef != nil && usable(src.SecretRef.Name, maxNameLength) {
			from[EnvFromRef{Kind: "Secret", Name: src.SecretRef.Name}] = struct{}{}
		}
		if src.ConfigMapRef != nil && usable(src.ConfigMapRef.Name, maxNameLength) {
			from[EnvFromRef{Kind: "ConfigMap", Name: src.ConfigMapRef.Name}] = struct{}{}
		}
	}

	envNames := slices.Sorted(maps.Keys(names))
	if len(envNames) > maxEnvNames {
		envNames = envNames[:maxEnvNames]
	}
	secretRefs := slices.SortedFunc(maps.Keys(refs), func(a, b SecretRef) int {
		return compareStrings(a.EnvName, b.EnvName, a.Secret, b.Secret, a.Key, b.Key)
	})
	envFrom := slices.SortedFunc(maps.Keys(from), func(a, b EnvFromRef) int {
		return compareStrings(a.Kind, b.Kind, a.Name, b.Name)
	})
	image := c.Image
	if len(image) > maxImageLength {
		image = image[:maxImageLength]
	}
	return Container{
		Name:        c.Name,
		Image:       image,
		Executables: executables(c.Command.First),
		EnvNames:    nonNil(envNames),
		SecretRefs:  nonNil(secretRefs),
		EnvFrom:     nonNil(envFrom),
	}
}

// compareStrings compares pairs of strings in order: a1 b1 a2 b2 ...
func compareStrings(pairs ...string) int {
	for i := 0; i+1 < len(pairs); i += 2 {
		switch {
		case pairs[i] < pairs[i+1]:
			return -1
		case pairs[i] > pairs[i+1]:
			return 1
		}
	}
	return 0
}

// nonNil makes sure an empty list encodes as [] and not null.
func nonNil[T any](list []T) []T {
	if list == nil {
		return []T{}
	}
	return list
}

// filterAnnotations keeps the allowlisted annotations. The decoder already drops
// the others; this is the second line of defence at the point of output.
func filterAnnotations(in map[string]string) map[string]string {
	out := map[string]string{}
	for key, value := range in {
		if kube.IsAllowedAnnotation(key) {
			out[key] = kube.CapAnnotationValue(value)
		}
	}
	return out
}
