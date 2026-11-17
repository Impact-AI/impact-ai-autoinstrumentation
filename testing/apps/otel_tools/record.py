#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Record what one AI tool's SDK sends its backend for the canonical interaction.

    python3 record.py traceloop --python .venv-traceloop/bin/python \\
        --package traceloop-sdk=0.63.0 --recording testing/fixtures/sources/traceloop/python.recording.json \\
        --otlp out/traceloop/python.otlp.json

    python3 record.py langfuse --node node_modules/.. --package @langfuse/otel=6.1.1 ...

It starts the kit's recording mock as the tool's backend (`theirs`) and provider.py as the model provider on free
local ports, runs canonical.py (or canonical.mjs) for the tool with THEIRS_URL and PROVIDER_URL, and keeps every
request the backend received: the recording (testing/mock/recording.py's format), and with --otlp the OTLP/JSON a
copy of it carries to impact.ai (otlp_json.py, which needs opentelemetry-proto in --python's environment). It fails
when the backend received no traces. Each tool's pinned environment is testing/apps/requirements-otel-<tool>.txt
(install it with UV_LINK_MODE=copy). Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[2]
sys.path.insert(0, str(KIT / "testing" / "mock"))
sys.path.insert(0, str(HERE))

from otlp_json import is_traces  # noqa: E402
from recording import recording  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(url: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1):  # noqa: S310 - a local test double
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", help="a tool canonical.py or canonical.mjs knows")
    runner = parser.add_mutually_exclusive_group(required=True)
    runner.add_argument("--python", help="the Python interpreter of the tool's pinned environment")
    runner.add_argument("--node", help="the directory whose node_modules holds the tool's pinned packages")
    parser.add_argument(
        "--launcher",
        help="a launcher that starts canonical.py, such as a distribution's opentelemetry-instrument (with --python)",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a variable for the app, {theirs} standing for the backend's URL (a launcher reads them at start)",
    )
    parser.add_argument("--converter", help="a Python with opentelemetry-proto for --otlp (default: --python)")
    parser.add_argument("--package", action="append", default=[], metavar="NAME=VERSION", required=True)
    parser.add_argument("--source", help="the source id the copy's provenance names (default: the tool)")
    parser.add_argument("--recording", required=True, help="where to write the recording")
    parser.add_argument("--otlp", help="where to write the OTLP/JSON a copy carries")
    parser.add_argument("--recorded", default=datetime.date.today().isoformat())
    parser.add_argument(
        "--tool-turns",
        type=int,
        default=1,
        help="the model asks for the tool this many times before answering (provider.py): more than 1 is a loop",
    )
    args = parser.parse_args(argv)
    packages = dict(package.split("=", 1) for package in args.package)

    mock_port, provider_port = free_port(), free_port()
    logs = tempfile.TemporaryDirectory()
    servers = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(KIT / "testing/mock/server.py"), "--host", "127.0.0.1", "--port", str(mock_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(HERE / "provider.py"),
                "--port",
                str(provider_port),
                "--tool-turns",
                str(args.tool_turns),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]
    try:
        wait_for(f"http://127.0.0.1:{mock_port}/healthz")
        wait_for(f"http://127.0.0.1:{provider_port}/healthz")
        env = {
            **os.environ,
            "THEIRS_URL": f"http://127.0.0.1:{mock_port}",
            "PROVIDER_URL": f"http://127.0.0.1:{provider_port}",
        }
        for setting in args.env:
            name, _, value = setting.partition("=")
            env[name] = value.replace("{theirs}", env["THEIRS_URL"])
        if args.python:
            launcher = [args.launcher] if args.launcher else []
            command, cwd = [*launcher, args.python, str(HERE / "canonical.py"), args.tool], None
        else:
            # Node.js resolves packages from the script's directory: run a copy beside the pinned node_modules.
            cwd = Path(args.node).resolve()
            shutil.copy(HERE / "canonical.mjs", cwd / "canonical.mjs")
            command = ["node", "canonical.mjs", args.tool]
        app = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=180, check=False)  # noqa: S603
        if app.returncode != 0:
            sys.stderr.write(app.stdout + app.stderr)
            raise SystemExit(f"the {args.tool} app failed ({app.returncode})")
        time.sleep(1.0)  # an exporter's last batch can still be in flight when the process exits
        with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/_requests", timeout=5) as response:  # noqa: S310
            captured = json.load(response)
    finally:
        for server in servers:
            server.terminate()
        logs.cleanup()

    how = (
        f"canonical.{'py' if args.python else 'mjs'} {args.tool} (testing/apps/otel_tools)"
        f"{' under ' + Path(args.launcher).name if args.launcher else ''} against the kit's recording mock as the "
        "tool's backend and provider.py as the model provider"
        f"{f', the model asking for the tool {args.tool_turns} times before it answers' if args.tool_turns > 1 else ''}"
    )
    made = recording(captured, how, packages, args.recorded)
    if not any(is_traces(request) for request in made["requests"]):
        raise SystemExit(f"the {args.tool} backend received no traces: {[r['path'] for r in made['requests']]}")
    Path(args.recording).parent.mkdir(parents=True, exist_ok=True)
    Path(args.recording).write_text(json.dumps(made, indent=2) + "\n", encoding="utf-8")
    if args.otlp:
        converter = args.converter or args.python
        if not converter:
            raise SystemExit("--otlp needs --converter (a Python with opentelemetry-proto) when recording Node.js")
        done = subprocess.run(  # noqa: S603
            [converter, str(HERE / "otlp_json.py"), args.recording, "--source", args.source or args.tool],
            capture_output=True,
            text=True,
            check=True,
        )
        Path(args.otlp).parent.mkdir(parents=True, exist_ok=True)
        Path(args.otlp).write_text(done.stdout, encoding="utf-8")
    print(f"{args.tool}: {len(made['requests'])} request(s) to {sorted({r['path'] for r in made['requests']})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
