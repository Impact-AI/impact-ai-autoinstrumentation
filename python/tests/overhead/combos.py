# SPDX-License-Identifier: Apache-2.0

"""The combinations of runtime and server the overhead is measured for: every one the images claim.

Python 3.11 to 3.14, each under uvicorn and gunicorn (8), and Node 22, 24 and 26 as CommonJS under Express (3): 11. The
canary of `make ci` is two of them, one round each; `make release` and `make overhead` run all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
NODES = ["22", "24", "26"]

UVICORN = ["uvicorn", "app_asgi:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
GUNICORN = [
    "gunicorn", "app_wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "2",  # noqa: S104
    "--worker-tmp-dir", "/dev/shm", "--no-control-socket",  # noqa: S108
]  # fmt: skip


# A Node.js workload is started the same way with and without injection: with the garbage collector exposed and a
# preload (in every fixture image, testing/apps/nodejs/src/settle.cjs) that collects all garbage on SIGUSR2. See
# harness.settle().
NODE_EXPRESS = ("node", "--expose-gc", "--require", "/src/settle.cjs", "express.cjs")
NODE_SETTLE = "USR2"


@dataclass(frozen=True)
class Combo:
    id: str
    runtime: str  # python | node
    version: str  # 3.12 | 22
    server: str  # uvicorn | gunicorn | express
    command: tuple[str, ...]
    directory: str | None  # the working directory of the workload in its image, None for the image's own
    settle: str | None = None  # the signal that makes the workload collect its garbage before its memory is read


def all_combos() -> list[Combo]:
    combos = []
    for python in PYTHONS:
        for server, command in (("uvicorn", UVICORN), ("gunicorn", GUNICORN)):
            combos.append(Combo(f"python{python}-{server}", "python", python, server, tuple(command), None))
    for node in NODES:
        combos.append(
            Combo(f"node{node}-express", "node", node, "express", NODE_EXPRESS, "/apps/express", settle=NODE_SETTLE)
        )
    return combos


# What `make ci` measures on every change: one of each runtime, one round each.
CANARY = ["python3.12-uvicorn", "node22-express"]
