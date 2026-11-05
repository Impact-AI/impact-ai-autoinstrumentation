# SPDX-License-Identifier: Apache-2.0

"""values.schema.json describes every value of the chart, in words a customer reads.

The chart's value reference is generated from this file, and the generation fails on any value without a description.
So: every property of the schema has one, every value of values.yaml has a property (the settings of the vendored
Operator chart under `operator` excepted, bar `operator.install`), and a description is one plain sentence that says
what the value does, what its default is, and when to change it.
"""

from __future__ import annotations

import json
import re

import yaml
from test_chart import CHART

SCHEMA = json.loads((CHART / "values.schema.json").read_text())
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())

# Everything under `operator` configures the vendored OpenTelemetry Operator chart (charts/opentelemetry-operator) and
# is that chart's to describe; `operator.install` is the kit's own switch for it.
OPERATOR_PASSTHROUGH = "operator."
OWN = {"operator.install"}

# The values this increment adds, which must be there whatever else changes.
NEW = (
    "install",
    "install.token",
    "cover",
    "cover.enabled",
    "cover.replicas",
    "cover.pollInterval",
    "cover.webhookConfigName",
    "cover.timeoutSeconds",
    "cover.resources",
    "cover.resources.requests.cpu",
    "cover.resources.requests.memory",
    "cover.resources.limits.memory",
    "images.nodejs",
    "images.nodejs.repository",
    "images.nodejs.tag",
    "images.nodejs.digest",
    "instrumentation.nodejs",
    "instrumentation.nodejs.resources",
)


def properties(node: dict, path: str = ""):
    """Every property below node, as (dotted path, definition); array items are walked under `path[]`."""
    for name, child in (node.get("properties") or {}).items():
        here = f"{path}.{name}" if path else name
        yield here, child
        yield from properties(child, here)
        if isinstance(child.get("items"), dict):
            yield from properties(child["items"], here + "[]")


NODES = dict(properties(SCHEMA))


def value_paths(node, path: str = "") -> list[str]:
    """Every key of values.yaml down to a leaf; an empty list or mapping is a leaf (it is itself the setting)."""
    if isinstance(node, dict) and node:
        found = []
        for key, child in node.items():
            found += value_paths(child, f"{path}.{key}" if path else key)
        return found
    return [path]


def chart_values() -> list[str]:
    return [p for p in value_paths(VALUES) if not p.startswith(OPERATOR_PASSTHROUGH) or p in OWN]


def test_every_value_of_the_schema_has_a_description():
    missing = [path for path, node in NODES.items() if not str(node.get("description", "")).strip()]
    assert missing == [], f"{len(missing)} of {len(NODES)} values have no description: {missing}"


def test_the_schema_has_a_property_for_every_value_a_customer_can_set():
    unknown = [path for path in chart_values() if path not in NODES]
    assert unknown == [], f"values.yaml has values the schema does not describe: {unknown}"


def test_every_value_this_increment_adds_is_in_both_files_and_described():
    for path in NEW:
        assert path in NODES, path
        assert str(NODES[path].get("description", "")).strip(), path
    for path in (p for p in NEW if p in set(chart_values())):
        assert path in NODES


def test_no_property_of_the_schema_is_a_value_the_chart_does_not_have():
    # A description of a value that does not exist misleads a reader.
    leaves_and_groups = set()
    for path in chart_values():
        parts = path.split(".")
        leaves_and_groups.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    stale = [p for p in NODES if "[]" not in p and p not in leaves_and_groups]
    assert stale == [], f"the schema describes values that values.yaml does not have: {stale}"


SENTENCE_BREAK = re.compile(r"[.!?]\s+[A-Z]")


def test_a_description_is_one_plain_sentence():
    problems = []
    for path, node in NODES.items():
        text = node.get("description", "")
        if not (30 <= len(text) <= 380):
            problems.append((path, f"{len(text)} characters"))
        if "\n" in text or text != text.strip():
            problems.append((path, "not one clean line"))
        if not text[:1].isupper() or not text.endswith("."):
            problems.append((path, "does not start with a capital and end with a full stop"))
        if SENTENCE_BREAK.search(text):
            problems.append((path, "more than one sentence"))
        if re.search(r"\b(TODO|FIXME|TBD|e\.g\.|i\.e\.)", text):
            problems.append((path, "a placeholder or an abbreviation"))
    assert problems == []


