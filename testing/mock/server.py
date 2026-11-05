#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""One mock for every outside party the tests need: model providers, the impact.ai ingest and a customer's own backend.

Standard library only, so it runs in a plain python image.

  Providers   POST /v1/chat/completions         OpenAI chat
              POST /v1/responses                OpenAI Responses (what the OpenAI Agents SDK calls)
              POST /v1/messages                 Anthropic messages
              POST /model/<id>/converse         Amazon Bedrock Converse (any model id, Nova's included)
              A chat or Responses request that offers tools and has no tool result yet is answered with a call of the
              first tool (arguments made from its schema); once the result is in, with the usual answer. That is one
              round of an agent loop.
  Ingest      POST /v1/traces, /v1/inventory    recorded; answered 202 unless a rule says otherwise (a snapshot with a
                                                workload member the intake does not know, or a stall that is not names
                                                alone, is refused 400 malformed_request, as the real intake refuses it)
              POST /v1/installs/exchange        the install token exchange (C10), see below
              GET  /v1/inventory/cover          the automatic coverage list (C11), see below
  Anything else POST/PUT is recorded and answered 200 (a customer's own collector or agent)

  GET  /_requests[?path=P&since=N]   every request so far, oldest first (bodies base64, JSON bodies also parsed)
  POST /_reset                        forget requests, rules, tokens, issued keys, events and the cover list
  POST /_control                      {"rules": [{"path": "/v1/inventory", "status": 503, "retry_after": 1, "times": 2,
                                        "delay": 0, "drop": false}], "require_key": "secret"}
  GET  /healthz

A rule answers the next `times` requests (all, when omitted) to its path; "delay" seconds are slept first, which is
how the tests make a gateway that accepts a connection and never answers. "drop" handles the request in full and then
closes the connection without an answer: the response is lost, the side effects are not.

Install token exchange (C10) and automatic coverage (C11), as the real Ingest serves them:

  POST /v1/installs/exchange          Authorization: Bearer impactinstall_<env>_<43 chars>; body impact.install.v1.
                                        200 {"installId","keyId","productId","autoCover","clusterName","replayed"}, or
                                        {"code","message"} with 400 malformed_request, 401 token_unknown, 403
                                        token_revoked, 409 token_used, 410 token_expired, 403 token_wrong_product, 403
                                        token_wrong_cluster (checked in this order); a rule can add 429 rate_limited and
                                        503 unavailable. Only the key's SHA-256 arrives here, never the key.
  GET  /v1/inventory/cover            Authorization: Bearer <install key>; {"schema":"impact.cover.v1","version",
                                        "automatic","workloads"} with an ETag, 304 on If-None-Match.

  Keys the exchange issued are accepted next to `require_key` on /v1/inventory, /v1/traces and the cover list; a
  snapshot for another cluster than the key was issued for is refused (403 key_cluster_mismatch). With no
  `require_key` and no key issued yet, nothing is checked (what the container tests rely on).

Test-only control (not recorded in /_requests):

  POST /_tokens         {"env","product","expires_in","auto_cover","cluster_name","bind":{"uid","arn"},"state"} ->
                          {"id","token","expires_at"}; state is live, used (spent on another key) or revoked
  POST /_tokens/revoke  {"id"} -> {"revoked": true|false}; a consumed token stays consumed
  POST /_keys/revoke    {"key_id"} or {"all": true} -> {"revoked": N}
  GET  /_installs       {"events": [...], "keys": [...], "tokens": [...]}, never a plain token or key
  POST /_cover          {"automatic","workloads"} replaces the list and bumps its version;  GET /_cover reads it
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROMPT_ECHO = True
ANSWER = "Hello from the stub."

EXCHANGE_PATH = "/v1/installs/exchange"
COVER_PATH = "/v1/inventory/cover"
KEYED_PATHS = ("/v1/inventory", "/v1/traces", COVER_PATH)  # the routes that want an ingest key
MAX_EXCHANGE_BODY = 4096
COVER_SCHEMA = "impact.cover.v1"

ENVIRONMENT = r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?"
TOKEN_GRAMMAR = re.compile(rf"impactinstall_({ENVIRONMENT})_[A-Za-z0-9_-]{{43}}")
PREFIX_GRAMMAR = re.compile(rf"impact_({ENVIRONMENT})_[A-Za-z0-9_-]{{8}}")
HASH_GRAMMAR = re.compile(r"[0-9a-f]{64}")
# What the real exchange reads of the request (capabilities/ai-coverage/go/exchange.go and inventory.go): a value that
# does not fit is a malformed_request, so a chart that sends one fails here as it would there.
PRODUCT_GRAMMAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}")
CLUSTER_UID_GRAMMAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")
CLUSTER_ARN_GRAMMAR = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._:/@+=,-]{0,511}")
CHART_VERSION_GRAMMAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
CLUSTER_NAME_RUNES = 128

