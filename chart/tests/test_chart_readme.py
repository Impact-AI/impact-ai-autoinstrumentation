# SPDX-License-Identifier: Apache-2.0

"""The figures and names the chart README and the documentation pages state are the ones the chart renders.

A reviewer reads the README first, then the pages under docs/ it links to. A number that drifted from the chart is a
claim that stopped being true, so the requests, ports, user IDs, annotation keys and the gateway queue they name are
read from the render and from the code here, and a limit they promised to state cannot be deleted without this file
failing.
"""

from __future__ import annotations

import re

import yaml
from test_chart import CHART, find, pod_specs, render

ROOT = CHART.parents[1]
# The chart README and the pages it links to, read as one text: a fact may move between them, never out of them.
README = "\n".join(path.read_text() for path in [CHART / "README.md", *sorted((ROOT / "docs").glob("*.md"))])
FAILURE_MODES = (ROOT / "docs" / "failure-modes.md").read_text()
TOKEN = "impactinstall_e2e_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"
WITH_TOKEN = ("ingest.key=", f"install.token={TOKEN}")


def millicores(quantity: str) -> int:
    quantity = str(quantity)
    return int(quantity[:-1]) if quantity.endswith("m") else int(float(quantity) * 1000)


def mebibytes(quantity: str) -> int:
    match = re.fullmatch(r"(\d+)(Mi|Gi)", str(quantity))
    assert match, f"{quantity!r} is not a quantity in Mi or Gi"
    return int(match.group(1)) * (1024 if match.group(2) == "Gi" else 1)


def deployments(documents: list) -> dict[str, dict]:
    """Per Deployment: the CPU and memory its pods request and the memory they are limited to, replicas counted."""
    out = {}
    for deployment, spec in pod_specs(documents):
        if deployment["kind"] != "Deployment":
            continue
        replicas = deployment["spec"].get("replicas", 1)
        cpu = memory = limit = 0
        for container in spec["containers"]:
            resources = container.get("resources", {})
            cpu += millicores(resources.get("requests", {}).get("cpu", "0")) * replicas
            memory += mebibytes(resources.get("requests", {}).get("memory", "0")) * replicas
            limit += mebibytes(resources.get("limits", {}).get("memory", "0")) * replicas
        out[deployment["metadata"]["name"]] = {
            "cpu": cpu,
            "memory": memory,
            "limit": limit,
        }
    return out


def test_the_footprint_the_readme_states_is_the_sum_of_the_requests():
    key, token = deployments(render()), deployments(render(*WITH_TOKEN))
    assert sorted(key) == [
        "impact-ai-gateway",
        "impact-ai-operator",
        "impact-ai-reporter",
    ]
    assert sorted(token) == sorted([*key, "impact-ai-cover"])
    stated_without = re.search(r"\*\*(\d+)m CPU and (\d+) Mi\*\*", README)
    stated_with = re.search(r"to \*\*(\d+)m and (\d+) Mi\*\* with the\s+cover pod", README)
    assert stated_without and stated_with, "the totals are stated as '**90m CPU and 224 Mi**' and '**100m and 248 Mi**'"
    assert tuple(map(int, stated_without.groups())) == (
        sum(d["cpu"] for d in key.values()),
        sum(d["memory"] for d in key.values()),
    )
    assert tuple(map(int, stated_with.groups())) == (
        sum(d["cpu"] for d in token.values()),
        sum(d["memory"] for d in token.values()),
    )


def test_the_requests_and_limits_the_readme_gives_per_pod_are_the_rendered_ones():
    token = deployments(render(*WITH_TOKEN))
    flat = " ".join(README.split())
    for label, name in (
        ("Operator", "impact-ai-operator"),
        ("gateway", "impact-ai-gateway"),
        ("reporter", "impact-ai-reporter"),
    ):
        asked = f"the {label} {token[name]['cpu']}m and {token[name]['memory']} Mi"
        assert asked in flat, asked
    cover = token["impact-ai-cover"]
    assert "(10m and 24 Mi)" in flat and (cover["cpu"], cover["memory"]) == (10, 24)
    limits = {name: d["limit"] for name, d in token.items()}
    assert limits == {
        "impact-ai-operator": 256,
        "impact-ai-gateway": 256,
        "impact-ai-reporter": 128,
        "impact-ai-cover": 64,
    }
    assert "256 Mi for the Operator and the gateway, 128 Mi for the reporter and 64 Mi for the cover pod" in flat


