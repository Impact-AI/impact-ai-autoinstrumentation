# SPDX-License-Identifier: Apache-2.0

"""What LangSmith's recording backend received, read so that two runs of the same workload can be compared.

LangSmith's default tracing posts its runs to /runs/multipart: one multipart/form-data part per run operation
(`post.<run id>`, `patch.<run id>`) and one per field it sends apart (`post.<run id>.inputs`, `.outputs`, `.extra`,
`.events`, `.serialized`). Its OpenTelemetry mode posts OTLP/HTTP protobuf to /otel/v1/traces. A run of the same
workload differs from another only by what is generated: run and trace ids, span ids, times and the dotted order made
of them. `masked_runs` and `masked_spans` replace those, so that equal means LangSmith received the same.

Standard library only, except `masked_spans`, which reads protobuf with opentelemetry-proto (as the image tests do).
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import gzip
import json
import re

RUNS_PATH = "/runs/multipart"
OTLP_PATH = "/otel/v1/traces"

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HEX_ID = re.compile(r"\b(?:[0-9a-f]{32}|[0-9a-f]{16})\b")  # an OpenTelemetry trace or span id in a value
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")
DOTTED_TIME = re.compile(r"\d{8}T\d{6}\d*Z")
EPOCH_MS = 10**12  # a number this large is a time in milliseconds (the JavaScript SDK sends end_time so)


def body(record: dict) -> bytes:
    raw = base64.b64decode(record.get("body_b64") or record.get("bodyBase64") or "")
    encoding = (header(record, "Content-Encoding") or "").lower()
    return gzip.decompress(raw) if encoding == "gzip" else raw


def header(record: dict, name: str) -> str | None:
    return next((value for key, value in (record.get("headers") or {}).items() if key.lower() == name.lower()), None)


def parts(record: dict):
    """(name, payload) of each part of a multipart/form-data request, in order."""
    content_type = header(record, "Content-Type") or ""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body(record)
    )
    for part in message.iter_parts():
        yield part.get_param("name", header="content-disposition"), part.get_payload(decode=True)


def runs(records: list[dict]) -> dict[str, dict]:
    """Every run LangSmith posted, by run id: its operations in order and each of its parts, decoded."""
    out: dict[str, dict] = {}
    for record in records:
        if record["path"] != RUNS_PATH:
            continue
        for name, payload in parts(record):
            operation, _, rest = name.partition(".")
            run_id, _, field = rest.partition(".")
            run = out.setdefault(run_id, {"operations": []})
            if field:
                run[f"{operation}.{field}"] = json.loads(payload)
            else:
                run["operations"].append(operation)
                run.setdefault(operation, {}).update(json.loads(payload))
    return out


def mask(value):
    """A value with its generated ids and times replaced, everything else as it was."""
    if isinstance(value, dict):
        return {key: mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item) for item in value]
    if isinstance(value, str):
        return HEX_ID.sub("<id>", DOTTED_TIME.sub("<time>", ISO_TIME.sub("<time>", UUID.sub("<id>", value))))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= EPOCH_MS:
        return "<time>"
    return value


def masked_runs(records: list[dict]) -> list[dict]:
    """The runs, in the order they started, with their generated ids and times masked."""
    found = runs(records)

    def started(run: dict) -> tuple[str, str]:
        post = run.get("post") or {}
        return str(post.get("start_time") or ""), str(post.get("dotted_order") or "")

    ordered = sorted(found.values(), key=started)
    return [mask(run) for run in ordered]


def otlp_requests(records: list[dict], path: str = OTLP_PATH) -> list[dict]:
    """The OTLP/HTTP protobuf trace requests posted to `path`, as OTLP/JSON dicts."""
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    out = []
    for record in records:
        if record["path"] != path:
            continue
        request = ExportTraceServiceRequest()
        request.ParseFromString(body(record))
        converted = MessageToDict(request, use_integers_for_enums=True)
        # OTLP JSON differs from ordinary protobuf JSON: trace/span ids are hexadecimal, not base64.
        for resource in converted.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    for item in [span, *span.get("links", [])]:
                        for key in ("traceId", "spanId", "parentSpanId"):
                            if key in item:
                                item[key] = base64.b64decode(item[key]).hex()
        out.append(converted)
    return out


def masked_spans(records: list[dict], path: str = OTLP_PATH, scope: str = "langsmith") -> list[dict]:
    """The spans LangSmith made of its runs, with their resource, whether each is a root, and ids and times masked.

    Only the spans of LangSmith's own scope: the provider it registers is the process's, so a library that traces
    itself (FastAPI does) sends spans there too, as many as have ended when the batch is sent.
    """
    spans = []
    for request in otlp_requests(records, path):
        for resource_spans in request.get("resourceSpans", []):
            resource = resource_spans.get("resource", {}).get("attributes", [])
            for scope_spans in resource_spans.get("scopeSpans", []):
                if scope_spans.get("scope", {}).get("name") != scope:
                    continue
                for span in scope_spans.get("spans", []):
                    spans.append(
                        {
                            "resource": resource,
                            "scope": scope_spans.get("scope", {}),
                            "name": span.get("name"),
                            "root": "parentSpanId" not in span,
                            "attributes": sorted(span.get("attributes", []), key=lambda item: item["key"]),
                            "status": span.get("status", {}),
                        }
                    )
    return sorted((mask(span) for span in spans), key=lambda span: json.dumps(span, sort_keys=True))


if __name__ == "__main__":
    # Regenerate a copy-route fixture from the original protobuf recording, without changing its content.
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Write an agent's recorded LangSmith trace as OTLP JSON")
    parser.add_argument("recording", type=Path)
    parser.add_argument("agent")
    args = parser.parse_args()
    output = {"resourceSpans": []}
    for request in otlp_requests(json.loads(args.recording.read_text())["requests"]):
        for resource in request.get("resourceSpans", []):
            root = next(
                (
                    span
                    for scope in resource["scopeSpans"]
                    for span in scope["spans"]
                    if span["name"] == args.agent and not span.get("parentSpanId")
                ),
                None,
            )
            if root is None:
                continue
            scopes = [
                {**scope, "spans": [span for span in scope["spans"] if span["traceId"] == root["traceId"]]}
                for scope in resource["scopeSpans"]
            ]
            resource["scopeSpans"] = [scope for scope in scopes if scope["spans"]]
            resource["resource"]["attributes"] += [
                {"key": "impact.source.id", "value": {"stringValue": "langsmith"}},
                {"key": "impact.source.route", "value": {"stringValue": "copy"}},
            ]
            output["resourceSpans"].append(resource)
    if not output["resourceSpans"]:
        parser.error("the recording has no trace rooted at that agent")
    print(json.dumps(output, indent=2))