# The refusals of the exchange: code -> (HTTP status, the fixed plain sentence). The sentences are the real exchange's,
# word for word: the chart prints them, and nothing of the request is echoed.
EXCHANGE_ERRORS = {
    "malformed_request": (400, "The install request is not valid. Update the chart and run the install command again."),
    "token_unknown": (401, "This install command is not recognised. Create a new one in Cover your AI."),
    "token_revoked": (
        403,
        "This install command was cancelled, or the person who made it can no longer install. "
        "Create a new one in Cover your AI.",
    ),
    "token_wrong_product": (
        403,
        "This install command is for another product. Create a new one in Cover your AI for this product.",
    ),
    "token_wrong_cluster": (
        403,
        "This install command is for a different cluster. Create a new one in Cover your AI for this cluster.",
    ),
    "token_used": (409, "This install command was already used. Create a new one in Cover your AI."),
    "token_expired": (
        410,
        "This install command expired. Install commands are valid for 15 minutes; create a new one in Cover your AI.",
    ),
    "rate_limited": (429, "Too many install attempts. Wait a minute, then run the install command again."),
    "unavailable": (
        503,
        "Impact AI could not take this install just now. The install command is unchanged: run it again in a moment.",
    ),
}
DEFAULT_RETRY_AFTER = 5

# An RLock: the exchange checks and consumes a token in one critical section and writes its events inside it.
state_lock = threading.RLock()
requests_seen: list[dict] = []
rules: list[dict] = []
require_key: str | None = None

tokens: dict[str, dict] = {}  # token id -> record
tokens_by_hash: dict[str, dict] = {}  # SHA-256 of the token -> record
issued_keys: list[dict] = []  # the keys the exchange issued, oldest first
keys_by_hash: dict[str, dict] = {}  # SHA-256 of the key -> issued key
events: list[dict] = []  # the install audit trail
cover_state: dict = {"schema": COVER_SCHEMA, "version": 1, "automatic": True, "workloads": []}


def tool_call_arguments(schema: dict | None) -> str:
    """JSON arguments for a tool, made from its parameter schema: a short string, a small number, true."""
    arguments = {}
    for name, spec in ((schema or {}).get("properties") or {}).items():
        kind = spec.get("type") if isinstance(spec, dict) else None
        arguments[name] = {"integer": 1, "number": 1.5, "boolean": True, "array": [], "object": {}}.get(kind, "Paris")
    return json.dumps(arguments)


def first_tool(request: dict) -> tuple[str, str] | None:
    """(name, arguments) of the first function tool a request offers (chat and Responses shape tools differently)."""
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if tool.get("type", "function") == "function" and function.get("name"):
            return function["name"], tool_call_arguments(function.get("parameters"))
    return None


def responses_body(request: dict) -> dict:
    inputs = request.get("input")
    answered = isinstance(inputs, list) and any(
        isinstance(item, dict) and item.get("type") == "function_call_output" for item in inputs
    )
    tool = None if answered else first_tool(request)
    if tool:
        output = [
            {
                "type": "function_call",
                "id": "fc_stub_1",
                "call_id": "call_stub_1",
                "name": tool[0],
                "arguments": tool[1],
                "status": "completed",
            }
        ]
    else:
        output = [
            {
                "type": "message",
                "id": "msg_stub",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
            }
        ]
    return {
        "id": "resp_stub",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": request.get("model", "gpt-4o-mini"),
        "output": output,
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "metadata": {},
        "parallel_tool_calls": True,
        "temperature": 1.0,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "usage": {
            "input_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 12,
        },
    }


