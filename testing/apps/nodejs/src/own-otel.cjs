// SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixture workload (CommonJS): runs an OpenTelemetry pipeline of its own, started in code, and then the OpenAI server of
// openai.cjs. The stand-down tests show that the package leaves its spans and its endpoint alone.

const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({ url: process.env.THEIRS_URL + '/v1/traces' }),
  instrumentations: [new HttpInstrumentation()],
});
sdk.start();

require('./openai.cjs');
