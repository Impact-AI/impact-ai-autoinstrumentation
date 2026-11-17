// SPDX-License-Identifier: Apache-2.0
'use strict';

// The coexistence list: the tools the package is proven to run alongside, and what the setup report says of the tool.
//
// The list ships with the package as coexistence.json beside this module, in the form of the image's qualification: one
// cell per tool, mode, Node major and version series of the tool (its major version, or for a 0.x version its minor),
// with the version the proof ran and what it found. An `alongside` cell passes when the package started beside the tool
// (`started`), traced the workload (`traced`), and the tool's own backend recorded the same as without the package once
// generated ids and times are masked (`same`); `proof` names the test that showed it.
//
// A workload whose tracer the package found (detect.findForeign) is run alongside only when every finding shows one tool
// and a passing alongside cell of that tool matches the workload's Node major and the tool's version series exactly. Any
// other workload keeps the stand-down: a tool joins the list, and a version series joins a tool, only with a passing
// proof. The list is read only when a tool was found; one that cannot be read proves nothing.

const fs = require('fs');
const path = require('path');
const detect = require('./detect');

const SCHEMA = 'impact.coexistence.v1';
const FILE = path.join(__dirname, 'coexistence.json');
// The coexistence modes by the names impact.ai's service uses; only alongside changes what the package does at start.
const MODES = ['alongside', 'copy', 'relay', 'capture', 'import'];

/** The shipped list; throws for a file that is not one or cannot be read. */
function load(file) {
  const table = JSON.parse(fs.readFileSync(file || FILE, 'utf8'));
  if (!table || typeof table !== 'object' || table.schema !== SCHEMA || !Array.isArray(table.cells)) {
    throw new Error('not a ' + SCHEMA);
  }
  return table;
}

/** What a cell of this version proves: its major version ('3' of 3.42.1), or for 0.x its minor ('0.22'); else null. */
function series(version) {
  const parts = String(version || '').trim().split('.');
  const major = /^\d+/.exec(parts[0]);
  if (!major) return null;
  const minor = parts.length > 1 ? /^\d+/.exec(parts[1]) : null;
  return Number(major[0]) === 0 && minor ? '0.' + Number(minor[0]) : String(Number(major[0]));
}

/** An alongside cell that proves its tool: started, traced, and the tool's backend recorded the same. */
function passes(cell) {
  return cell.mode === 'alongside' && cell.started === true && cell.traced === true && cell.same === true;
}

/** Whether a passing alongside cell proves `tool` at `version` on Node `nodeMajor`. */
function proven(table, tool, version, nodeMajor) {
  const wanted = series(version);
  if (wanted === null) return false;
  return table.cells.some(
    (cell) =>
      cell &&
      typeof cell === 'object' &&
      cell.tool === tool &&
      cell.node === nodeMajor &&
      String(cell.major) === wanted &&
      passes(cell)
  );
}

/** Whether the package starts beside the tracer it found instead of standing down. Never throws. */
function alongside(findings, tool, nodeMajor, table) {
  try {
    if (!findings || !findings.length || !tool || !tool.version) return false;
    if (findings.some((finding) => detect.findingTool(finding) !== tool.id)) return false;
    return proven(table || load(), tool.id, tool.version, nodeMajor);
  } catch (error) {
    return false; // a list that cannot be read proves nothing
  }
}

/**
 * The setup report's attributes for the tool beside the package: its id, version and exporter kind when known, and
 * whether the package started alongside it. Nothing when no tool was named.
 */
function attributes(tool, started) {
  if (!tool) return {};
  const out = { 'impact.setup.tool': tool.id };
  if (tool.version) out['impact.setup.tool.version'] = tool.version;
  if (tool.exporter) out['impact.setup.exporter'] = tool.exporter;
  out['impact.setup.alongside'] = started === true;
  return out;
}

module.exports = { SCHEMA, FILE, MODES, load, series, passes, proven, alongside, attributes };