def chat_body(request: dict) -> dict:
    answered = any(isinstance(m, dict) and m.get("role") == "tool" for m in request.get("messages") or [])
    tool = None if answered else first_tool(request)
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_stub_1", "type": "function", "function": {"name": tool[0], "arguments": tool[1]}}
            ],
        }
        finish = "tool_calls"
    else:
        message, finish = {"role": "assistant", "content": ANSWER}, "stop"
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 1,
        "model": request.get("model", "gpt-4o-mini"),
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
    }


def provider_body(path: str, request: dict) -> dict | None:
    if path.endswith("/v1/chat/completions"):
        return chat_body(request)
    if path.endswith("/v1/responses"):
        return responses_body(request)
    if path.endswith("/v1/messages"):
        return {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": request.get("model", "claude-stub"),
            "content": [{"type": "text", "text": ANSWER}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 5},
        }
    if path.startswith("/model/") and path.endswith("/converse"):
        usage = {"inputTokens": 7, "outputTokens": 5, "totalTokens": 12}
        if "/amazon.nova" in path:
            # Nova answers in the same Converse shape as every other model; its usage also names the prompt cache.
            usage.update({"cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": ANSWER}]}},
            "stopReason": "end_turn",
            "usage": usage,
            "metrics": {"latencyMs": 12},
        }
    return None


def decoded_json(headers, body: bytes):
    try:
        raw = gzip.decompress(body) if headers.get("Content-Encoding") == "gzip" else body
        return json.loads(raw)
    except Exception:  # noqa: BLE001 - protobuf and msgpack bodies are simply not JSON
        return None


# --- the install token exchange (C10), the keys it issues and the cover list (C11) ----------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# What the real exchange treats as a credential in a value it is given (capabilities/ai-coverage/go/inventory.go): a
# key or an install token by its exact shape, a Bearer header, a JWT, cloud and code-host tokens, a private key.
CREDENTIAL = re.compile(
    r"impact(?:install)?_[a-z0-9-]{1,32}_[A-Za-z0-9_-]{43}"
    r"|(?i:\bBearer +[A-Za-z0-9._~+/-]+=*)"
    r"|eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|(?:sk-|ghp_|github_pat_|xoxb-|xoxp-|AIza)[A-Za-z0-9_-]{20,}"
    r"|(?s:-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)"
)


def credential_shaped(value: str) -> bool:
    return CREDENTIAL.search(value) is not None


def exchange_bearer(header: str | None) -> str | None:
    """The install token of an Authorization header as the real exchange reads it: the word Bearer in any case, one
    space, the token, and nothing else."""
    parts = (header or "").split(" ")
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def bearer_of(header: str | None) -> str | None:
    """The credential of an `Authorization: Bearer <credential>` header, or None."""
    if header and header.startswith("Bearer ") and len(header) > len("Bearer "):
        return header[len("Bearer ") :]
    return None


def reset_installs() -> None:
    """Forget the tokens, the keys they issued, the audit trail and the cover list. Called with state_lock held."""
    tokens.clear()
    tokens_by_hash.clear()
    issued_keys.clear()
    keys_by_hash.clear()
    events.clear()
    cover_state.update(version=1, automatic=True, workloads=[])


def add_event(kind: str, token: dict, *, code: str | None = None, key: dict | None = None, uid: str | None = None):
    """One row of the audit trail. Called with state_lock held."""
    events.append(
        {
            "seq": len(events) + 1,
            "at": time.time(),
            "kind": kind,
            "code": code,
            "token_id": token["id"],
            "key_id": key["key_id"] if key else None,
            "product": token["product"],
            "cluster_uid": uid if uid is not None else token["bind"]["uid"],
        }
    )


def key_admitted(bearer: str | None) -> bool:
    """Whether ingest knows the key: the one `require_key` names, or one the exchange issued and nobody revoked."""
    with state_lock:
        if require_key is None and not issued_keys:
            return True  # nothing is configured yet: the container tests send no key at all
        if bearer is None:
            return False
        if require_key is not None and bearer == require_key:
            return True
        issued = keys_by_hash.get(sha256_hex(bearer))
        return issued is not None and not issued["revoked"]


