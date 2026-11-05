// SPDX-License-Identifier: Apache-2.0

package cover

import (
	"regexp"
	"strings"
)

// OwnerRef is an owner reference of a pod: the part the cover pod reads.
type OwnerRef struct {
	Kind       string
	Name       string
	Controller bool
}

// Workload names the top-level owner of a pod, inside the pod's namespace.
type Workload struct {
	Kind string
	Name string
}

// A Job that a CronJob created is named <cronjob>-<scheduled time in minutes since the epoch>: eight digits until the
// year 2160.
var cronJobRun = regexp.MustCompile(`^(.+)-[0-9]{8}$`)

// Candidates lists the workloads a pod may belong to, most specific first, from its controller reference alone. It
// is the rule the reporter uses to attribute pods to workloads (inventory.ownerKey), kept in step with it:
//
//   - a ReplicaSet named <deployment>-<pod-template-hash> belongs to that Deployment (the pod must carry the label);
//   - a StatefulSet or DaemonSet owns its pods directly;
//   - a Job is itself, and also the CronJob of its name without the eight-digit run time, because a pod does not say
//     whether its Job was made by a CronJob;
//   - anything else (no owner, a Rollout, a custom resource, a node) belongs to no workload the kit covers.
func Candidates(owners []OwnerRef, labels map[string]string) []Workload {
	var owner *OwnerRef
	for i := range owners {
		if owners[i].Controller {
			owner = &owners[i]
			break
		}
	}
	if owner == nil {
		return nil
	}
	switch owner.Kind {
	case "ReplicaSet":
		hash := labels["pod-template-hash"]
		suffix := "-" + hash
		if hash == "" || !strings.HasSuffix(owner.Name, suffix) || len(owner.Name) == len(suffix) {
			return nil
		}
		return []Workload{{Kind: "Deployment", Name: strings.TrimSuffix(owner.Name, suffix)}}
	case "StatefulSet", "DaemonSet":
		return []Workload{{Kind: owner.Kind, Name: owner.Name}}
	case "Job":
		out := []Workload{{Kind: "Job", Name: owner.Name}}
		if m := cronJobRun.FindStringSubmatch(owner.Name); m != nil {
			out = append(out, Workload{Kind: "CronJob", Name: m[1]})
		}
		return out
	}
	return nil
}
