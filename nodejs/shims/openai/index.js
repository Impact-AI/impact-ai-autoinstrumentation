// SPDX-License-Identifier: Apache-2.0
'use strict';

// OpenInference's openai instrumentation does `require("openai")` while it loads, only to test a promise with
// `instanceof APIPromise`. The image bundles no provider SDK, so that name would not resolve. This stand-in is found by
// that one require (the payload's own module path) and hands out the APIPromise of the module the application loaded,
// which the instrumentation's patch is given (see impact/openai.js). The application never sees this file.

let applicationModule = null;

function NotAnAPIPromise() {}

exports.__setApplicationModule = function setApplicationModule(module) {
  applicationModule = module;
};

Object.defineProperty(exports, 'APIPromise', {
  enumerable: true,
  get() {
    return (applicationModule && applicationModule.APIPromise) || NotAnAPIPromise;
  },
});
