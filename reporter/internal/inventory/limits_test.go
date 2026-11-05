// SPDX-License-Identifier: Apache-2.0

package inventory

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
)

// highEntropy is a long alphabet so that random names barely compress.
const highEntropy = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-!#$%&()*+,/:;<>?@[]^{}~"

func randomName(rng *rand.Rand, length int) string {
	b := make([]byte, length)
	for i := range b {
		b[i] = highEntropy[rng.IntN(len(highEntropy))]
	}
	return string(b)
}

func synthetic(i, desired, envNames, nameLength int, rng *rand.Rand) Workload {
	names := make([]string, envNames)
	for j := range names {
		names[j] = randomName(rng, nameLength)
	}
	slices.Sort(names)
	return Workload{
		Namespace:      fmt.Sprintf("ns-%04d", i%100),
		Kind:           "Deployment",
		Name:           fmt.Sprintf("workload-%05d", i),
		Pods:           Pods{Desired: desired, Ready: desired},
		ServiceAccount: "default",
		Annotations:    map[string]string{},
		Containers: []Container{{
			Name: "app", Image: "registry.example.com/shop/app:1.0",
			Executables: []string{"python"}, EnvNames: names,
			SecretRefs: []SecretRef{}, EnvFrom: []EnvFromRef{},
		}},
	}
}

func syntheticSnapshot(n int, desired func(int) int, envNames, nameLength int) *Snapshot {
	rng := rand.New(rand.NewPCG(1, 2))
	s := &Snapshot{
		Schema:     SchemaV1,
		Cluster:    Cluster{UID: "uid", Provider: "other", KubernetesVersion: "1.31.2"},
		Reporter:   ReporterInfo{Version: "test", Instance: "i", Mode: "inject"},
		ObservedAt: "2026-11-03T12:00:00Z", Sequence: 1,
	}
	for i := 0; i < n; i++ {
		s.Workloads = append(s.Workloads, synthetic(i, desired(i), envNames, nameLength, rng))
	}
	sortWorkloads(s.Workloads)
	return s
}

func decodeBody(t *testing.T, body []byte) (*Snapshot, int) {
	t.Helper()
	zr, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(zr)
	if err != nil {
		t.Fatal(err)
	}
	var s Snapshot
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatal(err)
	}
	return &s, len(raw)
}

func TestEncodeKeepsAtMostFiveThousandWorkloads(t *testing.T) {
	snap := syntheticSnapshot(6000, func(i int) int { return i % 7 }, 3, 20)
	enc, err := Encode(snap)
	if err != nil {
		t.Fatal(err)
	}
	if enc.Workloads != MaxWorkloads || enc.Dropped != 1000 {
		t.Fatalf("kept %d dropped %d, want 5000 and 1000", enc.Workloads, enc.Dropped)
	}
	got, _ := decodeBody(t, enc.Body)
	if len(got.Workloads) != MaxWorkloads {
		t.Fatalf("body holds %d workloads", len(got.Workloads))
	}
	if len(enc.Body) > MaxBodyBytes {
		t.Errorf("body is %d bytes, over the limit", len(enc.Body))
	}

	// The biggest workloads (most pods) are the ones kept.
	kept := map[string]bool{}
	minKept := 1 << 30
	for _, w := range got.Workloads {
		kept[w.Namespace+"/"+w.Name] = true
		minKept = min(minKept, w.Pods.Desired)
	}
	maxDropped := -1
	for _, w := range snap.Workloads {
		if !kept[w.Namespace+"/"+w.Name] {
			maxDropped = max(maxDropped, w.Pods.Desired)
		}
	}
	if maxDropped > minKept {
		t.Errorf("dropped a workload with %d pods while keeping one with %d", maxDropped, minKept)
	}
	if !slices.IsSortedFunc(got.Workloads, compareWorkloads) {
		t.Error("kept workloads must be sorted by namespace, kind, name")
	}
}

func TestEncodeStaysUnderTwoMiBOfGzip(t *testing.T) {
	// Random names do not compress well: 200 workloads of 100 names of 150
	// characters are about 3 MB, over the limit even after gzip.
	snap := syntheticSnapshot(200, func(i int) int { return 1 + i%5 }, 100, 150)
	enc, err := Encode(snap)
	if err != nil {
		t.Fatal(err)
	}
	if len(enc.Body) > MaxBodyBytes {
		t.Fatalf("body is %d bytes, over %d", len(enc.Body), MaxBodyBytes)
	}
	if enc.Dropped == 0 || enc.Workloads+enc.Dropped != 200 {
		t.Fatalf("kept %d dropped %d; this input cannot fit", enc.Workloads, enc.Dropped)
	}
	// The cut is as late as it can be: one more workload would not fit.
	if len(enc.Body) < MaxBodyBytes-128<<10 {
		t.Errorf("body is only %d bytes; too many workloads were dropped", len(enc.Body))
	}
	got, _ := decodeBody(t, enc.Body)
	if len(got.Workloads) != enc.Workloads {
		t.Errorf("body holds %d workloads, Encoded says %d", len(got.Workloads), enc.Workloads)
	}
	minKept := 1 << 30
	for _, w := range got.Workloads {
		minKept = min(minKept, w.Pods.Desired)
	}
	keptNames := map[string]bool{}
	for _, w := range got.Workloads {
		keptNames[w.Name] = true
	}
	for _, w := range snap.Workloads {
		if !keptNames[w.Name] && w.Pods.Desired > minKept {
			t.Errorf("dropped %s with %d pods while keeping a workload with %d", w.Name, w.Pods.Desired, minKept)
		}
	}
}

func TestEncodeLeavesSmallSnapshotsAlone(t *testing.T) {
	snap := syntheticSnapshot(25, func(i int) int { return i % 3 }, 4, 20)
	before := mustJSON(t, snap)
	enc, err := Encode(snap)
	if err != nil {
		t.Fatal(err)
	}
	if enc.Workloads != 25 || enc.Dropped != 0 {
		t.Errorf("kept %d dropped %d", enc.Workloads, enc.Dropped)
	}
	got, raw := decodeBody(t, enc.Body)
	if mustJSON(t, got) != before {
		t.Error("an encoded small snapshot differs from the input")
	}
	if enc.RawBytes != raw {
		t.Errorf("RawBytes = %d, want %d", enc.RawBytes, raw)
	}
	if mustJSON(t, snap) != before || !reflect.DeepEqual(snap.Workloads[0], got.Workloads[0]) {
		t.Error("Encode must not change its input")
	}
}

func TestEncodeDoesNotReorderOrShrinkTheInput(t *testing.T) {
	snap := syntheticSnapshot(6000, func(i int) int { return i % 7 }, 1, 20)
	first := snap.Workloads[0].Name
	if _, err := Encode(snap); err != nil {
		t.Fatal(err)
	}
	if len(snap.Workloads) != 6000 || snap.Workloads[0].Name != first || !slices.IsSortedFunc(snap.Workloads, compareWorkloads) {
		t.Error("Encode changed the snapshot it was given")
	}
}

func TestEncodeEmptySnapshot(t *testing.T) {
	enc, err := Encode(syntheticSnapshot(0, func(int) int { return 0 }, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	got, _ := decodeBody(t, enc.Body)
	if enc.Workloads != 0 || enc.Dropped != 0 || len(got.Workloads) != 0 {
		t.Errorf("enc = %+v", enc)
	}
}
