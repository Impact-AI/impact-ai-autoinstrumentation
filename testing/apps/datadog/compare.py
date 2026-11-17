# SPDX-License-Identifier: Apache-2.0

"""The relay's byte difference: what a tracer sent against what its Datadog Agent received, request by request.

Two recording taps (agent_tap.py) see the same requests in one run: one between the tracer and the relay, one between
the relay and the Agent. Comparing two separate runs cannot work, since every payload carries its own times and ids.
A request is the same when its method, path, query, body and every header but the hop-by-hop ones are the same byte for
byte; Host must name the Agent's own address on the Agent's side, as it would without the relay. The answer the tracer
got must be the Agent's own: same status, same body. The measure is the number of requests and answers that differ, 0
in every cell.
"""

from __future__ import annotations

import base64

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection", "host"}


def _headers(record: dict) -> list[tuple[str, str]]:
    raw = record.get("rawHeaders") or list((record.get("headers") or {}).items())
    # The framing (chunked or Content-Length) may change on the way; the body's bytes, compared below, are what Datadog
    # reads, and Content-Length is checked against them on the Agent's side.
    return sorted(
        (name.lower(), value)
        for name, value in raw
        if name.lower() not in HOP_BY_HOP and name.lower() != "content-length"
    )


def _request(record: dict) -> tuple:
    return (record["method"], record["path"], record.get("query", ""), _headers(record), record.get("body_b64", ""))


def _content_length_ok(record: dict) -> bool:
    lengths = [value for name, value in record.get("rawHeaders") or [] if name.lower() == "content-length"]
    return all(int(value) == len(base64.b64decode(record.get("body_b64", ""))) for value in lengths)


def relay_byte_difference(sent: list[dict], received: list[dict], agent_host: str) -> tuple[int, list[str]]:
    """The number of requests and answers that differ between the two taps, and what differs, in the order sent."""
    problems: list[str] = []
    unmatched = list(received)
    for index, request in enumerate(sent):
        key = _request(request)
        match = next((other for other in unmatched if _request(other) == key), None)
        name = f"{request['method']} {request['path']} (#{index})"
        if match is None:
            problems.append(f"{name}: the Agent did not receive it byte for byte")
            continue
        unmatched.remove(match)
        host = (match.get("headers") or {}).get("Host") or (match.get("headers") or {}).get("host")
        if host != agent_host:
            problems.append(f"{name}: the Agent saw Host {host!r}, not its own address {agent_host!r}")
        if not _content_length_ok(match):
            problems.append(f"{name}: Content-Length does not match the body")
        theirs, ours = match.get("response") or {}, request.get("response") or {}
        if (theirs.get("status"), theirs.get("body_b64")) != (ours.get("status"), ours.get("body_b64")):
            problems.append(f"{name}: the tracer did not get the Agent's own answer")
    for extra in unmatched:
        problems.append(f"{extra['method']} {extra['path']}: the Agent received a request the tracer did not send")
    return len(problems), problems
