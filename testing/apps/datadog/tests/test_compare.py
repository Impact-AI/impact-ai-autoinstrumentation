# SPDX-License-Identifier: Apache-2.0

"""The relay's byte difference counts every request or answer that is not passed on as it came."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compare import relay_byte_difference  # noqa: E402

BODY = base64.b64encode(b"\x91\x91\x80").decode()
ANSWER = {"status": 200, "body_b64": base64.b64encode(b'{"rate_by_service":{}}').decode()}


def sent(**changes) -> dict:
    record = {
        "method": "PUT",
        "path": "/v0.4/traces",
        "query": "",
        "rawHeaders": [
            ["Host", "relay:8126"],
            ["Content-Type", "application/msgpack"],
            ["X-Datadog-Trace-Count", "1"],
            ["Content-Length", "3"],
        ],
        "headers": {"Host": "relay:8126"},
        "chunked": False,
        "body_b64": BODY,
        "response": ANSWER,
    }
    record.update(changes)
    return record


def received(**changes) -> dict:
    record = sent(**changes)
    record["rawHeaders"] = [
        ["Host", "agent:8126"] if name == "Host" else [name, value] for name, value in record["rawHeaders"]
    ]
    record["headers"] = {"Host": "agent:8126"}
    return record


def test_the_same_stream_differs_in_nothing():
    info = {"path": "/info", "method": "GET", "body_b64": "", "rawHeaders": [["Host", "relay:8126"]]}
    assert relay_byte_difference([sent(), sent(**info)], [received(), received(**info)], "agent:8126") == (0, [])


def test_a_changed_body_header_or_answer_counts():
    changed_body = received(body_b64=base64.b64encode(b"\x91\x90").decode())
    assert relay_byte_difference([sent()], [changed_body], "agent:8126")[0] == 2  # not received, and one extra
    dropped_header = received()
    dropped_header["rawHeaders"] = [h for h in dropped_header["rawHeaders"] if h[0] != "X-Datadog-Trace-Count"]
    assert relay_byte_difference([sent()], [dropped_header], "agent:8126")[0] == 2
    other_answer = received(response={"status": 200, "body_b64": ""})
    count, problems = relay_byte_difference([sent()], [other_answer], "agent:8126")
    assert count == 1 and "answer" in problems[0]


def test_the_agent_sees_its_own_host_and_a_true_length():
    assert relay_byte_difference([sent()], [sent()], "agent:8126")[0] == 1
    wrong_length = received()
    wrong_length["rawHeaders"] = [[n, "9" if n == "Content-Length" else v] for n, v in wrong_length["rawHeaders"]]
    assert relay_byte_difference([sent()], [wrong_length], "agent:8126")[0] == 1


def test_chunked_framing_is_not_a_difference():
    chunked = sent(chunked=True)
    chunked["rawHeaders"] = [h for h in chunked["rawHeaders"] if h[0] != "Content-Length"] + [
        ["Transfer-Encoding", "chunked"]
    ]
    assert relay_byte_difference([chunked], [received()], "agent:8126") == (0, [])
