# SPDX-License-Identifier: Apache-2.0

"""impact.ai on AWS Lambda: the layer's start, at /opt/impact-ai/lambda/sitecustomize.py.

The layer's wrapper (/opt/impact-ai-instrument) puts this directory at the front of PYTHONPATH, so Python imports this
module when the runtime's interpreter starts, before the runtime has loaded anything of the function. It

1. takes this directory out of PYTHONPATH again, first, so that Lambda's runtime sets up the path exactly as it would
   without the layer (it leaves out the layers' /opt/python when PYTHONPATH is set);
2. starts the package from /opt/impact-ai/python (impact_instrumentation.aws_lambda), which wraps the handler when the
   runtime imports it;
3. runs the next sitecustomize on the path, if there is one, as if this module were not there.

Nothing here may stop the function: every failure is caught, and Python goes on even past one that is not.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _restore():
    """Take this directory out of PYTHONPATH, where the wrapper put it first; True when the function sets one itself."""
    value = os.environ.get("PYTHONPATH")
    if value is None:
        return False
    first, sep, rest = value.partition(os.pathsep)
    if first and os.path.abspath(first) == HERE:
        if sep:
            os.environ["PYTHONPATH"] = rest
            return True
        del os.environ["PYTHONPATH"]
        return False
    return True


def _forget_here():
    sys.path[:] = [p for p in sys.path if not p or os.path.abspath(p) != HERE]


def _start(pythonpath_set):
    root = os.path.join(os.path.dirname(HERE), "python")
    if not os.path.isdir(os.path.join(root, "impact_instrumentation")):
        return
    added = root not in sys.path
    if added:
        sys.path.append(root)
    try:
        from impact_instrumentation import aws_lambda

        aws_lambda.start(root=root, pythonpath_set=pythonpath_set)
    except Exception:  # noqa: BLE001 - the package did not load: leave the path as it was
        if added and root in sys.path:
            sys.path.remove(root)


def _chain():
    import importlib.machinery
    import importlib.util

    paths = [p for p in sys.path if p and os.path.abspath(p) != HERE]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


try:
    _pythonpath_set = _restore()
    _forget_here()
except Exception:  # noqa: BLE001, S110
    _pythonpath_set = "PYTHONPATH" in os.environ
try:
    _start(_pythonpath_set)
except Exception:  # noqa: BLE001, S110 - the function starts whatever happens here
    pass
_chain()
