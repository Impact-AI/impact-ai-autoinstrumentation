// SPDX-License-Identifier: Apache-2.0
'use strict';

// Arm the instrumentation: load the OpenTelemetry packages of the tree this Node needs, build a tracer provider of our own
// that exports to the gateway, and register the instrumentations behind it.
//
// Nothing here runs unless bootstrap.js decided to start: a workload with a tracer of its own never loads this file.
//
// Two choices keep the application's own OpenTelemetry state out of harm's way:
//
//   * the instrumentations are handed our provider explicitly, so the spans of the libraries they patch never depend on
//     the global provider;
//   * the provider is registered globally only when Anthropic's SDK is installed. Its own spans (0.134.0 and later) are
//     made with the global provider and nothing else can reach them. The context manager is global in any case: it is
//     what lets a model call hang under the request that made it.

const path = require('path');
const { createRequire, register } = require('module');
const { pathToFileURL } = require('url');
const defaults = require('./defaults');
const plan = require('./plan');
const qualification = require('./qualification');
const otlp = require('./otlp');
const { ImpactSpanExporter } = require('./exporter');
const { BoundedBatchSpanProcessor, flushOnExit } = require('./processor');
const { VERSION } = require('./version');

/** The disabled-instrumentations check of the workload: `enabled('express')`. */
function enabledCheck(env) {
  const off = defaults.disabledInstrumentations(env);
  return (name) => off.indexOf('all') < 0 && off.indexOf(name) < 0;
}

/** OpenInference's instrumentation, told which openai module the application loaded (see shims/openai). */
function openAiInstrumentation(treeRequire, packageName, provider, traceConfig) {
  const Base = treeRequire(packageName).OpenAIInstrumentation;
  const shim = treeRequire('openai');
  class ImpactOpenAIInstrumentation extends Base {
    patch(moduleExports, moduleVersion) {
      try {
        shim.__setApplicationModule(moduleExports);
      } catch (error) {
        // the instrumentation then finds no APIPromise to compare with, and treats a response as a plain value
      }
      return super.patch(moduleExports, moduleVersion);
    }

    // The older line of the instrumentation (openai 4) builds its tracer once, from the global provider, and never again.
    setTracerProvider(tracerProvider) {
      super.setTracerProvider(tracerProvider);
      if (this.oiTracer) this.oiTracer.tracer = this.tracer;
    }
  }
  return new ImpactOpenAIInstrumentation({ tracerProvider: provider, traceConfig });
}

/** The code of a warning, from the arguments of process.emitWarning(). */
function warningCode(warning, type, code) {
  if (type && typeof type === 'object') return type.code; // (warning, options)
  if (typeof type === 'string') return typeof code === 'string' ? code : undefined; // (warning, type, code)
  return warning && warning.code; // an Error
}

/**
 * Register the loader hook of the instrumentation. Node 26 deprecates module.register() in favour of
 * module.registerHooks() (DEP0205), which the loader (import-in-the-middle 3.x) can use only from Node 22.22.3, 24.11.1,
 * 25.1 and 26.0 on, and the warning would land in the application's own output for a call that is this package's. It is the
 * one warning swallowed, for this one call; every other warning goes where it would have.
 */
function registerLoaderHook(registerHook, hook, options) {
  const emit = process.emitWarning;
  process.emitWarning = function emitWarning(warning, type, code) {
    if (warningCode(warning, type, code) === 'DEP0205') return undefined;
    return emit.apply(this, arguments);
  };
  try {
    registerHook(hook, options);
  } finally {
    process.emitWarning = emit;
  }
}

// The server span of the request is the span that matters; the layers of Express and Koa (every middleware, router and
// handler) would be three spans more per request with nothing to say about a model call. They are still walked, which is
// how the request span gets its route (/users/:id, not /users/42).
const IGNORED_LAYERS = ['middleware', 'router', 'request_handler'];

/**
 * Load the tree and register what the application needs.
 *
 * input: tree (trees.js), env, root (the payload directory), packages ({npm name: version} found in the application),
 * moduleSystem ('cjs' | 'esm'), nodeMajor, table (qualification.js), decisions (plan.js).
 * Returns {armed: [names], skipped: {name: why}, traced: Set of npm names, suppress(fn)}.
 */
