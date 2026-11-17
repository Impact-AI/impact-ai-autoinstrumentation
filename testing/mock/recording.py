#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Turn the requests the recording mock captured into a recording for the relay's decoder tests.

    curl -s http://127.0.0.1:8080/_requests > captured.json
    python3 testing/mock/recording.py captured.json --how "what ran, against what" --package NAME=VERSION \\
        [--path /v0.4/traces] [--recorded YYYY-MM-DD] > testdata/<source>/<name>.recording.json

The mock's GET /_requests gives every request with its method, path, query, headers and body (base64). A recording
(reporter/internal/relay/relaytest) keeps exactly these, and says how, with what and when it was made. Standard library
only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys


def header(headers: dict, name: str) -> str:
    """A header's value, whatever the case of its name; empty when it is not there."""
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def recording(captured: list[dict], how: str, packages: dict[str, str], recorded: str, paths=None) -> dict:
    """The recording of the captured requests, or of those to one of `paths`."""
    requests = []
    for request in captured:
        if paths and request["path"] not in paths:
            continue
        headers = dict(request.get("headers") or {})
        requests.append(
            {
                "method": request["method"],
                "path": request["path"],
                "query": request.get("query") or "",
                "headers": headers,
                "contentType": header(headers, "Content-Type"),
                "contentEncoding": header(headers, "Content-Encoding"),
                "bodyBase64": request.get("body_b64") or "",
            }
        )
    return {"how": how, "packages": dict(sorted(packages.items())), "recorded": recorded, "requests": requests}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("captured", help="what GET /_requests of the mock answered, or - for standard input")
    parser.add_argument("--how", required=True, help="what ran, against what, and how the requests were captured")
    parser.add_argument("--package", action="append", default=[], metavar="NAME=VERSION", help="a pinned package")
    parser.add_argument("--recorded", default=datetime.date.today().isoformat(), help="the date, YYYY-MM-DD")
    parser.add_argument("--path", action="append", dest="paths", help="keep only the requests to this path")
    args = parser.parse_args(argv)
    packages = {}
    for package in args.package:
        name, equals, version = package.partition("=")
        if not equals or not name or not version:
            parser.error(f"--package {package!r} is not NAME=VERSION")
        packages[name] = version
    if args.captured == "-":
        captured = json.load(sys.stdin)
    else:
        with open(args.captured, encoding="utf-8") as file:
            captured = json.load(file)
    json.dump(recording(captured, args.how, packages, args.recorded, args.paths), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
