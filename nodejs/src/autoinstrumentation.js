// SPDX-License-Identifier: Apache-2.0
'use strict';

// impact.ai: the preload of the OpenTelemetry Operator's Node.js auto-instrumentation layout.
//
// The Operator copies this directory into the pod (`cp -r /autoinstrumentation/. /otel-auto-instrumentation-nodejs`) and
// starts every Node process of an annotated workload with
// `NODE_OPTIONS=--require /otel-auto-instrumentation-nodejs/autoinstrumentation.js`, before any application code. This
// file decides whether to start or to stand down (see impact/bootstrap.js), and nothing in it may throw: it is plain
// CommonJS that Node 16 can parse too, so that an unsupported Node reports why instead of failing to start.

try {
  require('./impact/bootstrap').start();
} catch (error) {
  // nothing here may stop the application from starting
}
