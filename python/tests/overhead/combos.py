# SPDX-License-Identifier: Apache-2.0

"""The combinations of runtime and server the overhead is measured for: every one the images claim.

Python 3.11 to 3.14, each under uvicorn and gunicorn (8), and Node 22, 24 and 26 as CommonJS under Express (3), each
calling OpenAI; and, for each library that joined the traced set with cells of its own, a workload that calls it and
only it, in the fixture image of its cells: Google's SDK, google-genai, on Python 3.12 under uvicorn (1). 12. The canary
of `make ci` is two of them, one round each; `make release` and `make overhead` run all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
NODES = ["22", "24", "26"]


def uvicorn(module: str) -> list[str]:
    return ["uvicorn", f"{module}:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104


UVICORN = uvicorn("app_asgi")
GUNICORN = [
    "gunicorn", "app_wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "2",  # noqa: S104
    "--worker-tmp-dir", "/dev/shm", "--no-control-socket",  # noqa: S108
]  # fmt: skip


# A Node.js workload is started the same way with and without injection: with the garbage collector exposed and a
# preload (in every fixture image, testing/apps/nodejs/src/settle.cjs) that collects all garbage on SIGUSR2. See
# harness.settle().
NODE_EXPRESS = ("node", "--expose-gc", "--require", "/src/settle.cjs", "express.cjs")
NODE_SETTLE = "USR2"

# The calls a workload makes, in turn, one per request.
OPENAI = ("/chat/openai",)
# google-genai's workload: the Gemini API and Vertex AI, sync, async and streaming (testing/apps/app_google_genai.py).
GOOGLE_GENAI = (
    "/chat/google-genai/gemini/sync",
    "/chat/google-genai/gemini/stream",
    "/chat/google-genai/vertex/async",
    "/chat/google-genai/vertex/async-stream",
)


@dataclass(frozen=True)
class Combo:
    id: str
    runtime: str  # python | node
    version: str  # 3.12 | 22
    server: str  # uvicorn | gunicorn | express
    command: tuple[str, ...]
    directory: str | None  # the working directory of the workload in its image, None for the image's own
    settle: str | None = None  # the signal that makes the workload collect its garbage before its memory is read
    routes: tuple[str, ...] = OPENAI  # the model calls it is asked for, in turn
    fixture: str = ""  # the fixture image, fixture-app[-<fixture>]:<version> (Python only)
    library: str | None = None  # the library it calls, where it is not the image's OpenAI


def all_combos() -> list[Combo]:
    combos = []
    for python in PYTHONS:
        for server, command in (("uvicorn", UVICORN), ("gunicorn", GUNICORN)):
            combos.append(Combo(f"python{python}-{server}", "python", python, server, tuple(command), None))
    for node in NODES:
        combos.append(
            Combo(f"node{node}-express", "node", node, "express", NODE_EXPRESS, "/apps/express", settle=NODE_SETTLE)
        )
    combos.append(
        Combo(
            "python3.12-uvicorn-google-genai",
            "python",
            "3.12",
            "uvicorn",
            tuple(uvicorn("app_google_genai")),
            None,
            routes=GOOGLE_GENAI,
            fixture="google-genai",
            library="google-genai",
        )
    )
    return combos


# What `make ci` measures on every change: one of each runtime, one round each.
CANARY = ["python3.12-uvicorn", "node22-express"]