# What the real intake reads of a workload (capabilities/ai-coverage/go/inventory.go and src/inventory.ts). It reads
# exactly the contract: a snapshot with a member it has not been taught is refused whole (400 malformed_request), and
# the cluster's inventory stays as it was until the kit stops sending it. So a field joins this list when the intake
# reads it, and a kit that sends one earlier fails here as it would there.
WORKLOAD_MEMBERS = {
    "namespace", "kind", "name", "pods", "serviceAccount", "annotations", "containers",  # the workload
    "labels", "namespaceLabels", "autoCovered",  # automatic coverage: the impact.ai labels and the marker
    "stall",  # why the pods do not start: names only
}  # fmt: skip
# The stall (names alone): the reason of the Deployment's ReplicaFailure condition, and the reasons the injected init
# container waits for. A reason is a plain CamelCase name, so a sentence, a key or a path never fits.
STALL_MEMBERS = {"replicaFailure", "initWaiting"}
STALL_REASON = re.compile(r"[A-Z][A-Za-z0-9]{1,62}")
MAX_STALL_REASONS = 8


def stall_reason(value) -> bool:
    return isinstance(value, str) and STALL_REASON.fullmatch(value) is not None and not credential_shaped(value)


def stall_problem(stall) -> str | None:
    """Why a stall is not what the contract says, or None. A stall that is null reads as none, like the other additive
    fields; one that holds nothing is refused: the kit leaves the member out when nothing is wrong."""
    if stall is None:
        return None
    if not isinstance(stall, dict) or not stall:
        return "stall: want an object that names a reason"
    if set(stall) - STALL_MEMBERS:
        return "stall: a member the intake does not know"
    if "replicaFailure" in stall and not stall_reason(stall["replicaFailure"]):
        return "stall.replicaFailure: not a plain name"
    if "initWaiting" in stall:
        waiting = stall["initWaiting"]
        if not isinstance(waiting, list) or not 1 <= len(waiting) <= MAX_STALL_REASONS:
            return f"stall.initWaiting: want 1 to {MAX_STALL_REASONS} reasons"
        if not all(stall_reason(reason) for reason in waiting) or len(set(waiting)) != len(waiting):
            return "stall.initWaiting: want plain names, once each"
    return None


def inventory_problem(document) -> str | None:
    """Why the workloads of a snapshot are not what the intake reads, or None. Only the workloads are read: the rest of
    a snapshot is the reporter's business, and the tests that post a bare one rely on that."""
    workloads = document.get("workloads") if isinstance(document, dict) else None
    if not isinstance(workloads, list):
        return None
    for index, workload in enumerate(workloads):
        if not isinstance(workload, dict):
            return f"workloads.{index}: want an object"
        if set(workload) - WORKLOAD_MEMBERS:
            return f"workloads.{index}: a member the intake does not know"
        problem = stall_problem(workload.get("stall"))
        if problem is not None:
            return f"workloads.{index}.{problem}"
    return None


def cluster_mismatch(bearer: str | None, document) -> bool:
    """A snapshot for another cluster than the one the key was issued for (a key without a cluster is unchanged)."""
    if bearer is None or not isinstance(document, dict):
        return False
    with state_lock:
        if require_key is not None and bearer == require_key:
            return False
        issued = keys_by_hash.get(sha256_hex(bearer))
    if issued is None or not issued["cluster_uid"]:
        return False
    cluster = document.get("cluster")
    presented = cluster.get("uid") if isinstance(cluster, dict) else None
    return presented != issued["cluster_uid"]


def exchange_error(code: str, retry_after=None) -> tuple[int, dict, dict]:
    status, message = EXCHANGE_ERRORS[code]
    return status, {"code": code, "message": message}, ({"Retry-After": retry_after} if retry_after is not None else {})


def exact(value, names: set[str]) -> bool:
    """An object with exactly these members: an unknown one is refused as firmly as a missing one."""
    return isinstance(value, dict) and set(value) == names


