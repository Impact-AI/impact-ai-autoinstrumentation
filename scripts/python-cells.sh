#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# The cells of the Python image's qualification (python/qualification.json), in containers, against the mock providers.
#
#   scripts/python-cells.sh changed   the cells of the libraries the change touches (scripts/qualify_python.py select):
#                                     what `make test-python-cells` and the end-to-end job run; none when it touches none
#   scripts/python-cells.sh all       every cell: what `make release` runs before it pushes anything
#   scripts/python-cells.sh qualify   every cell, each writing its answer to build/qualification-python, then
#                                     python/qualification.json from them (the file's excluded releases are kept)
#   scripts/python-cells.sh newest    every cell on fixtures whose libraries are upgraded to their newest releases (the
#                                     weekly run): fails when a release fails its cell, and writes
#                                     build/qualification-python-newest/SUMMARY.md, the body of the issue it opens
#
# A cell that no longer says what the file says fails `changed` and `all`. CELLS=all|none|<library>[@<python>],...
# replaces the selection of `changed`; CELLS_BUILD=0 says the images are there (the end-to-end job builds them with its
# own).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

mode="${1:-changed}"
case "$mode" in
  changed) libraries="$(python3 scripts/qualify_python.py select)" ;;
  all | qualify | newest) libraries="$(python3 scripts/qualify_python.py libraries)" ;;
  *) echo "usage: scripts/python-cells.sh changed|all|qualify|newest" >&2; exit 2 ;;
esac
if [ -z "$libraries" ]; then
  echo "the change touches no library of the Python qualification: no cell to run"
  exit 0
fi
echo "Python qualification cells of: $libraries"
export CELL_LIBRARIES="$libraries"

if [ "${CELLS_BUILD:-1}" != 0 ]; then
  if [ "$mode" = newest ]; then build_images newest; else build_images cells; fi
fi

pytest=(uv run --project python --frozen pytest python/tests/image/test_python_cells.py -m cells -n 3 -v -p no:cacheprovider --tb=short)
if [ "$mode" = newest ]; then
  out=build/qualification-python-newest
  rm -rf "$out" && mkdir -p "$out"
  CELLS_NEWEST=1 QUALIFY_DIR="$PWD/$out" "${pytest[@]}" || true  # the report below decides, cell by cell
  status=0
  python3 scripts/qualify_python.py report "$out" >"$out/SUMMARY.md" || status=$?
  cat "$out/SUMMARY.md"
  exit "$status"
elif [ "$mode" = qualify ]; then
  rm -rf build/qualification-python && mkdir -p build/qualification-python
  QUALIFY_DIR="$PWD/build/qualification-python" "${pytest[@]}"
  python3 scripts/qualify_python.py assemble build/qualification-python python/qualification.json
else
  "${pytest[@]}"
fi
