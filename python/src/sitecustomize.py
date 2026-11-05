# SPDX-License-Identifier: Apache-2.0

"""impact.ai: replaces the sitecustomize of the OpenTelemetry Operator's Python auto-instrumentation layout.

The Operator puts this directory first on PYTHONPATH, so Python imports this module at interpreter start, before any
application code. It decides whether to start or to stand down (see impact_instrumentation.bootstrap), and then runs
the next sitecustomize on the path, if there is one, as if this module were not there.
"""

import os
import sys


def _start():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if root not in sys.path:
        sys.path.append(root)
    try:
        from impact_instrumentation.bootstrap import start

        start()
    except Exception:  # noqa: BLE001, S110 - nothing here may stop the application from starting
        pass


def _chain():
    import importlib.machinery
    import importlib.util

    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    paths = [p for p in sys.path if p and os.path.realpath(p) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


_start()
_chain()
