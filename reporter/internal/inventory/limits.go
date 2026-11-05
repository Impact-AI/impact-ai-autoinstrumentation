// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"slices"
)

// Encoded is a snapshot ready to send.
type Encoded struct {
	// Body is the gzip-compressed JSON request body.
	Body []byte
	// Snapshot is what Body holds: the input without the workloads the limits cut.
	Snapshot *Snapshot
	// Workloads is how many workloads Body holds; Dropped how many the limits
	// cut. RawBytes is the size before compression.
	Workloads int
	Dropped   int
	RawBytes  int
}

// encode marshals the snapshot with exactly the given workloads (sorted by
// identity) and gzips it. The snapshot itself is not changed.
func encode(s *Snapshot, workloads []Workload) (body []byte, raw int, kept *Snapshot, err error) {
	out := *s
	out.Workloads = nonNil(slices.Clone(workloads))
	sortWorkloads(out.Workloads)
	data, err := json.Marshal(&out)
	if err != nil {
		return nil, 0, nil, err
	}
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(data); err != nil {
		return nil, 0, nil, err
	}
	if err := zw.Close(); err != nil {
		return nil, 0, nil, err
	}
	return buf.Bytes(), len(data), &out, nil
}

// Encode produces the request body within ingest's limits: at most
// MaxWorkloads workloads and MaxBodyBytes of gzip. Over the limits it keeps the
// workloads with the most pods (then by identity) and drops the rest, using the
// largest set whose body still fits.
func Encode(s *Snapshot) (Encoded, error) {
	ordered := slices.Clone(s.Workloads)
	slices.SortStableFunc(ordered, byPriority)
	n := min(len(ordered), MaxWorkloads)

	body, raw, kept, err := encode(s, ordered[:n])
	if err != nil {
		return Encoded{}, err
	}
	if len(body) > MaxBodyBytes {
		// Invariant: the first lo workloads fit, the first hi do not.
		lo, hi := 0, n
		if body, raw, kept, err = encode(s, nil); err != nil {
			return Encoded{}, err
		}
		for hi-lo > 1 {
			mid := lo + (hi-lo)/2
			b, r, k, err := encode(s, ordered[:mid])
			if err != nil {
				return Encoded{}, err
			}
			if len(b) <= MaxBodyBytes {
				lo, body, raw, kept = mid, b, r, k
			} else {
				hi = mid
			}
		}
		n = lo
	}
	return Encoded{Body: body, Snapshot: kept, Workloads: n, Dropped: len(s.Workloads) - n, RawBytes: raw}, nil
}