def test_the_init_container_the_readme_describes_asks_what_the_instrumentation_resource_says():
    documents = render()
    resources = find(documents, "Instrumentation")["spec"]["python"]["resourceRequirements"]
    assert resources == {
        "requests": {"cpu": "10m", "memory": "16Mi"},
        "limits": {"cpu": "200m", "memory": "64Mi"},
    }
    flat = " ".join(README.split())
    assert "asks 10m CPU and 16 Mi" in flat
    assert "limited to 200m and 64 Mi" in flat
    ratio = millicores(resources["limits"]["cpu"]) // millicores(resources["requests"]["cpu"])
    assert f"below {ratio} (200m against 10m)" in flat, "the LimitRange ratio that refuses the init container"


def test_the_ports_the_readme_names_are_the_ones_the_pods_listen_on():
    documents = render(*WITH_TOKEN)
    gateway = find(documents, "Service", "impact-ai-gateway")
    assert 4318 in {port["port"] for port in gateway["spec"]["ports"]}
    ports = {}
    for deployment, spec in pod_specs(documents):
        for container in spec["containers"]:
            for port in container.get("ports", []):
                ports.setdefault(deployment["metadata"]["name"], set()).add(port["containerPort"])
    assert 9443 in ports["impact-ai-cover"] and 9443 in ports["impact-ai-operator"]
    assert 4318 in ports["impact-ai-gateway"]
    for number in ("4318", "9443", "tcp:9443"):
        assert number in README, number


def test_the_user_ids_the_readme_names_are_the_only_fixed_ones_the_kit_sets():
    documents = render(
        *WITH_TOKEN,
        "images.nodejs.tag=v0.0.0-test",
        "images.nodejs.digest=sha256:" + "c" * 64,
    )
    ids = set()
    for _, spec in pod_specs(documents):
        ids.add(spec.get("securityContext", {}).get("runAsUser"))
        for container in [*spec["containers"], *spec.get("initContainers", [])]:
            ids.add(container.get("securityContext", {}).get("runAsUser"))
    ids.add(find(documents, "Instrumentation")["spec"]["initContainerSecurityContext"]["runAsUser"])
    assert ids - {None} == {65532, 10001}
    assert "65532" in README and "10001" in README


def test_the_gateway_queue_the_readme_describes_is_the_one_configured():
    configuration = yaml.safe_load(find(render(), "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"])
    exporter = configuration["exporters"]["otlp_http"]
    assert exporter["sending_queue"] == {"enabled": True, "queue_size": 2000}
    assert exporter["retry_on_failure"]["max_elapsed_time"] == "120s"
    # the one exporter serves both pipelines, and the Collector makes a queue of that size for each signal (read from
    # the pinned Collector's own metrics in python/tests/image/test_kit_gateway_logs.py)
    assert sorted(configuration["service"]["pipelines"]) == ["logs", "traces"]
    flat = " ".join(README.split())
    assert "each in a queue in memory (2,000 batches each)" in flat and "up to 120 seconds" in flat


