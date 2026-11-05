// SPDX-License-Identifier: Apache-2.0
'use strict';

// Test helpers shared by the unit tests: scratch directories with fake node_modules, and a local OTLP/HTTP receiver.

const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');
const { Worker } = require('worker_threads');
const zlib = require('zlib');

const created = [];

function tmpdir(prefix) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix || 'impact-'));
  created.push(directory);
  return directory;
}

process.on('exit', () => {
  for (const directory of created) fs.rmSync(directory, { recursive: true, force: true });
});

/** node_modules/<name>/package.json with a version, as an installed package has it. */
function fakePackage(root, name, version, extra) {
  const directory = path.join(root, 'node_modules', ...name.split('/'));
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify(Object.assign({ name, version }, extra || {})));
  return directory;
}

class Sink {
  /** statuses: the status of each request in turn (200 afterwards); delay: milliseconds before answering. */
  constructor(statuses, delay) {
    this.requests = [];
    this.statuses = (statuses || []).slice();
    this.delay = delay || 0;
    this.server = http.createServer((request, response) => {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', () => {
        this.requests.push({ path: request.url, headers: request.headers, body: Buffer.concat(chunks) });
        const status = this.statuses.length ? this.statuses.shift() : 200;
        const answer = () => {
          response.statusCode = typeof status === 'number' ? status : status.status;
          if (status && status.retryAfter !== undefined) response.setHeader('retry-after', String(status.retryAfter));
          response.end('{}');
        };
        if (this.delay) setTimeout(answer, this.delay);
        else answer();
      });
    });
  }

  listen() {
    return new Promise((resolve) => {
      this.server.listen(0, '127.0.0.1', () => {
        this.url = 'http://127.0.0.1:' + this.server.address().port;
        resolve(this);
      });
    });
  }

  close() {
    this.server.closeAllConnections();
    return new Promise((resolve) => this.server.close(resolve));
  }

  payloads() {
    return this.requests.map((request) => {
      const raw = request.headers['content-encoding'] === 'gzip' ? zlib.gunzipSync(request.body) : request.body;
      return JSON.parse(raw.toString('utf8'));
    });
  }

  spans() {
    const out = [];
    for (const payload of this.payloads()) {
      for (const resourceSpans of payload.resourceSpans) {
        for (const scopeSpans of resourceSpans.scopeSpans) out.push(...scopeSpans.spans);
      }
    }
    return out;
  }
}

function attributesOf(items) {
  const out = {};
  for (const item of items || []) {
    const [kind, value] = Object.entries(item.value)[0];
    out[item.key] = kind === 'arrayValue' ? (value.values || []).map((v) => Object.values(v)[0]) : value;
  }
  return out;
}

async function until(predicate, timeout, interval) {
  const deadline = Date.now() + (timeout || 3000);
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error('timed out waiting for a condition');
    await new Promise((resolve) => setTimeout(resolve, interval || 20));
  }
}

/** A listener that accepts connections and never answers: what a wedged gateway looks like to its clients. */
function hangingServer() {
  const sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    socket.resume(); // it reads what is sent and never answers; reading is how it sees the client hang up
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({
        url: 'http://127.0.0.1:' + server.address().port,
        connections: () => sockets.size,
        close() {
          for (const socket of sockets) socket.destroy();
          return new Promise((done) => server.close(done));
        },
      });
    });
  });
}

// A black hole on the loopback, for the packets a default-deny NetworkPolicy drops. The listener lives in a worker thread
// whose event loop is blocked, so nothing accepts: the kernel completes the handshake of the first few connections
// (the backlog is 1) and then drops the SYNs of the others, and a connect to it neither succeeds nor fails, as with
// an address nothing routes to. It needs Linux's behaviour for a full accept queue.
const BLACK_HOLE = `
const { parentPort } = require('worker_threads');
const net = require('net');
net.createServer().listen({ host: '127.0.0.1', port: 0, backlog: 1 }, function () {
  parentPort.postMessage(this.address().port);
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
});
`;

async function blackHole() {
  const worker = new Worker(BLACK_HOLE, { eval: true });
  const port = await new Promise((resolve, reject) => {
    worker.once('message', resolve);
    worker.once('error', reject);
  });
  const fillers = [];
  for (let i = 0; i < 6; i += 1) {
    const socket = net.connect({ host: '127.0.0.1', port });
    socket.on('error', () => {});
    fillers.push(socket);
  }
  await new Promise((resolve) => setTimeout(resolve, 200));
  return {
    url: 'http://127.0.0.1:' + port,
    async close() {
      for (const socket of fillers) socket.destroy();
      await worker.terminate();
    },
  };
}

/**
 * Run a script as a child process. Resolves when it has ended, or after `cap` milliseconds if it has not (then it is
 * killed and `capped` says so). `afterWork` is the time between the line "WORK_DONE" and the end of the process: what a
 * rollout, a Job or a CronJob waits for.
 */
function runNode(args, options) {
  const settings = options || {};
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [...(settings.execArgv || []), ...args], {
      env: Object.assign({}, process.env, settings.env || {}),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (settings.setup) settings.setup(child);
    let stdout = '';
    let stderr = '';
    let workDoneAt = null;
    let capped = false;
    const cap = setTimeout(() => {
      capped = true;
      child.kill('SIGKILL');
    }, settings.cap || 60000);
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
      if (workDoneAt === null && stdout.indexOf('WORK_DONE') >= 0) workDoneAt = Date.now();
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.on('close', (code, signal) => {
      clearTimeout(cap);
      const endedAt = Date.now();
      resolve({
        code,
        signal,
        stdout,
        stderr,
        capped,
        afterWork: workDoneAt === null ? null : (endedAt - workDoneAt) / 1000,
      });
    });
  });
}

module.exports = { tmpdir, fakePackage, Sink, attributesOf, until, hangingServer, blackHole, runNode };
