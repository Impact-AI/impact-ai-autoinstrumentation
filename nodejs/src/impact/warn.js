// SPDX-License-Identifier: Apache-2.0
'use strict';

// How the package speaks: one line on file descriptor 2, written with fs.write and not through process.stderr.
//
// A stream whose reader is gone (a pipe the other end has closed: EPIPE) or whose descriptor the application closed (EBADF)
// says so with an 'error' event, and an 'error' event that nobody listens to ends the process. A warning that the kit could
// not export is not worth that, and a listener on the application's own stderr would change what the application sees. So
// the line is written under the stream, and whatever the write answers is dropped.

const fs = require('fs');

function writeWarning(message) {
  try {
    fs.write(2, Buffer.from('impact-instrumentation: ' + message + '\n', 'utf8'), () => {});
  } catch (error) {
    // a descriptor that is not there is not a reason to stop
  }
}

module.exports = { writeWarning };
