# SPDX-License-Identifier: Apache-2.0

"""The gateway runs the OpenTelemetry Collector core distribution (`otelcol`), pinned by digest.

The distribution with every contributed component (`otelcol-contrib`) carries libraries the gateway never runs, and a
HIGH finding in one of them (go-jose, through the OIDC and Pulsar components) would have shipped with the kit. The
gateway needs six components, and the core distribution has all of them: the OTLP receiver, the memory limiter, the
transform processor, the batch processor, the OTLP/HTTP exporter and the health check extension.
"""

from __future__ import annotations

import json

import pytest
import yaml
from test_chart import CHART, CI_VALUES, DIGEST, find, gateway_attributes, helm, render
from test_chart_readme import WITH_TOKEN

CORE_IMAGE = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())

# What `otelcol components` lists for the core distribution, version 0.164.0 (podman run <image> components). A
# component of the gateway that is not here is one the core image does not have.
CORE_COMPONENTS = {
    "receivers": {"file_log", "host_metrics", "jaeger", "kafka", "nop", "otlp", "prometheus", "zipkin"},
    "processors": {
        "attributes",
        "batch",
        "filter",
        "memory_limiter",
        "probabilistic_sampler",
        "resource",
        "resource_detection",
        "span",
        "transform",
    },
    "exporters": {
        "debug",
        "file",
        "kafka",
        "nop",
        "otlp_grpc",
        "otlp_http",
        "prometheus",
        "prometheus_remote_write",
        "zipkin",
    },
    "extensions": {"health_check", "pprof", "zpages"},
    "connectors": {"forward"},
}
CLOUD = (
    "cloud.provider=aws",
    "cloud.accountId=012345678901",
    "cloud.region=eu-west-1",
    "cluster.arn=arn:aws:eks:eu-west-1:012345678901:cluster/c",
)


def gateway_config(documents: list) -> dict:
    return yaml.safe_load(find(documents, "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"])


def with_cloud() -> list:
    return render(*WITH_TOKEN, extra=[arg for value in CLOUD for arg in ("--set-string", value)])


def test_the_gateway_image_is_the_core_distribution_pinned_by_digest():
    collector = VALUES["images"]["collector"]
    assert collector["repository"] == CORE_IMAGE
    assert collector["tag"] == "0.164.0" and DIGEST.search("@" + collector["digest"])
    reference = f"{CORE_IMAGE}:0.164.0@{collector['digest']}"
    gateway = find(render(), "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]
    assert gateway["image"] == reference


@pytest.mark.parametrize("registry", ["", "registry.example.com/mirror"], ids=["ghcr.io", "a mirror"])
def test_the_operators_default_collector_image_is_the_same_image_so_that_no_contrib_image_is_named(registry):
    # Behind a mirror too: both references follow global.imageRegistry, so the Operator names no image outside it.
    documents = render(f"global.imageRegistry={registry}")
    reference = f"{CORE_IMAGE}:0.164.0@{VALUES['images']['collector']['digest']}"
    if registry:
        reference = reference.replace("ghcr.io", registry, 1)
    gateway = find(documents, "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]
    operator = find(documents, "Deployment", "impact-ai-operator")["spec"]["template"]["spec"]["containers"][0]
    assert gateway["image"] == reference
    assert f"--collector-image={reference}" in operator["args"]


@pytest.mark.parametrize("shape", [[], list(WITH_TOKEN)], ids=["a pasted key", "a token"])
def test_nothing_the_chart_renders_names_the_contrib_distribution(shape):
    rendered = json.dumps(render(*shape)).lower()
    assert "contrib" not in rendered
    assert "otelcol-contrib" not in rendered


def test_the_gateway_container_takes_its_binary_from_the_image_and_one_argument():
    # The image's entrypoint is /otelcol (the contrib image's was /otelcol-contrib): the chart names neither, so the
    # container is the image's own binary with the one configuration file.
    container = find(render(), "Deployment", "impact-ai-gateway")["spec"]["template"]["spec"]["containers"][0]
    assert "command" not in container
    assert container["args"] == ["--config=/conf/config.yaml"]


@pytest.mark.parametrize("make", [render, with_cloud], ids=["a pasted key", "a token and a cloud identity"])
def test_the_gateway_uses_only_components_of_the_core_distribution(make):
    config = gateway_config(make())
    named = {kind: set() for kind in CORE_COMPONENTS}
    for kind in CORE_COMPONENTS:
        named[kind] |= {name.split("/")[0] for name in config.get(kind, {})}
    for pipeline in config["service"]["pipelines"].values():
        for kind, key in (("receivers", "receivers"), ("processors", "processors"), ("exporters", "exporters")):
            named[kind] |= {name.split("/")[0] for name in pipeline[key]}
    named["extensions"] |= {name.split("/")[0] for name in config["service"].get("extensions", [])}
    for kind, names in named.items():
        assert names <= CORE_COMPONENTS[kind], (kind, sorted(names - CORE_COMPONENTS[kind]))
    # and they are the six the gateway needs, none of them the deprecated names the core distribution warns about
    assert named["receivers"] == {"otlp"} and named["exporters"] == {"otlp_http"}
    assert named["processors"] == {"memory_limiter", "transform", "batch"}
    assert named["extensions"] == {"health_check"}


def test_the_gateway_configuration_holds_none_of_the_names_the_core_distribution_deprecates():
    # `otlphttp` is an alias that logs a warning at every start, and the resource processor leaves the core distribution
    # in 0.167.0: the same attributes are set with the transform processor.
    text = find(render(), "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"]
    assert "otlphttp" not in text and "resource/cluster" not in text and "processors:\n  resource" not in text


NAMES = [
    'a "quoted" name',
    "back\\slash",
    "colon: space # hash",
    "prod & <staging>",
    "ünïcode — dash",
    "it's [odd] {and} (so) is 'this'",
    "trailing space ",
]


@pytest.mark.parametrize("name", NAMES, ids=range(len(NAMES)))
def test_a_cluster_name_with_symbols_reaches_the_gateway_as_the_one_value_it_was(name, tmp_path):
    values = tmp_path / "name.yaml"
    values.write_text(yaml.safe_dump({"cluster": {"name": name}}))
    out = helm("template", "impact-ai", str(CHART), "-n", "impact-ai", "-f", str(CI_VALUES), "-f", str(values)).stdout
    documents = [d for d in yaml.safe_load_all(out) if d]
    attributes = gateway_attributes(documents)
    assert attributes["k8s.cluster.name"]["value"] == name
    assert attributes["k8s.cluster.name"]["action"] == "upsert"
    assert set(attributes) == {"k8s.cluster.uid", "k8s.cluster.name"}


def test_the_cloud_identity_fills_a_gap_and_the_cluster_attributes_always_win():
    attributes = gateway_attributes(with_cloud())
    assert {k: v["action"] for k, v in attributes.items()} == {
        "k8s.cluster.uid": "upsert",
        "k8s.cluster.name": "upsert",
        "cloud.provider": "insert",
        "cloud.account.id": "insert",
        "cloud.region": "insert",
        "cloud.resource_id": "insert",
    }
    assert attributes["cloud.account.id"]["value"] == "012345678901"
    # a failing statement must not stop the spans: the processor goes on and logs
    config = gateway_config(with_cloud())
    assert config["processors"]["transform/cluster"]["error_mode"] == "ignore"
