// SPDX-License-Identifier: Apache-2.0
'use strict';

// Preload of the overhead measurement (python/tests/overhead), on both sides of it and nowhere else. On SIGUSR2 the process
// collects all its garbage the way V8 does when the machine is short of memory (`last-resort`: a full collection that also
// gives the young generation and the empty pages back), so that the memory of the two processes is read when each holds
// what it needs and nothing it merely has not yet let go of. Start the workload with `node --expose-gc --require
// /src/settle.cjs ...`.

process.on('SIGUSR2', () => {
  try {
    global.gc({ type: 'major', execution: 'sync', flavor: 'last-resort' });
  } catch (error) {
    process.stderr.write('settle: the collection failed: ' + error.message + '\n');
  }
});
