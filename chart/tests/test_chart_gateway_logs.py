# SPDX-License-Identifier: Apache-2.0

"""The gateway forwards log events beside spans.

Instrumentations that follow the OpenTelemetry GenAI conventions write a model call's prompt and answer as log events
(Java, .NET and some Node.js instrumentations, Python's botocore), so a gateway that forwarded spans alone would keep
the call and lose its content. The gateway has a `logs` pipeline beside `traces`: the same receiver, the same
processors in the same order, the same exporter. One exporter means one key header, one gzip setting and one retry
policy for both signals, and it adds `/v1/logs` to the ingest origin where it adds `/v1/traces` for spans (the
container test in python/tests/image/test_kit_gateway_logs.py sends a log event through the pinned Collector and reads
it at the mock ingest).
"""

from __future__ import annotations

import pytest
from test_chart import CHART, gateway_attributes, gateway_container, gateway_env, render
from test_chart_gateway import gateway_config, with_cloud

SHAPES = pytest.mark.parametrize("make", [render, with_cloud], ids=["a pasted key", "a token and a cloud identity"])
PIPELINE = {
    "receivers": ["otlp"],
    "processors": ["memory_limiter", "transform/cluster", "batch"],
    "exporters": ["otlp_http"],
}


@SHAPES
def test_the_gateway_has_a_logs_pipeline_beside_traces_with_the_same_receiver_processors_and_exporter(make):
    pipelines = gateway_config(make())["service"]["pipelines"]
    # spans and log events; a metrics pipeline would be a decision of its own
    assert set(pipelines) == {"traces", "logs"}
    assert pipelines["traces"] == PIPELINE
    assert pipelines["logs"] == PIPELINE


@SHAPES
def test_a_log_record_gets_the_cluster_attributes_a_span_gets(make):
    documents = make()
    assert gateway_attributes(documents, "logs") == gateway_attributes(documents, "traces")
    assert {"k8s.cluster.uid", "k8s.cluster.name"} <= set(gateway_attributes(documents, "logs"))
    processor = gateway_config(documents)["processors"]["transform/cluster"]
    # not two lists that happen to agree today: the one list, written twice
    assert processor["log_statements"] == processor["trace_statements"]
    assert processor["error_mode"] == "ignore"  # a statement that fails is logged and the log records go on


def test_the_cloud_identity_reaches_log_records_as_it_reaches_spans_and_a_workloads_own_value_wins():
    attributes = gateway_attributes(with_cloud(), "logs")
    assert {k: v["action"] for k, v in attributes.items()} == {
        "k8s.cluster.uid": "upsert",
        "k8s.cluster.name": "upsert",
        "cloud.provider": "insert",
        "cloud.account.id": "insert",
        "cloud.region": "insert",
        "cloud.resource_id": "insert",
    }
    assert attributes["cloud.account.id"]["value"] == "012345678901"


def test_one_exporter_carries_both_signals_so_the_key_gzip_and_retries_are_the_same():
    config = gateway_config(render())
    assert list(config["exporters"]) == ["otlp_http"]
    exporter = config["exporters"]["otlp_http"]
    # the origin and nothing else: the exporter appends /v1/traces for spans and /v1/logs for log records
    assert exporter["endpoint"] == "${env:IMPACT_INGEST_ENDPOINT}"
    assert not {"traces_endpoint", "logs_endpoint", "metrics_endpoint"} & set(exporter)
    assert exporter["headers"] == {"authorization": "Bearer ${env:IMPACT_INGEST_KEY}"}
    assert exporter["compression"] == "gzip"
    assert exporter["retry_on_failure"] == {
        "enabled": True,
        "initial_interval": "1s",
        "max_interval": "30s",
        "max_elapsed_time": "120s",
    }


def test_the_memory_ceiling_and_the_queue_bounds_are_one_setting_for_both_signals():
    documents = render()
    config = gateway_config(documents)
    # one memory limiter and one batch processor, named by both pipelines: the Collector runs one limiter for the
    # whole process
    assert config["processors"]["memory_limiter"] == {
        "check_interval": "1s",
        "limit_percentage": 75,
        "spike_limit_percentage": 20,
    }
    assert config["processors"]["batch"] == {"timeout": "2s", "send_batch_size": 512}
    # the queue of the one exporter: 2,000 batches, once for spans and once for log records (the Collector makes a queue
    # of that size per signal; the README says so)
    assert config["exporters"]["otlp_http"]["sending_queue"] == {"enabled": True, "queue_size": 2000}
    # and the container's ceiling is what it was: the limit, and the 80% of it the Go runtime collects garbage below
    resources = gateway_container(documents)["resources"]
    assert resources["requests"] == {"cpu": "50m", "memory": "128Mi"}
    assert resources["limits"] == {"memory": "256Mi"}
    assert gateway_env(documents)["GOMEMLIMIT"] == "204MiB"


def test_no_value_reaches_the_configuration_except_through_the_statements_that_escape_it():
    # Every "$" of a value is written as "$$" (cluster-kit.collectorLiteral), and the statements are written once, in
    # one definition, whose lines are checked for that in test_chart_gateway_literals.py. Nothing else of the
    # configuration may take a value of the release: a second place that did would be a second place to forget the
    # escape.
    template = (CHART / "templates" / "gateway-config.yaml").read_text()
    body = template.split("config.yaml: |", 1)[1]
    assert ".Values" not in body and 'include "cluster-kit.' not in body
    assert template.count('define "cluster-kit.gatewayStatements"') == 1
    assert body.count("$statements | nindent") == 2  # the traces pipeline's statements and the logs pipeline's
