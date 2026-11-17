// SPDX-License-Identifier: Apache-2.0

package inventory

import "strings"

// systemNamespaces are never listed.
var systemNamespaces = []string{"kube-system", "kube-public", "kube-node-lease"}

// NamespaceFilter decides which namespaces appear in the inventory.
type NamespaceFilter struct {
	include map[string]bool // empty means every namespace
	exclude map[string]bool
}

func nameSet(names ...string) map[string]bool {
	set := map[string]bool{}
	for _, n := range names {
		if n = strings.TrimSpace(n); n != "" {
			set[n] = true
		}
	}
	return set
}

// NewNamespaceFilter builds a filter. Names are exact. The system namespaces
// and the reporter's own namespace are always excluded; exclude adds to them
// and wins over include.
func NewNamespaceFilter(include, exclude []string, own string) NamespaceFilter {
	ex := nameSet(append(append([]string{own}, systemNamespaces...), exclude...)...)
	return NamespaceFilter{include: nameSet(include...), exclude: ex}
}

// Allows reports whether workloads of the namespace are listed.
func (f NamespaceFilter) Allows(namespace string) bool {
	if f.exclude[namespace] {
		return false
	}
	return len(f.include) == 0 || f.include[namespace]
}
