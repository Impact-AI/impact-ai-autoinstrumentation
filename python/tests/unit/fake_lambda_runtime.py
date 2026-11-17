# SPDX-License-Identifier: Apache-2.0

"""A stand-in for Lambda's Python runtime, for the tests of the layer's wrapper (test_lambda_wrapper.py).

It does what /var/runtime/bootstrap.py and awslambdaric do before and around the handler: puts the function's code first
on the path (and the layers' /opt/python when PYTHONPATH is not set), sets PYTHONPATH to the runtime's directory when it
is not set, imports the handler that _HANDLER names, and invokes it. Then it prints what it saw as JSON and ends the
process at once, with no exit handlers: Lambda freezes or ends an environment without telling the function's threads,
so whatever reached the ingest was sent within the invocations.

    fake_lambda_runtime.py <invocations>
"""

import importlib
import json
import os
import sys
import time

INVOKED_ARN = os.environ.get("FAKE_INVOKED_ARN", "arn:aws:lambda:eu-west-1:123456789012:function:refund-bot:live")


class Context:
    invoked_function_arn = INVOKED_ARN
    aws_request_id = "request"

    def __init__(self):
        self.deadline = time.monotonic() + 3.0

    def get_remaining_time_in_millis(self):
        return int((self.deadline - time.monotonic()) * 1000)


def main():
    pythonpath = os.environ.get("PYTHONPATH")
    seen = {
        "pythonpath": pythonpath,
        "sitecustomize": getattr(sys.modules.get("sitecustomize"), "__file__", None),
        "path_before": list(sys.path),
    }
    if pythonpath is None:
        sys.path.insert(0, "/opt/python")
    sys.path.insert(0, os.environ["LAMBDA_TASK_ROOT"])
    if pythonpath is None:
        os.environ["PYTHONPATH"] = "/var/runtime"
    module, _, name = os.environ["_HANDLER"].rpartition(".")
    try:
        handler = getattr(importlib.import_module(module.replace("/", ".")), name)
    except Exception as exc:  # noqa: BLE001 - reported like the runtime does
        seen["init_error"] = [type(exc).__name__, str(exc)]
        print(json.dumps(seen), flush=True)
        os._exit(0)
    seen["wrapped"] = bool(getattr(handler, "__impact_lambda__", False))
    seen["invocations"] = []
    for number in range(int(sys.argv[1])):
        begin = time.monotonic()
        try:
            outcome = {"result": handler({"order": f"A-{number}"}, Context())}
        except Exception as exc:  # noqa: BLE001
            outcome = {"error": [type(exc).__name__, str(exc)]}
        outcome["seconds"] = time.monotonic() - begin
        seen["invocations"].append(outcome)
    seen["loaded"] = sorted(m for m in sys.modules if m.startswith(("opentelemetry", "impact_instrumentation")))[:3]
    print(json.dumps(seen), flush=True)
    os._exit(0)


main()