def default_of(path: str):
    node = VALUES
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_a_description_says_what_the_default_is():
    # In words for an empty or structured default, and the value itself for a short scalar one.
    problems = []
    for path, node in NODES.items():
        text = node["description"].lower()
        if "default" not in text and "by default" not in text:
            problems.append((path, "does not mention its default"))
            continue
        default = default_of(path)
        if isinstance(default, (str, int, float)) and not isinstance(default, bool) and 0 < len(str(default)) <= 40:
            if str(default).lower() not in text:
                problems.append((path, f"the default is {default!r} but the description does not say so"))
    assert problems == []


def test_a_description_says_when_to_change_the_value():
    # "you set it", "you change it", "you raise it" ...: the reader is told when the value is theirs to touch.
    problems = [
        path
        for path, node in NODES.items()
        if not re.search(r"\b(you|your|cover your ai|the command|the release|never|only)\b", node["description"], re.I)
    ]
    assert problems == []


def test_the_new_values_keep_their_constraints():
    assert NODES["install.token"]["type"] == "string"
    token = re.compile(NODES["install.token"]["pattern"])
    assert token.match("") and token.match("impactinstall_e2e_" + "A" * 43) and token.match("impactinstall_e2e_abc-_\n")
    assert not token.match("has space inside") and not token.match("impact install")
    assert set(NODES["cover.enabled"]["enum"]) == {"auto", "true", "false", True, False}
    assert NODES["cover.replicas"]["minimum"] == 1
    assert re.match(NODES["cover.pollInterval"]["pattern"], "1m") and not re.match(
        NODES["cover.pollInterval"]["pattern"], "soon"
    )
    assert (NODES["cover.timeoutSeconds"]["minimum"], NODES["cover.timeoutSeconds"]["maximum"]) == (1, 10)
    assert NODES["cover.timeoutSeconds"]["type"] == "integer"
    assert re.match(NODES["cover.webhookConfigName"]["pattern"], "impact-ai-cover")
    assert not re.match(NODES["cover.webhookConfigName"]["pattern"], "Impact AI")
    assert NODES["images.nodejs.digest"]["pattern"] == NODES["images.python.digest"]["pattern"]


def test_the_cluster_name_and_the_namespaces_are_held_to_what_the_intake_reads():
    # The intake reads a cluster name of at most 128 printable characters (no control character, no DEL) and a namespace
    # as a DNS label. Helm's schema engine reads Go regular expressions, which have no \u escape: the class is written
    # with \x. The behaviour is tested in test_chart_hostile_values.py.
    name = NODES["cluster.name"]
    assert name["maxLength"] == 128
    assert re.fullmatch(name["pattern"], "eu: prod #1 (Ä)")
    assert not any(re.fullmatch(name["pattern"], f"a{c}b") for c in ("\n", "\t", "\x1b", "\x7f"))
    label = "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
    for key in ("include", "exclude"):
        assert SCHEMA["properties"]["namespaces"]["properties"][key]["items"] == {"type": "string", "pattern": label}


def test_the_values_that_were_already_constrained_still_are():
    # The constraints of v0.2.0 are not loosened by describing them.
    assert NODES["product.id"]["minLength"] == 1 and NODES["ingest.endpoint"]["pattern"].startswith("^https?://")
    assert NODES["cluster.uid"]["pattern"] == "^([0-9a-fA-F-]{36})?$"
    assert NODES["cloud.provider"]["enum"] == ["", "aws", "azure", "gcp"]
    assert NODES["webhook.failurePolicy"]["enum"] == ["Ignore"]
    assert (NODES["webhook.timeoutSeconds"]["minimum"], NODES["webhook.timeoutSeconds"]["maximum"]) == (1, 10)
    assert NODES["operator.install"]["enum"] == ["auto", "true", "false", True, False]
    assert SCHEMA["required"] == ["product", "ingest"]