def text_of(value, limit: int) -> bool:
    """Free text as the real exchange reads a cluster name: printable, 1 to `limit` characters, not a credential."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
        and not credential_shaped(value)
    )


def fits(grammar: re.Pattern, value) -> bool:
    """A value of a grammar that is not a credential either (the real exchange's plain())."""
    return isinstance(value, str) and grammar.fullmatch(value) is not None and not credential_shaped(value)


def has_only(value, required: set[str], optional: set[str] = frozenset()) -> bool:
    """An object with every required member and no member beyond the optional ones."""
    return isinstance(value, dict) and required <= set(value) <= required | optional


def install_request(body: bytes) -> dict | None:
    """The exchange body when it is the strict impact.install.v1 document, else None. The returned document has the
    optional members of the cluster (arn, name) filled in as null when they were absent."""
    if len(body) > MAX_EXCHANGE_BODY:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not exact(document, {"schema", "productId", "cluster", "key", "chart"}):
        return None
    cluster, key, chart = document["cluster"], document["key"], document["chart"]
    if not has_only(cluster, {"uid"}, {"arn", "name"}):
        return None
    cluster = {"arn": None, "name": None, **cluster}
    valid = (
        document["schema"] == "impact.install.v1"
        and fits(PRODUCT_GRAMMAR, document["productId"])
        and fits(CLUSTER_UID_GRAMMAR, cluster["uid"])
        and (cluster["arn"] is None or fits(CLUSTER_ARN_GRAMMAR, cluster["arn"]))
        and (cluster["name"] is None or text_of(cluster["name"], CLUSTER_NAME_RUNES))
        and exact(key, {"lookupHash", "displayPrefix"})
        and fits(HASH_GRAMMAR, key["lookupHash"])
        and fits(PREFIX_GRAMMAR, key["displayPrefix"])
        and exact(chart, {"version"})
        and fits(CHART_VERSION_GRAMMAR, chart["version"])
    )
    return {**document, "cluster": cluster} if valid else None


def prefix_environment(request: dict) -> str:
    """The environment a valid request's displayPrefix names (impact_<environment>_<8 characters>)."""
    return PREFIX_GRAMMAR.fullmatch(request["key"]["displayPrefix"]).group(1)


def install_answer(key: dict, token: dict, replayed: bool) -> dict:
    return {
        "installId": key["install_id"],
        "keyId": key["key_id"],
        "productId": token["product"],
        "autoCover": token["auto_cover"],
        "clusterName": token["cluster_name"],
        "replayed": replayed,
    }


def json_body(content_type: str | None, content_encoding: str | None) -> bool:
    """The exchange reads application/json, uncompressed, and nothing else."""
    media = (content_type or "").split(";")[0].strip().lower()
    return media == "application/json" and (content_encoding or "").strip().lower() in ("", "identity")


def exchange(
    authorization: str | None, body: bytes, content_type: str | None = "application/json", content_encoding=None
) -> tuple[int, dict, dict]:
    """POST /v1/installs/exchange: (HTTP status, JSON answer, extra headers). The checks run in the order of C10."""
    request = install_request(body) if json_body(content_type, content_encoding) else None
    bearer = exchange_bearer(authorization)
    shape = TOKEN_GRAMMAR.fullmatch(bearer) if bearer else None
    if request is None or (shape and prefix_environment(request) != shape.group(1)):
        return exchange_error("malformed_request")  # the key's prefix names the token's environment, once that is known
    if shape is None:
        return exchange_error("token_unknown")
    with state_lock:  # check and consume as one step: two exchanges at once have one winner
        token = tokens_by_hash.get(sha256_hex(bearer))
        if token is None:
            return exchange_error("token_unknown")  # nothing about an unknown token is written down
        cluster = request["cluster"]

        def refuse(code: str) -> tuple[int, dict, dict]:
            add_event("refused", token, code=code, uid=cluster["uid"])
            return exchange_error(code)

        if token["state"] == "revoked":
            return refuse("token_revoked")
        lookup_hash = request["key"]["lookupHash"]
        if token["state"] == "used":
            spent = token["install"]
            replay = (
                spent is not None and spent["lookup_hash"] == lookup_hash and request["productId"] == token["product"]
            )
            if replay:  # the answer got lost: the same install (a repeat is still the same after the token expired)
                add_event("replayed", token, key=spent, uid=spent["cluster_uid"])
                return 200, install_answer(spent, token, replayed=True), {}
            return refuse("token_used")
        if time.time() >= token["expires_at"]:
            return refuse("token_expired")
        if request["productId"] != token["product"]:
            return refuse("token_wrong_product")
        bound = token["bind"]
        if (bound["uid"] is not None and cluster["uid"] != bound["uid"]) or (
            bound["arn"] is not None and cluster["arn"] != bound["arn"]
        ):
            return refuse("token_wrong_cluster")
        if lookup_hash in keys_by_hash:  # a key is issued once: the client made two installs from one key
            return exchange_error("malformed_request")  # nothing is recorded, as the real exchange does not
        key = {
            "key_id": "pk_" + secrets.token_hex(12),
            "install_id": "ins_" + secrets.token_hex(12),
            "token_id": token["id"],
            "product": token["product"],
            "cluster_uid": cluster["uid"],
            "cluster_arn": cluster["arn"],
            "cluster_name": cluster["name"],
            "lookup_hash": lookup_hash,
            "display_prefix": request["key"]["displayPrefix"],
            "chart_version": request["chart"]["version"],
            "revoked": False,
        }
        issued_keys.append(key)
        keys_by_hash[lookup_hash] = key
        token.update(state="used", consumed_key_hash=lookup_hash, install=key)
        add_event("exchanged", token, key=key, uid=cluster["uid"])
        return 200, install_answer(key, token, replayed=False), {}


def scripted_exchange_answer(rule: dict) -> tuple[int, dict, dict]:
    """What a rule with a status makes of the exchange: 429 and 503 speak the exchange's language."""
    status = int(rule.get("status", 200))
    retry_after = rule.get("retry_after")
    for code, (known, _) in EXCHANGE_ERRORS.items():
        if status == known and code in ("rate_limited", "unavailable"):
            return exchange_error(code, DEFAULT_RETRY_AFTER if retry_after is None else retry_after)
    return status, {}, ({"Retry-After": retry_after} if retry_after is not None else {})


def etag_matches(header: str | None, etag: str) -> bool:
    for candidate in (header or "").split(","):
        candidate = candidate.strip().removeprefix("W/")
        if candidate in ("*", etag):
            return True
    return False


def cover_key_refusal(bearer: str | None) -> tuple[int, dict] | None:
    """Why the cover route does not serve this bearer, or None when it does. The route serves an install's own key: a
    key that was pasted (the one `require_key` names) has no install, and is refused for that."""
    with state_lock:
        if bearer is None:
            return 401, {"code": "credential_missing"}
        issued = keys_by_hash.get(sha256_hex(bearer))
        if issued is not None:
            return (401, {"code": "credential_revoked"}) if issued["revoked"] else None
        if require_key is not None and bearer == require_key:
            return 403, {"code": "not_an_install_key"}
        return 401, {"code": "credential_invalid"}


def cover_etag(version: int, automatic: bool, cluster_uid: str) -> str:
    """An opaque name for the state the list is a function of, as the real route makes one."""
    name = "|".join([COVER_SCHEMA, cluster_uid, "inject", str(automatic).lower(), str(version)])
    return '"' + sha256_hex(name)[:24] + '"'


def cover_answer(if_none_match: str | None, bearer: str | None = None) -> tuple[int, bytes, dict]:
    """GET /v1/inventory/cover: the list with its ETag, or 304 when the caller has this version."""
    with state_lock:
        state = json.dumps(cover_state)
        issued = keys_by_hash.get(sha256_hex(bearer)) if bearer else None
        etag = cover_etag(cover_state["version"], cover_state["automatic"], issued["cluster_uid"] if issued else "")
    headers = {"ETag": etag, "Cache-Control": "no-store"}
    if etag_matches(if_none_match, etag):
        return 304, b"", headers
    return 200, state.encode(), headers


def object_of(body: bytes) -> dict | None:
    try:
        document = json.loads(body or b"{}")
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def mint_token(body: bytes) -> tuple[int, dict]:
    """POST /_tokens: an install token as the product would issue it."""
    payload = object_of(body)
    if payload is None:
        return 400, {"error": "the body is not a JSON object"}
    env, product = payload.get("env", "e2e"), payload.get("product", "prod_e2e")
    expires_in, auto_cover = payload.get("expires_in", 900), payload.get("auto_cover", True)
    name, state = payload.get("cluster_name"), payload.get("state", "live")
    bind = payload.get("bind") or {}
    problem = (
        "env is not an environment name"
        if not (isinstance(env, str) and re.fullmatch(ENVIRONMENT, env))
        else "product is not a name"
        if not text_of(product, 128)
        else "expires_in is not a number of seconds"
        if not is_number(expires_in)
        else "auto_cover is not a boolean"
        if not isinstance(auto_cover, bool)
        else "cluster_name is not a string"
        if name is not None and not isinstance(name, str)
        else "state is not live, used or revoked"
        if state not in ("live", "used", "revoked")
        else "bind is not {uid, arn}"
        if not isinstance(bind, dict) or any(bind.get(k) is not None and not isinstance(bind[k], str) for k in bind)
        else None
    )
    if problem:
        return 400, {"error": problem}
    token = f"impactinstall_{env}_{secrets.token_urlsafe(32)}"
    record = {
        "id": "itk_" + secrets.token_hex(12),
        "env": env,
        "product": product,
        "expires_at": time.time() + expires_in,
        "auto_cover": auto_cover,
        "cluster_name": name,
        "bind": {"uid": bind.get("uid"), "arn": bind.get("arn")},
        "state": state,
        "consumed_key_hash": None,
        "install": None,
    }
    if state == "used":  # spent elsewhere, on a key this mock never saw
        record["consumed_key_hash"] = sha256_hex("spent-elsewhere:" + record["id"])
    with state_lock:
        tokens[record["id"]] = record
        tokens_by_hash[sha256_hex(token)] = record
        add_event("issued", record)
    return 200, {"id": record["id"], "token": token, "expires_at": record["expires_at"]}


def revoke_token(body: bytes) -> tuple[int, dict]:
    payload = object_of(body)
    with state_lock:
        token = tokens.get(payload.get("id")) if payload and isinstance(payload.get("id"), str) else None
        if token is None:
            return 404, {"error": "no such token"}
        if token["state"] != "live":  # idempotent, and a consumed token stays consumed
            return 200, {"revoked": False}
        token["state"] = "revoked"
        add_event("revoked", token)
    return 200, {"revoked": True}


def revoke_keys(body: bytes) -> tuple[int, dict]:
    payload = object_of(body)
    if payload is None or not (payload.get("all") is True or isinstance(payload.get("key_id"), str)):
        return 400, {"error": 'want {"all": true} or {"key_id": "pk_..."}'}
    with state_lock:
        everything = payload.get("all") is True
        chosen = issued_keys if everything else [k for k in issued_keys if k["key_id"] == payload["key_id"]]
        if not chosen and not everything:
            return 404, {"error": "no such key"}
        changed = [k for k in chosen if not k["revoked"]]
        for key in changed:
            key["revoked"] = True
    return 200, {"revoked": len(changed)}


def set_cover(body: bytes) -> tuple[int, dict]:
    payload = object_of(body)
    automatic, workloads = (payload or {}).get("automatic", True), (payload or {}).get("workloads", [])
    if payload is None or not isinstance(automatic, bool):
        return 400, {"error": "want an object with a boolean automatic and a list of workloads"}
    if not isinstance(workloads, list) or not all(isinstance(w, dict) for w in workloads):
        return 400, {"error": "workloads must be a list of objects"}
    with state_lock:
        cover_state.update(version=cover_state["version"] + 1, automatic=automatic, workloads=workloads)
        return 200, dict(cover_state)


def installs_view() -> dict:
    with state_lock:
        return {
            "events": [dict(e) for e in events],
            "keys": [{k: v for k, v in key.items()} for key in issued_keys],
            "tokens": [
                {k: t[k] for k in ("id", "product", "state", "expires_at", "consumed_key_hash")}
                for t in tokens.values()
            ],
        }


def control_post(path: str, body: bytes) -> tuple[int, dict] | None:
    """The test-only POST endpoints; None for any other path."""
    global require_key
    if path == "/_reset":
        with state_lock:
            requests_seen.clear()
            rules.clear()
            require_key = None
            reset_installs()
        return 200, {}
    if path == "/_control":
        payload = json.loads(body or b"{}")
        with state_lock:
            rules[:] = [dict(r, _left=r.get("times")) for r in payload.get("rules", [])]
            require_key = payload.get("require_key")
        return 200, {}
    handlers = {
        "/_tokens": mint_token,
        "/_tokens/revoke": revoke_token,
        "/_keys/revoke": revoke_keys,
        "/_cover": set_cover,
    }
    return handlers[path](body) if path in handlers else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes = b"{}", headers: dict | None = None, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def _drop(self):
        """Close the connection without an answer: the request was handled, only the response is lost."""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # kubectl --raw through the API server's service proxy, and some collectors, send chunked bodies.
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # trailers
                        pass
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if parts.path == "/_requests":
            query = parse_qs(parts.query)
            wanted = query.get("path", [None])[0]
            since = int(query.get("since", ["0"])[0])
            with state_lock:
                out = [r for r in requests_seen if r["seq"] > since and (wanted is None or r["path"] == wanted)]
            return self._send(200, json.dumps(out).encode())
        if parts.path == "/_installs":
            return self._send(200, json.dumps(installs_view()).encode())
        if parts.path == "/_cover":
            with state_lock:
                return self._send(200, json.dumps(cover_state).encode())
        if parts.path == COVER_PATH:
            return self._handle()
        # A GET to anything else is a customer's probe or a provider health check: answer, but do not record.
        return self._send(200, b'{"ok":true}')

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self):  # noqa: N802
        self._handle()

    do_PUT = do_POST  # noqa: N815

    def _answer(self, path: str, body: bytes, rule: dict | None, drop: bool, record: dict):
        """(status, content, extra headers, delay) for a recorded request. `rule` is the one that matched, if any."""
        authorization = self.headers.get("Authorization")
        bearer = bearer_of(authorization)
        delay = float(rule.get("delay", 0)) if rule is not None else 0.0
        answering = None if drop else rule  # a dropped request is handled as usual
        if path == EXCHANGE_PATH:
            if answering is not None and "status" in answering:
                status, payload, extra = scripted_exchange_answer(answering)
            else:
                status, payload, extra = exchange(
                    authorization, body, self.headers.get("Content-Type"), self.headers.get("Content-Encoding")
                )
            return status, json.dumps(payload).encode(), {**extra, "Cache-Control": "no-store"}, delay
        if path == COVER_PATH:
            refusal = cover_key_refusal(bearer)
            if refusal is not None:
                return refusal[0], json.dumps(refusal[1]).encode(), {"Cache-Control": "no-store"}, 0.0
        elif path in KEYED_PATHS and not key_admitted(bearer):
            return 401, b'{"error":"unauthorized"}', {}, 0.0
        if answering is not None:
            extra = {"Retry-After": answering["retry_after"]} if "retry_after" in answering else {}
            return answering.get("status", 200), b"{}", extra, delay
        if path == COVER_PATH:
            status, content, extra = cover_answer(self.headers.get("If-None-Match"), bearer)
            return status, content, extra, delay
        provider = provider_body(path, self._json(body))
        if provider is not None:
            return 200, json.dumps(provider).encode(), {}, delay
        if path == "/v1/inventory":
            if inventory_problem(record["json"]) is not None:  # the body is read before it is stored
                return 400, b'{"code":"malformed_request"}', {}, delay
            if cluster_mismatch(bearer, record["json"]):
                return 403, b'{"code":"key_cluster_mismatch"}', {}, delay
            return 202, b"{}", {}, delay
        return 200, b"{}", {}, delay

    def _handle(self):
        body = self._body()
        parts = urlsplit(self.path)
        path = parts.path
        if self.command in ("POST", "PUT"):
            control = control_post(path, body)
            if control is not None:
                return self._send(control[0], json.dumps(control[1]).encode())

        headers = {k: v for k, v in self.headers.items()}
        with state_lock:
            seq = len(requests_seen) + 1
            record = {
                "seq": seq,
                "time": time.time(),
                "method": self.command,
                "path": path,
                "query": parts.query,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode(),
                "json": decoded_json(self.headers, body),
            }
            requests_seen.append(record)
            matched = None
            for rule in rules:
                if rule["path"] == path and rule.get("_left") != 0:
                    matched = rule
                    if rule.get("_left") is not None:
                        rule["_left"] -= 1
                    break
        drop = bool(matched and matched.get("drop"))
        status, content, extra, delay = self._answer(path, body, matched, drop, record)
        if delay:
            time.sleep(delay)
        with state_lock:
            record["status"] = status
        if drop:
            return self._drop()
        self._send(status, content, extra)

    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            value = json.loads(body)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a test double inside a container
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
