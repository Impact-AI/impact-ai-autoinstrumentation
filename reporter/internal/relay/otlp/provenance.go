// SPDX-License-Identifier: Apache-2.0

package otlp

import "slices"

// The resource attributes that say where a span came from.
const (
	// AttrSourceID is the id of the tool that traced the workload, such as "datadog".
	AttrSourceID = "impact.source.id"
	// AttrSourceRoute is how its spans reached impact.ai: one of the Route constants.
	AttrSourceRoute = "impact.source.route"
	// AttrSourceDecoder is the decoder that turned the tool's own format into
	// OTLP, as "<decoder>/<version>"; only decoded spans carry it.
	AttrSourceDecoder = "impact.source.decoder"
)

// The routes by which a source's spans reach impact.ai.
const (
	// RouteAlongside: impact.ai's own instrumentation runs next to the tool.
	RouteAlongside = "alongside"
	// RouteCopy: the tool exports OTLP, and the kit copies it.
	RouteCopy = "copy"
	// RouteRelay: the tool's own protocol is relayed unchanged, and a copy is decoded.
	RouteRelay = "relay"
	// RouteCapture: the spans are read from the network.
	RouteCapture = "capture"
	// RouteImport: the spans are read from what the tool's vendor stores.
	RouteImport = "import"
)

// Provenance is where a batch of spans came from.
type Provenance struct {
	Source  string // the source id
	Route   string // one of the Route constants
	Decoder string // "<decoder>/<version>"; empty for spans that were not decoded
}

// Apply sets the provenance attributes on every resource of t. Any value of
// these keys the payload already carried is replaced, and a payload's own
// decoder attribute is removed when p names none, so that a payload cannot
// claim another source.
func (p Provenance) Apply(t *Traces) {
	for i := range t.ResourceSpans {
		res := &t.ResourceSpans[i].Resource
		res.Attributes = slices.DeleteFunc(res.Attributes, func(kv KeyValue) bool {
			return kv.Key == AttrSourceID || kv.Key == AttrSourceRoute || kv.Key == AttrSourceDecoder
		})
		res.Attributes = append(res.Attributes, Str(AttrSourceID, p.Source), Str(AttrSourceRoute, p.Route))
		if p.Decoder != "" {
			res.Attributes = append(res.Attributes, Str(AttrSourceDecoder, p.Decoder))
		}
	}
}
