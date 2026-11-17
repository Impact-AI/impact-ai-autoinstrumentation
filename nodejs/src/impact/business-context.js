// SPDX-License-Identifier: Apache-2.0
'use strict';

// Copies the business context the application put in OpenTelemetry baggage onto every span the package makes.
//
// Who made a request, in which session and for which use case is known to the application, once it has authenticated the
// request, and to nothing outside it. The application says so with one line of OpenTelemetry's API (baggage on the context
// of the request, `propagation.setBaggage`) and every span the package's provider makes in that context, the model calls
// among them, carries it:
//
//   * user.id, session.id, gen_ai.conversation.id and impact.use_case, the names of the OpenTelemetry semantic conventions
//     (impact.ai's own for the use case) that impact.ai reads. No other key of the baggage is read, ever;
//   * text only, at most MAX_VALUE_BYTES bytes of UTF-8, cut on a character boundary. An empty value and one that is not
//     text are dropped;
//   * an attribute the span already has when it starts (the instrumentation's own) is kept.
//
// The baggage is read from the context the span starts in, so it works whether the provider is the kit's own or registered
// globally (Anthropic).
//
// Baggage also arrives from callers, in the `baggage` header of a request, and a caller can put any user in it. By default
// the package registers W3C trace context only (propagator()), so nothing of a caller's baggage is read and only values the
// application sets in the process reach the spans. A workload behind a gateway that sets them, and that trusts it, says so
// with IMPACT_TRUST_CALLER_BAGGAGE=true (the chart sets it from the pod's annotation impact.ai/trust-caller-baggage): the
// package then registers W3C baggage beside trace context, OpenTelemetry's default, and a caller's values reach the spans
// too, as they do in Python with the same setting. A value the application sets in the process always wins.
//
// The OpenTelemetry API is handed in (the tree's), so this file loads on any Node and needs no package.

const KEYS = ['user.id', 'session.id', 'gen_ai.conversation.id', 'impact.use_case'];
const MAX_VALUE_BYTES = 256;
// "true" (any case) reads a caller's baggage. Anything else, an empty value among them (what Kubernetes gives when the pod
// has no such annotation), reads none of it.
const TRUST = 'IMPACT_TRUST_CALLER_BAGGAGE';

function trustsCallers(env) {
  return String((env && env[TRUST]) || '').trim().toLowerCase() === 'true';
}

/**
 * The propagator the package registers, from the tree's @opentelemetry/core: W3C trace context, and W3C baggage beside it
 * where the workload trusts its callers. Trace context alone if the pair cannot be built.
 */
function propagator(core, env) {
  const traceContext = new core.W3CTraceContextPropagator();
  if (!trustsCallers(env)) return traceContext;
  try {
    return new core.CompositePropagator({ propagators: [traceContext, new core.W3CBaggagePropagator()] });
  } catch (error) {
    return traceContext;
  }
}

/** The value as the span carries it: text of at most MAX_VALUE_BYTES bytes, or undefined to leave it off. */
function bounded(value) {
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  if (value.length * 3 <= MAX_VALUE_BYTES) return value; // a UTF-16 unit is at most 3 bytes of UTF-8
  // A unit is at least one byte, so the cut lies within the first MAX_VALUE_BYTES of them: a large value is never encoded
  // whole.
  const encoded = Buffer.from(value.slice(0, MAX_VALUE_BYTES), 'utf8');
  if (value.length <= MAX_VALUE_BYTES && encoded.length <= MAX_VALUE_BYTES) return value;
  let end = MAX_VALUE_BYTES;
  while (end > 0 && (encoded[end] & 0xc0) === 0x80) end -= 1; // the byte at the cut continues a character: drop it whole
  return encoded.subarray(0, end).toString('utf8');
}

class BusinessContext {
  constructor(api) {
    this.api = api;
  }

  onStart(span, parentContext) {
    try {
      const baggage = this.api.propagation.getBaggage(parentContext);
      if (!baggage) return;
      const present = span.attributes || {};
      for (const key of KEYS) {
        if (Object.prototype.hasOwnProperty.call(present, key)) continue;
        try {
          const entry = baggage.getEntry(key);
          const value = entry && bounded(entry.value);
          if (value !== undefined) span.setAttribute(key, value);
        } catch (error) {
          // one value that cannot be read or set costs only itself
        }
      }
    } catch (error) {
      // called by the application's span start: nothing may reach it
    }
  }

  onEnd() {}

  forceFlush() {
    return Promise.resolve();
  }

  shutdown() {
    return Promise.resolve();
  }
}

module.exports = { BusinessContext, bounded, propagator, trustsCallers, KEYS, MAX_VALUE_BYTES, TRUST };