function arm(input) {
  const { tree, env, root, packages, moduleSystem, nodeMajor, table, decisions } = input;
  const treeDir = path.join(root, tree.name);
  const treeRequire = createRequire(path.join(treeDir, 'package.json'));
  const enabled = enabledCheck(env);
  const packageNames = packages || {};
  const armed = [];
  const skipped = {};

  const api = treeRequire('@opentelemetry/api');
  const core = treeRequire('@opentelemetry/core');
  const { resourceFromAttributes } = treeRequire('@opentelemetry/resources');
  const { AsyncLocalStorageContextManager } = treeRequire('@opentelemetry/context-async-hooks');
  const { registerInstrumentations } = treeRequire('@opentelemetry/instrumentation');

  // Run fn without tracing what it does: the package's own requests, and its setup report, are no spans of the application's.
  // fn's own exceptions are fn's.
  const suppress = (fn) => {
    let context;
    try {
      context = core.suppressTracing(api.context.active());
    } catch (error) {
      return fn();
    }
    return api.context.with(context, fn);
  };

  const exporter = new ImpactSpanExporter({ env });
  const processor = new BoundedBatchSpanProcessor(exporter, { env, suppress });
  const provider = new (treeRequire(tree.provider.module)[tree.provider.name])({
    resource: resourceFromAttributes(otlp.resourceAttributes(env)),
    spanProcessors: [processor],
  });

  const contextManager = new AsyncLocalStorageContextManager();
  contextManager.enable();
  api.context.setGlobalContextManager(contextManager);
  api.propagation.setGlobalPropagator(new core.W3CTraceContextPropagator());

  // An ES module is only patched where the matrix proved it: the loader hook is registered for those, and only when it
  // cannot break a library of the application's (see qualification.hook).
  const esm = moduleSystem === 'esm';
  const loader = esm ? qualification.hook(table, nodeMajor, env, decisions, tree.name) : { register: false };
  if (loader.register && typeof register === 'function') {
    try {
      const hook = path.join(treeDir, 'node_modules', '@opentelemetry', 'instrumentation', 'hook.mjs');
      registerLoaderHook(register, pathToFileURL(hook), { parentURL: pathToFileURL(__filename) });
    } catch (error) {
      skipped.esm = 'the loader hook did not register: ' + error.message;
    }
  }

  const instrumentations = [];
  const add = (name, create) => {
    if (!enabled(name)) return false;
    instrumentations.push(create());
    armed.push(name);
    return true;
  };

  // The requests the workload serves (health probes excepted). Its calls out, to a model or to anything else, get no span
  // of their own here: the model call is the span of the library that makes it, and a client span for every call out
  // would double the spans of a request for no coverage.
  add('http', () => {
    const { HttpInstrumentation } = treeRequire('@opentelemetry/instrumentation-http');
    return new HttpInstrumentation({
      ignoreIncomingRequestHook: (request) => defaults.probe(request && request.url),
      ignoreOutgoingRequestHook: () => true,
    });
  });
  if ('express' in packageNames) {
    add('express', () => {
      const { ExpressInstrumentation } = treeRequire('@opentelemetry/instrumentation-express');
      return new ExpressInstrumentation({ ignoreLayersType: IGNORED_LAYERS });
    });
  }
  if ('koa' in packageNames) {
    add('koa', () => {
      const { KoaInstrumentation } = treeRequire('@opentelemetry/instrumentation-koa');
      return new KoaInstrumentation({ ignoreLayersType: IGNORED_LAYERS });
    });
  }

  // OpenAI: a patched module (so an ES module is traced only where the matrix says so, see plan.js).
  if (decisions.openai.action === 'trace') {
    const traceConfig = defaults.contentOff(env) ? { hideInputs: true, hideOutputs: true } : undefined;
    const name =
      qualification.majorOf(packageNames.openai) === 4
        ? '@arizeai/openinference-instrumentation-openai-v4'
        : '@arizeai/openinference-instrumentation-openai';
    instrumentations.push(openAiInstrumentation(treeRequire, name, provider, traceConfig));
    armed.push('openai');
  }

  // Anthropic: the SDK's own spans, made with the global provider. Nothing is patched, so the module system does not matter.
  const anthropic = decisions['@anthropic-ai/sdk'];
  if (anthropic.action === 'trace') {
    api.trace.setGlobalTracerProvider(provider);
    armed.push('anthropic');
  } else if (anthropic.disabled && env.ANTHROPIC_OPEN_TELEMETRY === undefined) {
    env.ANTHROPIC_OPEN_TELEMETRY = 'false';
  }

  registerInstrumentations({ instrumentations, tracerProvider: provider });

  // What is left in the queue when the event loop is empty is sent, within the limits of processor.flushAtExit().
  flushOnExit(processor);

  return {
    version: VERSION,
    armed: armed.sort(),
    skipped,
    traced: plan.tracedSet(decisions),
    suppress,
  };
}

module.exports = { arm, registerLoaderHook };
