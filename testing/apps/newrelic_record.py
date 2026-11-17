#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Record what a New Relic agent sends its collector, for the relay's New Relic decoder tests.

    python3 testing/apps/newrelic_record.py --how "..." --package newrelic=14.2.0 [--package ...] \\
        --output reporter/internal/relay/source/testdata/newrelic/python.recording.json \\
        -- python3 testing/apps/newrelic_app.py

It makes a throwaway certificate authority and a server certificate for localhost, starts the collector double
(testing/apps/newrelic_collector.py) with it, runs the command with the agent pointed at the double, and writes the
recording of the calls that carry spans and AI monitoring events (span_event_data and custom_event_data), in the form
testing/mock/recording.py writes. The agent trusts the authority through its own CA setting, which is how a workload
trusts the kit's relay: NEW_RELIC_CA_BUNDLE_PATH (Python, Java) and NEW_RELIC_CERTIFICATES (Node.js) are both set.
The license key is a planted, fictitious one. Needs openssl. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mock"))
from recording import recording  # noqa: E402

# A fictitious license key of the real length (40), planted so that the decoder tests can prove it reaches no output.
LICENSE_KEY = "planted0license0key0000000000000000NRAL"
DECODED = ("span_event_data", "custom_event_data")


def openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607 - fixed command


def authority(directory: Path) -> tuple[Path, Path, Path]:
    """(ca.pem, server.pem, server.key): a throwaway authority and a certificate for localhost signed by it."""
    openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key", cwd=directory)
    openssl(
        "req",
        "-x509",
        "-new",
        "-key",
        "ca.key",
        "-sha256",
        "-days",
        "1",
        "-subj",
        "/CN=recording CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign",
        "-out",
        "ca.pem",
        cwd=directory,
    )
    openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "server.key", cwd=directory)
    openssl("req", "-new", "-key", "server.key", "-subj", "/CN=localhost", "-out", "server.csr", cwd=directory)
    (directory / "server.ext").write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    openssl(
        "x509",
        "-req",
        "-in",
        "server.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-days",
        "1",
        "-sha256",
        "-extfile",
        "server.ext",
        "-out",
        "server.pem",
        cwd=directory,
    )
    return directory / "ca.pem", directory / "server.pem", directory / "server.key"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(url: str, ca: Path) -> bytes:
    context = ssl.create_default_context(cafile=str(ca))
    with urllib.request.urlopen(url, context=context, timeout=5) as response:  # noqa: S310 - a local double
        return response.read()


def wait_until_up(url: str, ca: Path, collector: subprocess.Popen) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if collector.poll() is not None:
            raise SystemExit("the collector double stopped")
        try:
            fetch(url, ca)
            return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the collector double did not start")


def agent_environment(port: int, ca: Path, app_name: str) -> dict[str, str]:
    """What points an agent at the double and switches its AI monitoring on; nothing else of its settings changes."""
    return {
        "NEW_RELIC_LICENSE_KEY": LICENSE_KEY,
        "NEW_RELIC_APP_NAME": app_name,
        "NEW_RELIC_HOST": "localhost",
        "NEW_RELIC_PORT": str(port),
        "NEW_RELIC_CA_BUNDLE_PATH": str(ca),
        "NEW_RELIC_CERTIFICATES": ca.read_text(),
        "NEW_RELIC_AI_MONITORING_ENABLED": "true",
        # Every trace sampled, so that its span events are sent (an unsampled transaction sends only its AI events).
        "NEW_RELIC_DISTRIBUTED_TRACING_SAMPLER_ROOT": "always_on",
        "NEW_RELIC_LOG": "stderr",
        "NEW_RELIC_LOG_LEVEL": os.environ.get("NEW_RELIC_LOG_LEVEL", "info"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--how", required=True, help="what ran, against what")
    parser.add_argument("--package", action="append", default=[], metavar="NAME=VERSION")
    parser.add_argument("--app-name", default="support-assistant")
    parser.add_argument("--recorded", default=datetime.date.today().isoformat())
    parser.add_argument("--output", required=True, help="the recording to write")
    parser.add_argument("--captured", help="also write every captured request here (for a look, never committed)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- the app to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command to run after --")
    packages = {}
    for package in args.package:
        name, _, version = package.partition("=")
        if not name or not version:
            parser.error(f"--package {package!r} is not NAME=VERSION")
        packages[name] = version

    with tempfile.TemporaryDirectory() as tmp:
        ca, cert, key = authority(Path(tmp))
        port = free_port()
        collector = subprocess.Popen(  # noqa: S603 - this interpreter and the double next to this file
            [
                sys.executable,
                str(HERE / "newrelic_collector.py"),
                "--port",
                str(port),
                "--cert",
                str(cert),
                "--key",
                str(key),
            ]
        )
        try:
            base = f"https://localhost:{port}"
            wait_until_up(base + "/healthz", ca, collector)
            env = {**os.environ, **agent_environment(port, ca, args.app_name)}
            subprocess.run(command, env=env, check=True, timeout=300)  # noqa: S603 - the command the caller gave
            captured = json.loads(fetch(base + "/_requests", ca))
        finally:
            collector.terminate()
            collector.wait(timeout=10)

    if args.captured:
        Path(args.captured).write_text(json.dumps(captured, indent=2) + "\n")
    methods = [parse_qs(request["query"]).get("method", [""])[0] for request in captured]
    kept = [request for request, method in zip(captured, methods, strict=True) if method in DECODED]
    print("captured: " + ", ".join(methods), file=sys.stderr)
    if not kept:
        raise SystemExit("the agent sent no span or custom events")
    out = recording(kept, args.how, packages, args.recorded)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
