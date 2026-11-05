# SPDX-License-Identifier: Apache-2.0

"""The modules that run before the application starts must import nothing but the standard library.

If one of them imported a third-party package it could shadow, or be shadowed by, the application's own copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

CODE = """
import json, sys
sys.path.insert(0, {src!r})
before = set(sys.modules)
import importlib
importlib.import_module({module!r})
new = {{m.split('.')[0] for m in set(sys.modules) - before}}
print(json.dumps(sorted(new - set(sys.stdlib_module_names) - {{'impact_instrumentation'}})))
"""


@pytest.mark.parametrize(
    "module",
    [
        "impact_instrumentation.detect",
        "impact_instrumentation.defaults",
        "impact_instrumentation.frameworks",
        "impact_instrumentation.otlp",
        "impact_instrumentation.setup_report",
        "impact_instrumentation.bootstrap",
    ],
)
def test_module_imports_only_the_standard_library(module):
    result = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-I", "-c", CODE.format(src=str(SRC), module=module)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
