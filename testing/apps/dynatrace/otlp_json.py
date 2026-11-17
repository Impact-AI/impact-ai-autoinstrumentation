# SPDX-License-Identifier: Apache-2.0

"""Read what the Dynatrace endpoint mock recorded: binary protobuf ExportTraceServiceRequests as OTLP/JSON.

OTLP/JSON as the OpenTelemetry specification writes it (and as impact.ai's ingest reads it): lowerCamelCase keys,
hexadecimal trace and span ids, enums as integers, 64-bit integers as strings. Needs opentelemetry-proto (the kit's
test environment has it).

    python3 otlp_json.py captured.json [--path /api/v2/otlp/v1/traces] > request.otlp.json
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ID_KEYS = ("traceId", "spanId", "parentSpanId")


def _hex_ids(node):
    """Turn the base64 of protobuf's JSON mapping into the hexadecimal ids of OTLP/JSON, wherever they are."""
    if isinstance(node, dict):
        return {
            key: base64.b64decode(value).hex() if key in ID_KEYS and isinstance(value, str) else _hex_ids(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_hex_ids(item) for item in node]
    return node


def request_of(body: bytes, content_encoding: str = "") -> dict:
    """One binary protobuf ExportTraceServiceRequest as OTLP/JSON."""
    if content_encoding.strip().lower() == "gzip":
        body = gzip.decompress(body)
    message = ExportTraceServiceRequest()
    message.ParseFromString(body)
    return _hex_ids(MessageToDict(message, use_integers_for_enums=True))


def header(headers: dict, name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def merged(captured: list[dict], path_suffix: str = "/v1/traces") -> dict:
    """Every trace request of a capture, in order, as one OTLP/JSON request (resourceSpans concatenated)."""
    resource_spans = []
    for record in captured:
        if not record["path"].endswith(path_suffix) or record.get("status", 200) != 200:
            continue
        body = base64.b64decode(record.get("body_b64") or record.get("bodyBase64") or "")
        resource_spans += request_of(body, header(record.get("headers") or {}, "Content-Encoding")).get(
            "resourceSpans", []
        )
    return {"resourceSpans": resource_spans}


def with_provenance(request: dict, source: str, route: str) -> dict:
    """The request with the resource attributes a copy carries to impact.ai: which source, by which route."""
    stamped = {"impact.source.id": source, "impact.source.route": route}
    for resource_spans in request.get("resourceSpans", []):
        resource = resource_spans.setdefault("resource", {})
        kept = [a for a in resource.get("attributes", []) if a.get("key") not in stamped]
        resource["attributes"] = kept + [{"key": k, "value": {"stringValue": v}} for k, v in stamped.items()]
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("captured", help="what GET /_requests of the mock answered, or a recording")
    parser.add_argument("--path", default="/v1/traces", help="keep the requests whose path ends so")
    parser.add_argument("--source", help="stamp impact.source.id with this source id (and --route) on every resource")
    parser.add_argument("--route", default="copy", help="the route --source stamps (impact.source.route)")
    args = parser.parse_args(argv)
    with open(args.captured, encoding="utf-8") as file:
        captured = json.load(file)
    if isinstance(captured, dict):
        captured = captured.get("requests", [])
    request = merged(captured, args.path)
    if args.source:
        request = with_provenance(request, args.source, args.route)
    json.dump(request, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
