// SPDX-License-Identifier: Apache-2.0

// Package source holds the decoders of the sources the relay relays, one file
// per source, named by its id: datadog.go is the source "datadog". A source's
// helpers may go into files named <id>_<what>.go, its tests next to them and its
// recorded requests into testdata/<id>/.
package source

import "github.com/impact-ai/impact-ai-autoinstrumentation/reporter/internal/relay"

// All is every relay source's decoder, in the order of their ids. A new source
// adds its file and one line here.
func All() []relay.Decoder {
	return []relay.Decoder{
		datadog{},
		newrelic{},
		zipkin{},
	}
}