def test_the_log_endpoint_the_readme_gives_is_the_one_the_gateway_listens_on():
    documents = render()
    env = {e["name"]: e.get("value") for e in find(documents, "Instrumentation")["spec"]["python"]["env"]}
    assert f"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT={env['IMPACT_OTLP_ENDPOINT']}/v1/logs" in README
    # OTLP over HTTP on 4318 and nothing else, as the README says: no gRPC port
    assert {port["port"] for port in find(documents, "Service", "impact-ai-gateway")["spec"]["ports"]} == {4318}
    receiver = yaml.safe_load(find(documents, "ConfigMap", "impact-ai-gateway")["data"]["config.yaml"])["receivers"]
    assert receiver == {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
    assert "OTLP over HTTP, protobuf or JSON, on port 4318" in " ".join(README.split())


def test_the_annotations_the_readme_says_the_cover_pod_may_add_are_the_five_it_may():
    source = (ROOT / "reporter" / "internal" / "cover" / "decide.go").read_text()
    names = re.search(r"var allowedKeys = \[\]string\{([^}]*)\}", source).group(1).split(",")
    constants = (ROOT / "reporter" / "internal" / "cover" / "cover.go").read_text()
    keys = []
    for name in (n.strip() for n in names if n.strip()):
        value = re.search(rf'\b{name}\s*=\s*"([^"]+)"', constants)
        if value:
            keys.append(value.group(1))
        else:  # InjectPython = InjectPrefix + "python"
            prefix = re.search(r'InjectPrefix\s*=\s*"([^"]+)"', constants).group(1)
            suffix = re.search(rf'\b{name}\s*=\s*InjectPrefix \+ "([^"]+)"', constants).group(1)
            keys.append(prefix + suffix)
    assert len(keys) == 5, keys
    flat = " ".join(README.split())
    assert "annotations from five keys" in flat and "the label `impact.ai/auto-covered=true`" in flat
    for key in keys:
        short = key.replace("instrumentation.opentelemetry.io/", "")
        assert key in README or short in README, key


# What the README promised to state. Each is a limit or a condition a reader would otherwise find out in production, and
# none of them may be deleted without this list changing in review.
STATED = (
    "--reset-then-reuse-values",
    "kubectl apply --server-side",
    "masterIpv4CidrBlock",
    "pkg-containers.githubusercontent.com",
    "OpenShift is not supported",
    "fs.inotify.max_user_instances",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT",
    "query string",
    "impact.ai/opt-out",
    "impact.ai/trust-caller-baggage",
    "namespaces.include",
    "cover.enabled=false",
    "Helm 3.18",
    "AKS Automatic",
    "Karpenter",
    "Linux nodes",
    "operator.imagePullSecrets",
    "--collector-image",
    "oras cp -r <src> <dst>",
    "PowerShell and cmd have no `/dev/stdin`",
    "No long-lived role of the kit reads a Secret",
    "KSV-0113",
    "Where the token can be read",
    "The memory limits are",
    "at most 16 MiB of unsent spans",
    "core distribution `otelcol`",
    "about 20 seconds at exit",
    "Behind an image mirror, ask for it",
    "automatic coverage needs the matching impact.ai release",
    "It writes to no pod",
    "`cover.timeoutSeconds`",
    "log events to `<ingest.endpoint>/v1/logs`",
    "## Log events",
    "The gateway forwards every log record it receives",
    "The kit's packages send no log records",
)


def test_the_readme_states_every_limit_it_promised():
    flat = " ".join(README.split())
    missing = [phrase for phrase in STATED if phrase not in flat]
    assert missing == []


# The failure table says which of its cases the end-to-end suite runs. Each case it names is a test of the suite, and
# the cases it says it does not run are the ones no test makes: they follow from the webhook settings and the init
# container.
RUN_IN_THE_SUITE = {
    "the Operator down": "test_the_operator_down",
    "the cover pod down": "with_the_cover_pod_down",
    "ingest down": "test_ingest_down",
    "a revoked key": "test_a_revoked_key",
    "an image that cannot be pulled": "test_the_injected_image_cannot_be_pulled",
    "a policy that refuses the init container": "when_a_policy_refuses_the_injected_init_container",
}
NOT_RUN = ("a webhook that is slow", "cannot reach", "a copy that fails in the init container")


def test_the_failure_table_says_which_cases_the_suite_runs_and_which_it_does_not():
    flat = " ".join(FAILURE_MODES.split())
    section = flat[flat.index("## When part of the kit is unavailable") :]
    sentence = section[: section.index("| What is unavailable")]
    assert "the end-to-end suite runs each case below" not in flat
    assert "The end-to-end suite runs these cases" in sentence
    suite = "".join(path.read_text() for path in (ROOT / "e2e").glob("test_*.py"))
    for case, test in RUN_IN_THE_SUITE.items():
        assert case in sentence, case
        assert f"def {test}" in suite or test in suite, f"the failure modes say the suite runs {case}: no test {test}"
    assert "are not run" in sentence
    for case in NOT_RUN:
        assert case in sentence, case
