# SPDX-License-Identifier: Apache-2.0

"""The mock ingest (testing/mock/server.py) stands in for impact.ai in the end-to-end suite.

The kit's exchange hook and its cover pod are tested against nothing else, so the mock has to answer the install token
exchange (C10) and serve the automatic-coverage list (C11) the way the real Ingest does: the same codes, in the same
order, with the same plain sentences. This file pins that, and the behaviour the older tests rely on (require_key,
rules, the recorded requests).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import importlib.util
import json
import re
import secrets
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "testing" / "mock" / "server.py"
EXCHANGE = "/v1/installs/exchange"
COVER = "/v1/inventory/cover"
UID = "4e5622e7-f420-499c-9662-50af7f8a64b9"
ARN = "arn:aws:eks:eu-west-1:123456789012:cluster/kind-e2e"
TOKEN_SHAPE = re.compile(r"^impactinstall_[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?_[A-Za-z0-9_-]{43}$")

# The fixed plain sentence of every refusal (C10), with its HTTP status.
REFUSALS = {
    "malformed_request": (400, "The install request is not valid. Update the chart and run the install command again."),
    "token_unknown": (401, "This install command is not recognised. Create a new one in Cover your AI."),
    "token_revoked": (
        403,
        "This install command was cancelled, or the person who made it can no longer install. "
        "Create a new one in Cover your AI.",
    ),
    "token_used": (409, "This install command was already used. Create a new one in Cover your AI."),
    "token_expired": (
        410,
        "This install command expired. Install commands are valid for 15 minutes; create a new one in Cover your AI.",
    ),
    "token_wrong_product": (
        403,
        "This install command is for another product. Create a new one in Cover your AI for this product.",
    ),
    "token_wrong_cluster": (
        403,
        "This install command is for a different cluster. Create a new one in Cover your AI for this cluster.",
    ),
    "rate_limited": (429, "Too many install attempts. Wait a minute, then run the install command again."),
    "unavailable": (
        503,
        "Impact AI could not take this install just now. The install command is unchanged: run it again in a moment.",
    ),
}


class Reply:
    def __init__(self, status: int, headers: dict[str, str], raw: bytes) -> None:
        self.status = status
        self.headers = headers
        self.raw = raw

    @property
    def json(self):
        return json.loads(self.raw) if self.raw else None

    @property
    def text(self) -> str:
        return self.raw.decode()


class Mock:
    """The mock server running in this process, and a client for it."""

    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method: str, path: str, body=None, headers: dict[str, str] | None = None) -> Reply:
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
        finally:
            connection.close()

    def get(self, path: str, headers: dict[str, str] | None = None) -> Reply:
        return self.request("GET", path, headers=headers)

    def post(self, path: str, body=None, *, bearer: str | None = None, headers: dict[str, str] | None = None) -> Reply:
        sent = dict(headers or {})
        if bearer is not None:
            sent["Authorization"] = f"Bearer {bearer}"
        return self.request("POST", path, body, sent)

    # --- control -----------------------------------------------------------------------------------------------

    def reset(self) -> None:
        assert self.post("/_reset", {}).status == 200

    def control(self, rules: list[dict] | None = None, require_key: str | None = None) -> None:
        assert self.post("/_control", {"rules": rules or [], "require_key": require_key}).status == 200

    def mint(self, **fields) -> dict:
        reply = self.post("/_tokens", fields)
        assert reply.status == 200, reply.text
        return reply.json

    def installs(self) -> dict:
        reply = self.get("/_installs")
        assert reply.status == 200
        return reply.json

    def events(self) -> list[dict]:
        return self.installs()["events"]

    def requests(self, path: str | None = None, since: int = 0) -> list[dict]:
        query = "&".join(part for part in (f"path={path}" if path else "", f"since={since}" if since else "") if part)
        return self.get("/_requests" + (f"?{query}" if query else "")).json

    # --- the exchange ------------------------------------------------------------------------------------------

    def exchange(self, token: str | None, body=None, *, headers: dict[str, str] | None = None) -> Reply:
        sent = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        return self.request("POST", EXCHANGE, body, sent)

    def issue(self, token: str, **fields) -> tuple[Reply, str]:
        """Exchange the token with a fresh key; returns the reply and the key the cluster would hold."""
        body, key = install(**fields)
        return self.exchange(token, body), key


def new_key(env: str = "e2e") -> str:
    return f"impact_{env}_{secrets.token_urlsafe(32)}"


def install(
    *,
    product: str = "prod_e2e",
    uid: str = UID,
    arn: str | None = None,
    name: str | None = None,
    env: str = "e2e",
    version: str = "0.3.0",
    key: str | None = None,
) -> tuple[dict, str]:
    """A valid exchange body and the key whose hash it carries."""
    key = key or new_key(env)
    body = {
        "schema": "impact.install.v1",
        "productId": product,
        "cluster": {"uid": uid, "arn": arn, "name": name},
        "key": {"lookupHash": hashlib.sha256(key.encode()).hexdigest(), "displayPrefix": key[: len(env) + 8 + 8]},
        "chart": {"version": version},
    }
    return body, key


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot(uid: str = UID) -> dict:
    return {"schema": "impact.inventory.v1", "cluster": {"uid": uid}, "workloads": []}


@pytest.fixture(scope="module")
def _server():
    spec = importlib.util.spec_from_file_location("mock_ingest_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    mock = Mock(httpd.server_address[1])
    mock.module = module  # for the tests that look at what the mock makes of a value HTTP cannot carry
    yield mock
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def mock(_server: Mock):
    _server.reset()
    yield _server
    _server.reset()


def refusal(reply: Reply, code: str) -> None:
    status, message = REFUSALS[code]
    assert (reply.status, reply.json) == (status, {"code": code, "message": message}), reply.text
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["content-type"].startswith("application/json")


# --- the token the control endpoint mints ---------------------------------------------------------------------------


def test_a_minted_token_has_the_install_token_shape_and_a_lifetime(mock):
    before = time.time()
    minted = mock.mint(env="e2e", product="prod_e2e", expires_in=900)
    assert TOKEN_SHAPE.match(minted["token"])
    assert re.fullmatch(r"itk_[0-9a-f]{24}", minted["id"])
    assert before + 899 <= minted["expires_at"] <= time.time() + 901
    assert mock.mint(env="other-env1")["token"].startswith("impactinstall_other-env1_")
    assert mock.mint()["token"].startswith("impactinstall_e2e_"), "the environment defaults to e2e"
    assert minted["token"] != mock.mint()["token"]


@pytest.mark.parametrize("fields", [{"env": "Bad_Env"}, {"env": ""}, {"state": "frozen"}, {"expires_in": "soon"}])
def test_a_token_that_cannot_be_minted_is_refused(mock, fields):
    assert mock.post("/_tokens", fields).status == 400


def test_the_listing_never_shows_the_token_or_a_key(mock):
    minted = mock.mint(product="prod_x")
    body, key = install(product="prod_x")
    assert mock.exchange(minted["token"], body).status == 200
    listing = mock.get("/_installs")
    assert minted["token"] not in listing.text and key not in listing.text
    (token,) = mock.installs()["tokens"]
    assert token["id"] == minted["id"] and token["product"] == "prod_x" and token["state"] == "used"
    assert token["consumed_key_hash"] == sha256(key)
    assert token["expires_at"] == minted["expires_at"]


# --- a good exchange ------------------------------------------------------------------------------------------------


def test_an_exchange_issues_a_key_from_its_hash_and_consumes_the_token(mock):
    minted = mock.mint(auto_cover=False, cluster_name="prod-eks")
    body, key = install(arn=ARN, name="kind-e2e", version="0.3.1")
    reply = mock.exchange(minted["token"], body)
    assert reply.status == 200, reply.text
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["content-type"].startswith("application/json")
    answer = reply.json
    assert set(answer) == {"installId", "keyId", "productId", "autoCover", "clusterName", "replayed"}
    assert re.fullmatch(r"ins_[0-9a-f]+", answer["installId"]) and re.fullmatch(r"pk_[0-9a-f]+", answer["keyId"])
    assert answer["productId"] == "prod_e2e"
    assert answer["autoCover"] is False and answer["clusterName"] == "prod-eks" and answer["replayed"] is False
    assert key not in reply.text

    (issued,) = mock.installs()["keys"]
    assert issued["key_id"] == answer["keyId"] and issued["install_id"] == answer["installId"]
    assert issued["product"] == "prod_e2e" and issued["cluster_uid"] == UID
    assert issued["cluster_arn"] == ARN and issued["cluster_name"] == "kind-e2e"
    assert issued["lookup_hash"] == sha256(key) and issued["display_prefix"] == body["key"]["displayPrefix"]
    assert issued["chart_version"] == "0.3.1" and issued["revoked"] is False


def test_the_defaults_of_a_token_are_automatic_coverage_and_no_cluster_name(mock):
    minted = mock.mint()
    reply, _ = mock.issue(minted["token"])
    assert reply.json["autoCover"] is True and reply.json["clusterName"] is None


def test_a_second_exchange_with_the_same_hash_is_a_replay_of_the_first(mock):
    minted = mock.mint()
    body, _ = install()
    first = mock.exchange(minted["token"], body).json
    second = mock.exchange(minted["token"], body)
    assert second.status == 200 and second.headers["cache-control"] == "no-store"
    assert second.json == {**first, "replayed": True}
    assert len(mock.installs()["keys"]) == 1
    assert [e["kind"] for e in mock.events()] == ["issued", "exchanged", "replayed"]


def test_a_replay_still_works_after_the_token_expired(mock):
    minted = mock.mint(expires_in=0.25)
    body, _ = install()
    assert mock.exchange(minted["token"], body).status == 200
    time.sleep(0.3)
    replay = mock.exchange(minted["token"], body)
    assert replay.status == 200 and replay.json["replayed"] is True


def test_another_key_for_a_used_token_is_refused_and_mints_nothing(mock):
    minted = mock.mint()
    assert mock.issue(minted["token"])[0].status == 200
    refusal(mock.issue(minted["token"])[0], "token_used")
    assert len(mock.installs()["keys"]) == 1


# --- the refusals, one by one ---------------------------------------------------------------------------------------


def test_every_refusal_has_its_code_its_fixed_sentence_and_echoes_nothing(mock):
    product, uid, name = "prod_secretive", "uid-secretive-0001", "cluster-secretive"
    tokens = {
        "token_revoked": mock.mint(product=product, state="revoked"),
        "token_used": mock.mint(product=product, state="used"),
        "token_expired": mock.mint(product=product, expires_in=-1),
        "token_wrong_product": mock.mint(product="prod_other"),
        "token_wrong_cluster": mock.mint(product=product, bind={"uid": "uid-elsewhere-0002"}),
    }
    for code, minted in tokens.items():
        body, key = install(product=product, uid=uid, name=name)
        reply = mock.exchange(minted["token"], body)
        refusal(reply, code)
        for secret in (minted["token"], product, uid, name, key, sha256(key), "prod_other"):
            assert secret not in reply.text, (code, secret)
    body, _ = install(product=product, uid=uid, name=name)
    refusal(mock.exchange(new_token_shape(), body), "token_unknown")
    refusal(mock.exchange(None, body), "token_unknown")
    refusal(mock.exchange(tokens["token_used"]["token"], {"schema": "nope"}), "malformed_request")
    assert mock.installs()["keys"] == [], "a refusal never mints a key"


def new_token_shape(env: str = "e2e") -> str:
    return f"impactinstall_{env}_{secrets.token_urlsafe(32)}"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "bearer-without-space",
        "Token abc",
    ],
)
def test_a_missing_or_foreign_authorization_header_is_an_unknown_token(mock, header):
    body, _ = install()
    sent = {"Content-Type": "application/json"}
    if header is not None:
        sent["Authorization"] = header
    refusal(mock.request("POST", EXCHANGE, body, sent), "token_unknown")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("impactinstall_", "impact_", 1),  # a product key is not an install token
        lambda t: t.replace("impactinstall_e2e_", "impactinstall_E2E_", 1),  # the environment is lower case
        lambda t: t[:-1],  # 42 characters of secret
        lambda t: t + "x",  # 44 characters of secret
        lambda t: t[:-1] + "!",
        lambda t: t.replace("impactinstall_e2e_", "impactinstall__", 1),  # no environment
        lambda t: " " + t,
        lambda t: t.upper(),
    ],
)
def test_a_token_of_the_wrong_shape_is_unknown_and_writes_nothing(mock, mutate):
    minted = mock.mint()
    body, _ = install()
    refusal(mock.exchange(mutate(minted["token"]), body), "token_unknown")
    assert [e["kind"] for e in mock.events()] == ["issued"], "only the mint is on record"


def test_a_well_formed_token_nobody_minted_is_unknown_and_writes_nothing(mock):
    body, _ = install()
    refusal(mock.exchange(new_token_shape(), body), "token_unknown")
    assert mock.events() == []


def test_a_revoked_token_is_cancelled_and_the_refusal_is_on_record(mock):
    minted = mock.mint()
    assert mock.post("/_tokens/revoke", {"id": minted["id"]}).json == {"revoked": True}
    refusal(mock.issue(minted["token"])[0], "token_revoked")
    kinds = [(e["kind"], e["code"]) for e in mock.events()]
    assert kinds == [("issued", None), ("revoked", None), ("refused", "token_revoked")]


def test_revoking_a_used_token_changes_nothing_a_consumed_token_stays_consumed(mock):
    minted = mock.mint()
    body, _ = install()
    assert mock.exchange(minted["token"], body).status == 200
    assert mock.post("/_tokens/revoke", {"id": minted["id"]}).json == {"revoked": False}
    assert mock.exchange(minted["token"], body).json["replayed"] is True  # the same install, still
    refusal(mock.issue(minted["token"])[0], "token_used")  # and no other key
    assert mock.post("/_tokens/revoke", {"id": minted["id"]}).status == 200  # idempotent
    assert mock.post("/_tokens/revoke", {"id": "itk_" + "0" * 24}).status == 404


def test_the_server_clock_decides_expiry_at_the_instant_itself(mock):
    refusal(mock.issue(mock.mint(expires_in=0)["token"])[0], "token_expired")
    refusal(mock.issue(mock.mint(expires_in=-60)["token"])[0], "token_expired")
    assert mock.issue(mock.mint(expires_in=60)["token"])[0].status == 200


def test_another_products_token_is_refused_and_not_consumed(mock):
    minted = mock.mint(product="prod_a")
    refusal(mock.issue(minted["token"], product="prod_b")[0], "token_wrong_product")
    assert mock.issue(minted["token"], product="prod_a")[0].status == 200


@pytest.mark.parametrize(
    ("bind", "presented", "ok"),
    [
        ({"uid": UID}, {"uid": UID}, True),
        ({"uid": UID}, {"uid": "11111111-2222-3333-4444-555555555555"}, False),
        ({"arn": ARN}, {"arn": ARN}, True),
        ({"arn": ARN}, {"arn": ARN + "-other"}, False),
        ({"arn": ARN}, {"arn": None}, False),  # a bound value that is missing is a mismatch
        ({"uid": UID, "arn": ARN}, {"uid": UID, "arn": ARN}, True),
        ({"uid": UID, "arn": ARN}, {"uid": UID, "arn": "arn:aws:eks:eu-west-1:123456789012:cluster/else"}, False),
        ({"uid": UID, "arn": ARN}, {"uid": "11111111-2222-3333-4444-555555555555", "arn": ARN}, False),
        ({}, {"uid": "any-cluster-0001", "arn": None}, True),  # an unbound token takes what the chart presents
        ({"uid": None, "arn": None}, {"uid": UID, "arn": ARN}, True),
    ],
)
def test_a_bound_token_wants_every_bound_value_to_match(mock, bind, presented, ok):
    minted = mock.mint(bind=bind)
    reply, _ = mock.issue(minted["token"], **presented)
    if ok:
        assert reply.status == 200, reply.text
    else:
        refusal(reply, "token_wrong_cluster")
        assert mock.issue(minted["token"], uid=bind.get("uid") or UID, arn=bind.get("arn"))[0].status == 200


def test_an_unbound_exchange_records_what_the_chart_presented(mock):
    mock.issue(mock.mint()["token"], uid="uid-from-the-chart-1", arn=ARN, name="from-the-chart")
    (issued,) = mock.installs()["keys"]
    assert (issued["cluster_uid"], issued["cluster_arn"], issued["cluster_name"]) == (
        "uid-from-the-chart-1",
        ARN,
        "from-the-chart",
    )


# --- the order the checks run in ------------------------------------------------------------------------------------


def test_a_bad_body_beats_a_bad_token(mock):
    refusal(mock.exchange(new_token_shape(), {"schema": "impact.install.v1"}), "malformed_request")
    refusal(mock.exchange("not-a-token", b"not json"), "malformed_request")
    refusal(mock.exchange(None, b""), "malformed_request")


def test_the_checks_after_the_body_run_in_the_order_of_the_table(mock):
    # revoked before used: a token is only ever one of them, so the order shows through what a refusal leaves behind
    used_and_expired = mock.mint(state="used", expires_in=-5)
    refusal(mock.issue(used_and_expired["token"])[0], "token_used")  # used beats expired
    expired_and_foreign = mock.mint(expires_in=-5, product="prod_a", bind={"uid": "elsewhere-uid-0001"})
    refusal(mock.issue(expired_and_foreign["token"], product="prod_b")[0], "token_expired")  # expired beats product
    foreign_and_elsewhere = mock.mint(product="prod_a", bind={"uid": "elsewhere-uid-0001"})
    refusal(mock.issue(foreign_and_elsewhere["token"], product="prod_b")[0], "token_wrong_product")  # product first
    revoked = mock.mint(state="revoked", expires_in=-5, product="prod_a")
    refusal(mock.issue(revoked["token"], product="prod_b")[0], "token_revoked")  # revoked beats the rest


# --- the body is strict ---------------------------------------------------------------------------------------------


def _broken(change) -> dict:
    body, _ = install(arn=ARN, name="kind-e2e")
    change(body)
    return body


BAD_BODIES = {
    "wrong schema": lambda b: b.update(schema="impact.install.v2"),
    "no schema": lambda b: b.pop("schema"),
    "no product": lambda b: b.pop("productId"),
    "product not a string": lambda b: b.update(productId=7),
    "empty product": lambda b: b.update(productId=""),
    "no cluster": lambda b: b.pop("cluster"),
    "cluster not an object": lambda b: b.update(cluster=UID),
    "no uid": lambda b: b["cluster"].pop("uid"),
    "empty uid": lambda b: b["cluster"].update(uid=""),
    "uid not a string": lambda b: b["cluster"].update(uid=1234),
    "arn not a string": lambda b: b["cluster"].update(arn=5),
    "name not a string": lambda b: b["cluster"].update(name=["x"]),
    "unknown cluster field": lambda b: b["cluster"].update(region="eu-west-1"),
    "no key": lambda b: b.pop("key"),
    "key not an object": lambda b: b.update(key="abc"),
    "no hash": lambda b: b["key"].pop("lookupHash"),
    "hash too short": lambda b: b["key"].update(lookupHash="a" * 63),
    "hash too long": lambda b: b["key"].update(lookupHash="a" * 65),
    "hash not hex": lambda b: b["key"].update(lookupHash="g" * 64),
    "hash upper case": lambda b: b["key"].update(lookupHash="A" * 64),
    "hash not a string": lambda b: b["key"].update(lookupHash=12),
    "no prefix": lambda b: b["key"].pop("displayPrefix"),
    "prefix of another environment": lambda b: b["key"].update(displayPrefix="impact_prod_abcdefgh"),
    "prefix is not a key prefix": lambda b: b["key"].update(displayPrefix="notakey_e2e_abcdefgh"),
    "prefix one character short": lambda b: b["key"].update(displayPrefix="impact_e2e_abcdefg"),
    "prefix one character long": lambda b: b["key"].update(displayPrefix="impact_e2e_abcdefghi"),
    "prefix with a bad character": lambda b: b["key"].update(displayPrefix="impact_e2e_abcd!fgh"),
    "unknown key field": lambda b: b["key"].update(note="x"),
    "no chart": lambda b: b.pop("chart"),
    "chart not an object": lambda b: b.update(chart="0.3.0"),
    "no version": lambda b: b["chart"].pop("version"),
    "version not a string": lambda b: b["chart"].update(version=3),
    "unknown chart field": lambda b: b["chart"].update(appVersion="1"),
    "unknown top level field": lambda b: b.update(extra=True),
}


@pytest.mark.parametrize("case", BAD_BODIES)
def test_a_body_that_does_not_fit_the_schema_is_malformed_and_spends_nothing(mock, case):
    minted = mock.mint()
    refusal(mock.exchange(minted["token"], _broken(BAD_BODIES[case])), "malformed_request")
    assert mock.issue(minted["token"], arn=ARN)[0].status == 200, "the token is intact"
    assert "refused" not in [e["kind"] for e in mock.events()], "a malformed request is not on record"


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b'"text"', b"123", b'{"schema":'])
def test_a_body_that_is_not_a_json_object_is_malformed(mock, raw):
    refusal(mock.exchange(mock.mint()["token"], raw), "malformed_request")


def test_the_body_is_limited_to_4096_bytes(mock):
    minted = mock.mint()

    def padded(size: int) -> bytes:
        body, _ = install(name="x" * 10)
        compact = json.dumps(body, separators=(",", ":")).encode()
        return compact + b" " * (size - len(compact))

    refusal(mock.exchange(minted["token"], padded(4097)), "malformed_request")
    assert mock.exchange(minted["token"], padded(4096)).status == 200


def test_a_key_hash_that_belongs_to_another_install_is_not_issued_twice(mock):
    body, _ = install()
    assert mock.exchange(mock.mint()["token"], body).status == 200
    refusal(mock.exchange(mock.mint()["token"], body), "malformed_request")
    assert len(mock.installs()["keys"]) == 1


# --- two at once ----------------------------------------------------------------------------------------------------


def _parallel(mock, token: str, bodies: list[dict]) -> list[Reply]:
    barrier = threading.Barrier(len(bodies))
    replies: list[Reply | None] = [None] * len(bodies)

    def run(index: int) -> None:
        barrier.wait()
        replies[index] = mock.exchange(token, bodies[index])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(bodies))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(replies)
    return replies  # type: ignore[return-value]


def test_parallel_exchanges_with_different_keys_have_one_winner(mock):
    minted = mock.mint()
    replies = _parallel(mock, minted["token"], [install()[0] for _ in range(8)])
    winners = [r for r in replies if r.status == 200]
    assert len(winners) == 1 and winners[0].json["replayed"] is False
    for loser in (r for r in replies if r.status != 200):
        refusal(loser, "token_used")
    assert len(mock.installs()["keys"]) == 1
    assert [e["kind"] for e in mock.events()].count("exchanged") == 1


def test_parallel_exchanges_with_the_same_key_make_one_install_and_the_rest_replays(mock):
    minted = mock.mint()
    body, _ = install()
    replies = _parallel(mock, minted["token"], [body] * 8)
    assert [r.status for r in replies] == [200] * 8
    assert sorted(r.json["replayed"] for r in replies) == [False] + [True] * 7
    assert len({r.json["keyId"] for r in replies}) == 1 and len(mock.installs()["keys"]) == 1


# --- the audit trail ------------------------------------------------------------------------------------------------


def test_every_mint_exchange_refusal_and_revocation_leaves_an_event(mock):
    minted = mock.mint(product="prod_e2e")
    refused = mock.mint(product="prod_e2e", state="used")
    other = mock.mint(product="prod_e2e")
    body, _ = install(uid="uid-for-the-events-1")
    exchanged = mock.exchange(minted["token"], body).json
    mock.exchange(minted["token"], body)  # replay
    mock.exchange(refused["token"], install(uid="uid-for-the-events-2")[0])  # refused: used
    mock.post("/_tokens/revoke", {"id": other["id"]})
    events = mock.events()
    assert [(e["kind"], e["code"]) for e in events] == [
        ("issued", None),
        ("issued", None),
        ("issued", None),
        ("exchanged", None),
        ("replayed", None),
        ("refused", "token_used"),
        ("revoked", None),
    ]
    assert [e["seq"] for e in events] == sorted({e["seq"] for e in events}) and events[0]["seq"] >= 1
    assert all(set(e) == {"seq", "at", "kind", "code", "token_id", "key_id", "product", "cluster_uid"} for e in events)
    assert all(isinstance(e["at"], float) and abs(e["at"] - time.time()) < 60 for e in events)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["exchanged"]["token_id"] == minted["id"] and by_kind["exchanged"]["key_id"] == exchanged["keyId"]
    assert by_kind["exchanged"]["cluster_uid"] == "uid-for-the-events-1"
    assert by_kind["exchanged"]["product"] == "prod_e2e"
    assert by_kind["replayed"]["key_id"] == exchanged["keyId"]
    assert by_kind["refused"]["token_id"] == refused["id"] and by_kind["refused"]["key_id"] is None
    assert by_kind["refused"]["cluster_uid"] == "uid-for-the-events-2"
    assert by_kind["revoked"]["token_id"] == other["id"]


def test_a_malformed_request_and_an_unknown_token_write_no_event(mock):
    mock.exchange(new_token_shape(), install()[0])
    mock.exchange(mock.mint()["token"], {"schema": "impact.install.v1"})
    assert [e["kind"] for e in mock.events()] == ["issued"]


# --- rules and lost responses ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "code"), [(503, "unavailable"), (429, "rate_limited")])
def test_a_rule_makes_the_exchange_unavailable_or_rate_limited_with_the_token_intact(mock, status, code):
    minted = mock.mint()
    mock.control(rules=[{"path": EXCHANGE, "status": status, "retry_after": 1, "times": 2}])
    for _ in range(2):
        reply, _ = mock.issue(minted["token"])
        refusal(reply, code)
        assert reply.headers["retry-after"] == "1"
    assert mock.installs()["keys"] == [] and [e["kind"] for e in mock.events()] == ["issued"]
    reply, _ = mock.issue(minted["token"])
    assert reply.status == 200, "the rule ran out and the token was intact"


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_defaults_to_five_seconds(mock, status):
    mock.control(rules=[{"path": EXCHANGE, "status": status}])
    assert mock.issue(mock.mint()["token"])[0].headers["retry-after"] == "5"


def test_a_rule_beats_the_token_logic_even_for_a_bad_token(mock):
    mock.control(rules=[{"path": EXCHANGE, "status": 503, "times": 1}])
    refusal(mock.exchange("not-a-token", b"not json"), "unavailable")
    refusal(mock.exchange("not-a-token", b"not json"), "malformed_request")


def test_a_rule_with_only_a_delay_slows_the_exchange_and_then_answers_it(mock):
    minted = mock.mint()
    mock.control(rules=[{"path": EXCHANGE, "delay": 0.2, "times": 1}])
    started = time.monotonic()
    reply, _ = mock.issue(minted["token"])
    assert reply.status == 200 and time.monotonic() - started >= 0.2


def test_a_lost_response_still_spends_the_token_and_the_retry_is_a_replay(mock):
    minted = mock.mint()
    body, _ = install()
    mock.control(rules=[{"path": EXCHANGE, "drop": True, "times": 1}])
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        mock.exchange(minted["token"], body)
    (token,) = mock.installs()["tokens"]
    assert token["state"] == "used", "the exchange happened; only the answer was lost"
    assert len(mock.installs()["keys"]) == 1
    retry = mock.exchange(minted["token"], body)
    assert retry.status == 200 and retry.json["replayed"] is True
    assert [e["kind"] for e in mock.events()] == ["issued", "exchanged", "replayed"]
    # And another key cannot take its place.
    refusal(mock.issue(minted["token"])[0], "token_used")


def test_a_lost_response_works_on_any_path_and_is_recorded_as_processed(mock):
    mock.control(rules=[{"path": "/v1/inventory", "drop": True, "times": 1}])
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        mock.post("/v1/inventory", snapshot())
    assert mock.post("/v1/inventory", snapshot()).status == 202
    assert [r["status"] for r in mock.requests("/v1/inventory")] == [202, 202]


# --- the keys the exchange issues -----------------------------------------------------------------------------------


def issued_key(mock, **fields) -> tuple[str, dict]:
    reply, key = mock.issue(mock.mint()["token"], **fields)
    assert reply.status == 200, reply.text
    return key, reply.json


def test_an_issued_key_is_admitted_on_inventory_traces_and_the_cover_list(mock):
    key, _ = issued_key(mock)
    mock.control(require_key="the-key-the-tests-installed-with")
    assert mock.post("/v1/inventory", snapshot(), bearer=key).status == 202
    assert mock.post("/v1/traces", {}, bearer=key).status == 200
    assert mock.get(COVER, {"Authorization": f"Bearer {key}"}).status == 200
    # the key require_key names still works, and nothing else does
    assert mock.post("/v1/inventory", snapshot(), bearer="the-key-the-tests-installed-with").status == 202
    for bearer in ("a-key-nobody-issued", ""):
        assert mock.post("/v1/inventory", snapshot(), bearer=bearer).status == 401
        assert mock.post("/v1/traces", {}, bearer=bearer).status == 401
        assert mock.get(COVER, {"Authorization": f"Bearer {bearer}"}).status == 401
    assert mock.post("/v1/inventory", snapshot()).status == 401
    assert mock.get(COVER).status == 401


def test_a_revoked_key_is_refused_everywhere_and_the_others_are_not(mock):
    first, first_answer = issued_key(mock)
    second, _ = issued_key(mock, uid="uid-of-the-second-1")
    assert mock.post("/_keys/revoke", {"key_id": first_answer["keyId"]}).status == 200
    assert mock.post("/v1/inventory", snapshot(), bearer=first).status == 401
    assert mock.post("/v1/traces", {}, bearer=first).status == 401
    assert mock.get(COVER, {"Authorization": f"Bearer {first}"}).status == 401
    assert mock.post("/v1/inventory", snapshot("uid-of-the-second-1"), bearer=second).status == 202
    assert [k["revoked"] for k in mock.installs()["keys"]] == [True, False]
    assert mock.post("/_keys/revoke", {"all": True}).status == 200
    assert mock.post("/v1/traces", {}, bearer=second).status == 401
    assert [k["revoked"] for k in mock.installs()["keys"]] == [True, True]
    assert mock.post("/_keys/revoke", {"key_id": "pk_" + "0" * 24}).status == 404


def test_revoking_all_keys_when_there_are_none_is_harmless_and_a_bad_request_is_refused(mock):
    reply = mock.post("/_keys/revoke", {"all": True})
    assert (reply.status, reply.json) == (200, {"revoked": 0})
    assert mock.post("/_keys/revoke", {}).status == 400
    assert mock.post("/_keys/revoke", {"all": False}).status == 400
    assert mock.post("/_keys/revoke", b"not json").status == 400
    _, answer = issued_key(mock)
    assert mock.post("/_keys/revoke", {"key_id": answer["keyId"]}).json == {"revoked": 1}
    assert mock.post("/_keys/revoke", {"all": True}).json == {"revoked": 0}, "only keys still in force count"


def test_without_require_key_the_mock_is_open_until_the_first_key_is_issued(mock):
    assert mock.post("/v1/inventory", snapshot()).status == 202  # the container tests rely on this
    assert mock.post("/v1/traces", {}, bearer="whatever").status == 200
    assert mock.get(COVER).status == 401, "the cover route serves an install's key and nobody else, open mode or not"
    key, _ = issued_key(mock)
    assert mock.post("/v1/traces", {}, bearer="whatever").status == 401
    assert mock.post("/v1/traces", {}).status == 401
    assert mock.post("/v1/traces", {}, bearer=key).status == 200
    mock.reset()
    assert mock.post("/v1/traces", {}, bearer="whatever").status == 200, "a reset forgets the keys"


def test_a_key_is_held_to_the_cluster_it_was_issued_for(mock):
    key, _ = issued_key(mock, uid="uid-of-the-cluster-1")
    assert mock.post("/v1/inventory", snapshot("uid-of-the-cluster-1"), bearer=key).status == 202
    wrong = mock.post("/v1/inventory", snapshot("uid-of-another-cluster"), bearer=key)
    assert (wrong.status, wrong.json) == (403, {"code": "key_cluster_mismatch"})
    # the check reads gzip bodies like the reporter sends them
    zipped = mock.post(
        "/v1/inventory",
        gzip.compress(json.dumps(snapshot("uid-of-another-cluster")).encode()),
        bearer=key,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert zipped.status == 403
    assert mock.post("/v1/traces", {}, bearer=key).status == 200, "only snapshots carry a cluster"
    assert [r["status"] for r in mock.requests("/v1/inventory")] == [202, 403, 403]


def test_the_key_require_key_names_has_no_cluster(mock):
    mock.control(require_key="legacy-key-without-cluster")
    issued_key(mock, uid="uid-of-the-cluster-1")
    assert mock.post("/v1/inventory", snapshot("any-other-cluster"), bearer="legacy-key-without-cluster").status == 202


# --- the snapshot's stall (why a workload's pods do not start) --------------------------------------------------------


def workload(**extra) -> dict:
    """A workload of a snapshot as the reporter writes it, with these members added."""
    return {
        "namespace": "shop",
        "kind": "Deployment",
        "name": "api",
        "pods": {"desired": 2, "ready": 1},
        "serviceAccount": "api",
        "annotations": {},
        "containers": [],
        **extra,
    }


def with_workloads(*workloads: dict) -> dict:
    return {**snapshot(), "workloads": list(workloads)}


@pytest.mark.parametrize(
    "stall",
    [
        {"replicaFailure": "FailedCreate"},
        {"initWaiting": ["ImagePullBackOff"]},
        {"replicaFailure": "FailedCreate", "initWaiting": ["ErrImagePull", "ImagePullBackOff"]},
        {"initWaiting": [f"Reason{i:02d}" for i in range(8)]},
        {"replicaFailure": "A" + "b" * 62},
        {"replicaFailure": "Unknown"},
    ],
)
def test_a_stall_the_contract_describes_is_accepted_and_recorded(mock, stall):
    reply = mock.post("/v1/inventory", with_workloads(workload(stall=stall)))
    assert reply.status == 202, reply.text
    (recorded,) = mock.requests("/v1/inventory")
    assert recorded["json"]["workloads"][0]["stall"] == stall


@pytest.mark.parametrize(
    "stall",
    [
        {},
        "FailedCreate",
        ["FailedCreate"],
        {"replicaFailure": ""},
        {"replicaFailure": None},
        {"replicaFailure": ["FailedCreate"]},
        {"replicaFailure": "failedCreate"},
        {"replicaFailure": "Failed-Create"},
        {"replicaFailure": "Failed Create"},
        {"replicaFailure": "F"},
        {"replicaFailure": "A" + "b" * 63},
        {"replicaFailure": "FailedCreate\n"},
        {"replicaFailure": "Sk" + "x" * 5 + "-" + "a" * 30},
        {"initWaiting": []},
        {"initWaiting": "ImagePullBackOff"},
        {"initWaiting": [1]},
        {"initWaiting": [None]},
        {"initWaiting": [f"Reason{i:02d}" for i in range(9)]},
        {"initWaiting": ["ErrImagePull", "ErrImagePull"]},
        {"initWaiting": ["image pull backoff"]},
        {"initWaiting": ["ImagePullBackOff", ""]},
        {"replicaFailure": "FailedCreate", "message": "pods is forbidden"},
        {"initWaiting": ["ErrImagePull"], "reason": "x"},
    ],
    ids=repr,
)
def test_a_stall_that_is_not_names_alone_refuses_the_whole_snapshot(mock, stall):
    reply = mock.post("/v1/inventory", with_workloads(workload(), workload(name="web", stall=stall)))
    assert (reply.status, reply.json) == (400, {"code": "malformed_request"}), reply.text
    assert [r["status"] for r in mock.requests("/v1/inventory")] == [400], "recorded, and refused"


def test_a_member_of_a_workload_the_intake_does_not_know_refuses_the_whole_snapshot(mock):
    # The real intake reads exactly the contract: a snapshot with a member it has not been taught is refused whole, so a
    # kit that sends a new field before the intake reads it costs the cluster its inventory until the field is gone.
    reply = mock.post("/v1/inventory", with_workloads(workload(coverStall={"replicaFailure": "FailedCreate"})))
    assert (reply.status, reply.json) == (400, {"code": "malformed_request"})
    # Every member the reporter writes today is known.
    members = workload(
        labels={}, namespaceLabels={}, autoCovered="2026-11-03T11:00:00Z", stall={"initWaiting": ["ErrImagePull"]}
    )
    assert mock.post("/v1/inventory", with_workloads(members)).status == 202
    assert mock.post("/v1/inventory", with_workloads("not a workload")).status == 400


def test_a_malformed_snapshot_is_refused_before_the_cluster_it_belongs_to_is_looked_at(mock):
    key, _ = issued_key(mock, uid="uid-of-the-cluster-1")
    bad = {**with_workloads(workload(stall={})), "cluster": {"uid": "uid-of-another-cluster"}}
    assert mock.post("/v1/inventory", bad, bearer=key).status == 400, "the body is read before it is stored"
    good = {
        **with_workloads(workload(stall={"replicaFailure": "FailedCreate"})),
        "cluster": {"uid": "uid-of-another-cluster"},
    }
    assert mock.post("/v1/inventory", good, bearer=key).status == 403


def test_a_stall_is_read_in_a_gzip_snapshot_like_the_reporter_sends_it(mock):
    body = gzip.compress(json.dumps(with_workloads(workload(stall={"replicaFailure": "bad reason"}))).encode())
    reply = mock.post("/v1/inventory", body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert reply.status == 400
    body = gzip.compress(json.dumps(with_workloads(workload(stall={"replicaFailure": "FailedCreate"}))).encode())
    reply = mock.post("/v1/inventory", body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert reply.status == 202


REPORTER_TESTDATA = Path(__file__).resolve().parents[2] / "reporter" / "testdata"


@pytest.mark.parametrize("name", ["golden-snapshot.json", "golden-snapshot-cover.json"])
def test_the_snapshots_the_reporter_writes_are_what_the_intake_reads(mock, name):
    # The golden files of the reporter are what it sends: the plain snapshot, and the one of an install with automatic
    # coverage, which names why the pods of two workloads do not start. Both are accepted, and the stall arrives whole.
    document = json.loads((REPORTER_TESTDATA / name).read_text())
    assert mock.post("/v1/inventory", document).status == 202
    (recorded,) = mock.requests("/v1/inventory")
    stalls = {w["name"]: w["stall"] for w in recorded["json"]["workloads"] if "stall" in w}
    if name == "golden-snapshot.json":
        assert stalls == {}, "a server that does not know the field never sees it"
    else:
        assert stalls == {
            "billing-api": {"replicaFailure": "FailedCreate"},
            "search-assistant": {"initWaiting": ["ErrImagePull", "ImagePullBackOff"]},
        }


def test_a_stall_that_is_null_reads_as_none_like_the_other_additive_fields(mock):
    assert mock.post("/v1/inventory", with_workloads(workload(stall=None))).status == 202


def test_a_snapshot_without_workloads_to_read_is_answered_as_it_always_was(mock):
    for body in ({"schema": "impact.inventory.v1", "cluster": {"uid": UID}}, {"workloads": None}, snapshot()):
        assert mock.post("/v1/inventory", body).status == 202


# --- the cover list (C11) -------------------------------------------------------------------------------------------


def cover(mock, key: str, etag: str | None = None) -> Reply:
    headers = {"Authorization": f"Bearer {key}"}
    if etag is not None:
        headers["If-None-Match"] = etag
    return mock.get(COVER, headers)


WORKLOAD = {
    "namespace": "shop",
    "kind": "Deployment",
    "name": "api",
    "lang": "python",
    "container": "app",
    "musl": False,
}


def test_the_cover_list_starts_empty_and_automatic_at_version_one(mock):
    key, _ = issued_key(mock)
    reply = cover(mock, key)
    assert reply.status == 200
    assert reply.json == {"schema": "impact.cover.v1", "version": 1, "automatic": True, "workloads": []}
    assert re.fullmatch(r'"[0-9a-f]{24}"', reply.headers["etag"]), "an opaque name, as the real route makes one"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["content-type"].startswith("application/json")


def test_the_cover_list_is_replaced_and_versioned(mock):
    key, _ = issued_key(mock)
    before_etag = cover(mock, key).headers["etag"]
    assert mock.post("/_cover", {"automatic": True, "workloads": [WORKLOAD]}).status == 200
    reply = cover(mock, key)
    assert reply.json == {"schema": "impact.cover.v1", "version": 2, "automatic": True, "workloads": [WORKLOAD]}
    assert reply.headers["etag"] != before_etag, "the name moves with the list"
    assert mock.get("/_cover").json == reply.json
    mock.post("/_cover", {"automatic": False})  # a full replacement: no workloads, automatic coverage off
    assert cover(mock, key).json == {"schema": "impact.cover.v1", "version": 3, "automatic": False, "workloads": []}
    mock.post("/_cover", {})
    assert cover(mock, key).json["automatic"] is True, "automatic defaults to on"


def test_if_none_match_answers_304_until_the_list_changes(mock):
    key, _ = issued_key(mock)
    first = cover(mock, key)
    etag = first.headers["etag"]
    unchanged = cover(mock, key, etag)
    assert unchanged.status == 304 and unchanged.raw == b""
    assert unchanged.headers["etag"] == etag and unchanged.headers["cache-control"] == "no-store"
    assert cover(mock, key, f"W/{etag}").status == 304
    assert cover(mock, key, f'"nothing", {etag}').status == 304
    assert cover(mock, key, "*").status == 304
    mock.post("/_cover", {"workloads": [WORKLOAD]})
    changed = cover(mock, key, etag)
    assert changed.status == 200 and changed.headers["etag"] != etag and changed.json["workloads"] == [WORKLOAD]
    assert cover(mock, key, changed.headers["etag"]).status == 304
    assert cover(mock, key, '"someone-else"').status == 200
    mock.post("/_cover", {"automatic": False})  # the setting moves the name too, as it does at the real route
    assert cover(mock, key, changed.headers["etag"]).status == 200


@pytest.mark.parametrize("bad", [{"workloads": "all"}, {"workloads": [1]}, {"automatic": "yes"}, {"workloads": {}}])
def test_a_cover_state_that_is_not_a_list_cannot_be_set(mock, bad):
    assert mock.post("/_cover", bad).status == 400
    assert mock.get("/_cover").json["version"] == 1


def test_the_cover_route_refuses_a_missing_unknown_or_revoked_key_with_a_code(mock):
    key, answer = issued_key(mock)
    assert (mock.get(COVER).status, mock.get(COVER).json) == (401, {"code": "credential_missing"})
    assert (cover(mock, "not-a-key").status, cover(mock, "not-a-key").json) == (401, {"code": "credential_invalid"})
    mock.post("/_keys/revoke", {"key_id": answer["keyId"]})
    assert (cover(mock, key).status, cover(mock, key).json) == (401, {"code": "credential_revoked"})


def test_a_pasted_key_has_no_install_and_the_cover_route_says_so(mock):
    # The key the suite's reporter used before AE-14 (require_key) belongs to no install: nothing to cover for it.
    mock.control(require_key="the-pasted-key")
    reply = cover(mock, "the-pasted-key")
    assert (reply.status, reply.json) == (403, {"code": "not_an_install_key"})


def test_cover_polls_are_recorded_with_their_status_and_rules_apply_to_them(mock):
    key, _ = issued_key(mock)
    mock.control(rules=[{"path": COVER, "status": 503, "retry_after": 2, "times": 1}], require_key=None)
    down = cover(mock, key)
    assert down.status == 503 and down.headers["retry-after"] == "2"
    served = cover(mock, key)
    assert served.status == 200
    assert cover(mock, key, served.headers["etag"]).status == 304
    statuses = [(r["method"], r["status"]) for r in mock.requests(COVER)]
    assert statuses == [("GET", 503), ("GET", 200), ("GET", 304)]
    assert mock.requests(COVER)[1]["headers"]["Authorization"] == f"Bearer {key}"


def test_a_lost_cover_response_is_recorded_and_the_next_poll_answers(mock):
    key, _ = issued_key(mock)
    mock.control(rules=[{"path": COVER, "drop": True, "times": 1}], require_key=None)
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        cover(mock, key)
    assert cover(mock, key).status == 200


# --- what the older tests rely on -----------------------------------------------------------------------------------


def test_require_key_guards_inventory_and_traces_only(mock):
    mock.control(require_key="the-key")
    refused = mock.post("/v1/inventory", snapshot(), bearer="wrong")
    assert (refused.status, refused.json) == (401, {"error": "unauthorized"})
    assert mock.post("/v1/traces", {}, bearer="wrong").status == 401
    assert mock.post("/v1/inventory", snapshot(), bearer="the-key").status == 202
    assert mock.post("/v1/traces", {}, bearer="the-key").status == 200
    # no key needed for the model providers and a customer's own backend
    assert mock.post("/v1/chat/completions", {"model": "m"}).status == 200
    assert mock.post("/v1/some/other/path", {}).status == 200
    assert mock.get("/anything").status == 200


def test_rules_answer_instead_of_the_default_a_limited_number_of_times(mock):
    mock.control(rules=[{"path": "/v1/traces", "status": 503, "retry_after": 1, "times": 2}])
    first, second, third = (mock.post("/v1/traces", {}) for _ in range(3))
    assert (first.status, first.headers["retry-after"]) == (503, "1")
    assert second.status == 503 and third.status == 200
    mock.control(rules=[{"path": "/v1/inventory", "status": 429}])
    assert [mock.post("/v1/inventory", snapshot()).status for _ in range(3)] == [429, 429, 429]
    assert mock.post("/v1/traces", {}).status == 200, "a rule is for its path only"


def test_a_rule_with_a_delay_sleeps_before_it_answers(mock):
    mock.control(rules=[{"path": "/v1/traces", "status": 200, "delay": 0.2, "times": 1}])
    started = time.monotonic()
    assert mock.post("/v1/traces", {}).status == 200
    assert time.monotonic() - started >= 0.2


def test_the_key_check_comes_before_a_rule(mock):
    mock.control(rules=[{"path": "/v1/traces", "status": 503}], require_key="the-key")
    assert mock.post("/v1/traces", {}, bearer="wrong").status == 401
    assert mock.post("/v1/traces", {}, bearer="the-key").status == 503


def test_every_request_is_recorded_in_order_with_its_status_and_body(mock):
    mock.post("/v1/traces", {"resourceSpans": []})
    mock.post("/v1/inventory", snapshot())
    mock.post("/v1/other", {"x": 1})
    records = mock.requests()
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert [(r["method"], r["path"], r["status"]) for r in records] == [
        ("POST", "/v1/traces", 200),
        ("POST", "/v1/inventory", 202),
        ("POST", "/v1/other", 200),
    ]
    assert records[0]["json"] == {"resourceSpans": []}
    assert json.loads(base64.b64decode(records[1]["body_b64"])) == snapshot()
    assert set(records[0]) >= {"seq", "time", "method", "path", "query", "headers", "body_b64", "json", "status"}
    assert [r["seq"] for r in mock.requests("/v1/traces")] == [1]
    assert [r["seq"] for r in mock.requests(since=1)] == [2, 3]


def test_the_exchange_is_recorded_like_any_request_and_control_calls_are_not(mock):
    minted = mock.mint()
    mock.issue(minted["token"])
    mock.post("/_tokens/revoke", {"id": minted["id"]})
    mock.post("/_keys/revoke", {"all": True})
    mock.post("/_cover", {"workloads": [WORKLOAD]})
    mock.get("/_cover")
    mock.get("/_installs")
    assert [(r["method"], r["path"], r["status"]) for r in mock.requests()] == [("POST", EXCHANGE, 200)]


def test_the_model_providers_still_answer(mock):
    chat = mock.post("/v1/chat/completions", {"model": "gpt-4o-mini"})
    assert chat.json["choices"][0]["message"]["content"] == "Hello from the stub."
    converse = mock.post("/model/amazon.nova-micro-v1:0/converse", {})
    assert converse.json["usage"]["totalTokens"] == 12
    assert mock.get("/healthz").json == {"ok": True}


# --- reset ----------------------------------------------------------------------------------------------------------


def test_a_reset_forgets_requests_rules_keys_tokens_events_and_the_cover_list(mock):
    key, _ = issued_key(mock)
    mock.post("/_cover", {"automatic": False, "workloads": [WORKLOAD]})
    mock.control(rules=[{"path": "/v1/traces", "status": 503}], require_key="the-key")
    mock.reset()
    assert mock.requests() == []
    assert mock.installs() == {"events": [], "keys": [], "tokens": []}
    assert mock.get("/_cover").json == {"schema": "impact.cover.v1", "version": 1, "automatic": True, "workloads": []}
    assert mock.post("/v1/traces", {}).status == 200, "no rule and no required key"
    assert mock.get(COVER, {"Authorization": f"Bearer {key}"}).status == 401, "a reset forgets the keys too"
    assert mock.mint()["id"].startswith("itk_")


# --- what the real exchange also does (capabilities/ai-coverage/go/exchange.go) -------------------------------------


def test_a_replay_is_only_for_the_product_the_token_is_for(mock):
    minted = mock.mint()
    body, _ = install()
    assert mock.exchange(minted["token"], body).status == 200
    other = {**body, "productId": "prod_someone_else"}
    refusal(mock.exchange(minted["token"], other), "token_used")


def test_a_key_that_was_already_issued_is_malformed_and_leaves_no_record(mock):
    first, second = mock.mint(), mock.mint()
    body, _ = install()
    assert mock.exchange(first["token"], body).status == 200
    events = len(mock.events())
    refusal(mock.exchange(second["token"], body), "malformed_request")
    assert len(mock.events()) == events, "the real exchange refuses the reused key before it records anything"
    assert len(mock.installs()["keys"]) == 1


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("a product id shorter than three characters", lambda b: b.update(productId="p1")),
        ("a product id with a dot", lambda b: b.update(productId="prod.one")),
        ("a cluster id of three characters", lambda b: b["cluster"].update(uid="abc")),
        ("a cluster id of 129 characters", lambda b: b["cluster"].update(uid="a" * 129)),
        ("an ARN of 513 characters", lambda b: b["cluster"].update(arn="arn:" + "a" * 509)),
        ("an ARN with a space", lambda b: b["cluster"].update(arn="arn:aws:eks:x y")),
        ("a cluster name of 129 characters", lambda b: b["cluster"].update(name="n" * 129)),
        ("an empty cluster name", lambda b: b["cluster"].update(name="")),
        ("a cluster name with a control character", lambda b: b["cluster"].update(name="a\nb")),
        ("a chart version of 65 characters", lambda b: b["chart"].update(version="1" * 65)),
        ("a chart version with a space", lambda b: b["chart"].update(version="1 2")),
    ],
)
def test_the_request_is_read_with_the_grammars_of_the_real_exchange(mock, what, change):
    minted = mock.mint()
    body, _ = install()
    change(body)
    refusal(mock.exchange(minted["token"], body), "malformed_request")
    assert mock.installs()["keys"] == [], what


def test_values_at_the_limits_are_accepted(mock):
    product = "prod_" + "p" * 59
    minted = mock.mint(product=product)
    body, _ = install(
        product=product,
        uid="u" * 128,
        arn="arn:" + "a" * 508,
        name="é" * 128,  # 128 characters, 256 bytes: the limit counts characters
        version="1" * 64,
    )
    reply = mock.exchange(minted["token"], body)
    assert reply.status == 200, reply.text


@pytest.mark.parametrize(
    "headers",
    [{"Content-Type": "text/plain"}, {"Content-Type": ""}, {"Content-Encoding": "gzip"}],
)
def test_the_body_must_be_json_and_uncompressed(mock, headers):
    minted = mock.mint()
    body, _ = install()
    sent = {"Authorization": f"Bearer {minted['token']}", "Content-Type": "application/json", **headers}
    refusal(mock.request("POST", EXCHANGE, body, sent), "malformed_request")


def test_a_content_type_with_parameters_and_an_identity_encoding_are_fine(mock):
    minted = mock.mint()
    body, _ = install()
    sent = {
        "Authorization": f"Bearer {minted['token']}",
        "Content-Type": "application/json; charset=utf-8",
        "Content-Encoding": "identity",
    }
    assert mock.request("POST", EXCHANGE, body, sent).status == 200


# --- the shared vectors of the exchange (testing/mock/exchange-vectors.json) ----------------------------------
#
# A byte-for-byte copy of capabilities/ai-coverage/tests/fixtures/exchange-vectors.json of the monorepo, whose intake
# reads the same file: every way the body of the exchange may be wrong, and the Authorization headers that are no
# install token. The mock must read each of them as the real exchange does. To refresh it, copy the file over.

VECTORS = json.loads((SERVER.parent / "exchange-vectors.json").read_text())


def vector_body(case: dict) -> dict:
    body = json.loads(json.dumps(VECTORS["base"]))
    for step in case["patch"]:
        path = (step.get("set") or step["remove"]).split(".")
        target = body
        for part in path[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        last = int(path[-1]) if isinstance(target, list) else path[-1]
        if "set" in step:
            target[last] = step["value"]
        else:
            del target[last]
    return body


def environment_of(body: dict) -> str:
    prefix = body.get("key", {}).get("displayPrefix") if isinstance(body.get("key"), dict) else None
    match = re.fullmatch(r"impact_([a-z0-9][a-z0-9-]*)_[A-Za-z0-9_-]{8}", prefix) if isinstance(prefix, str) else None
    return match.group(1) if match else VECTORS["token"]["environment"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_the_mock_reads_every_body_of_the_shared_vectors_as_the_real_exchange_does(mock, case):
    body = vector_body(case)
    product = body.get("productId") if isinstance(body.get("productId"), str) else VECTORS["base"]["productId"]
    minted = mock.mint(env=environment_of(body), product=product if case["valid"] else VECTORS["base"]["productId"])
    reply = mock.exchange(minted["token"], body)
    if case["valid"]:
        assert reply.status == 200, f"{case['name']}: {reply.text}"
    else:
        refusal(reply, "malformed_request")
        assert mock.installs()["keys"] == [], "a malformed body issues nothing"


def test_a_key_of_another_environment_than_the_token_is_malformed_though_the_body_is_well_formed(mock):
    # The vectors call the body well-formed; the exchange then holds the key's environment against the token's.
    case = next(c for c in VECTORS["cases"] if c["name"].startswith("a key of another environment"))
    minted = mock.mint(env=VECTORS["token"]["environment"], product=VECTORS["base"]["productId"])
    refusal(mock.exchange(minted["token"], vector_body(case)), "malformed_request")


@pytest.mark.parametrize("parts", VECTORS["token"]["invalid"], ids=lambda parts: "".join(parts)[:40] or "empty")
def test_a_header_that_is_no_install_token_is_unknown_and_never_reaches_a_token(mock, parts):
    mock.mint(env=VECTORS["token"]["environment"], product=VECTORS["base"]["productId"])
    header = "".join(parts)
    if "\n" in header or "\r" in header:
        # HTTP cannot carry it (the client refuses to send it): what the mock makes of the value is what counts.
        found = mock.module.exchange_bearer(header)
        assert found is None or mock.module.TOKEN_GRAMMAR.fullmatch(found) is None
        return
    sent = {"Content-Type": "application/json", **({"Authorization": header} if header else {})}
    refusal(mock.request("POST", EXCHANGE, VECTORS["base"], sent), "token_unknown")
    assert [e["kind"] for e in mock.events()] == ["issued"], "an unknown token leaves no record"


def test_the_reference_token_of_the_vectors_is_read_in_any_case_of_the_scheme(mock):
    token = "impactinstall_" + VECTORS["token"]["environment"] + "_" + VECTORS["token"]["secret"]
    assert VECTORS["token"]["header"] == f"Bearer {token}"
    refusal(mock.exchange(token, VECTORS["base"]), "token_unknown")  # nobody minted it
    minted = mock.mint(env=VECTORS["token"]["environment"], product=VECTORS["base"]["productId"])
    sent = {"Content-Type": "application/json", "Authorization": f"bEaReR {minted['token']}"}
    assert mock.request("POST", EXCHANGE, VECTORS["base"], sent).status == 200


@pytest.mark.parametrize("absent", ["arn", "name", "both"])
def test_the_arn_and_the_name_of_the_cluster_may_be_absent_or_null(mock, absent):
    minted = mock.mint()
    body, _ = install()
    for member in ("arn", "name") if absent == "both" else (absent,):
        del body["cluster"][member]
    assert mock.exchange(minted["token"], body).status == 200
    (key,) = mock.installs()["keys"]
    assert key["cluster_arn"] is None and key["cluster_name"] is None
