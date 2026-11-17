# SPDX-License-Identifier: Apache-2.0

"""A token install belongs to Helm, or to a tool that runs Helm against the cluster.

The exchange hook, the Secret it fills and the uninstall hook are Helm hooks: Helm creates them when it installs,
upgrades or uninstalls, and it deletes the ones that carry a delete policy when a hook fails. A tool that renders the
chart without the cluster (`helm template`, Argo CD, `helm install --dry-run`) turns the hooks into plain objects or
into its own hooks. Then the pre-delete hook, a Job that stops the reporter and deletes the key Secret, runs when the
objects are applied, and a failed sync deletes the key Secret of a token install. The chart therefore refuses an install
token when it has no cluster to look in, and says what to do: give the key (ingest.existingSecret) and render with
--no-hooks. `install.allowRenderWithoutCluster` is for these tests, which render without a cluster to check the chart
itself.
"""

from __future__ import annotations

import pytest
import yaml
from test_chart import CHART, CI_VALUES, helm
from test_chart_exchange import TOKEN

# The install options page, which the chart README links to for installs without a token.
README = (CHART.parents[1] / "docs" / "install.md").read_text()


def template(*sets: str, switch: bool = False):
    """helm template with the repository's test values, the switch set the way the caller says, and `sets`."""
    command = ["template", "impact-ai", str(CHART), "-n", "impact-ai", "-f", str(CI_VALUES)]
    command += ["--set", f"install.allowRenderWithoutCluster={'true' if switch else 'false'}"]
    for item in sets:
        command += ["--set", item]
    return helm(*command, check=False)


def test_a_token_install_is_refused_when_the_chart_is_rendered_without_a_cluster():
    result = template("ingest.key=", f"install.token={TOKEN}")
    assert result.returncode != 0
    message = result.stderr
    for needed in ("install.token", "needs Helm", "ingest.existingSecret", "--no-hooks"):
        assert needed in message, needed
    assert TOKEN not in message, "the token is not echoed"


def test_the_refusal_says_why_in_plain_words():
    message = " ".join(template("ingest.key=", f"install.token={TOKEN}").stderr.split())
    assert "no cluster to look in" in message
    assert "uninstall hook would run" in message and "failed sync" in message and "key Secret" in message
    assert len(message) < 900, "a message, not a page"


def test_the_test_switch_lets_the_chart_tests_render_a_token_install():
    result = template("ingest.key=", f"install.token={TOKEN}", switch=True)
    assert result.returncode == 0, result.stderr
    assert "impact-ai-exchange" in result.stdout


@pytest.mark.parametrize(
    "sets",
    [["ingest.key=a-key-of-the-customers"], ["ingest.key=", "ingest.existingSecret=their-key"]],
    ids=["a key", "a Secret of the customer's"],
)
def test_the_key_modes_render_without_a_cluster_and_without_the_switch(sets):
    result = template(*sets)
    assert result.returncode == 0, result.stderr


def test_a_render_with_the_chart_tests_values_is_what_the_switch_is_for():
    assert yaml.safe_load(CI_VALUES.read_text())["install"]["allowRenderWithoutCluster"] is True
    assert yaml.safe_load((CHART / "values.yaml").read_text())["install"]["allowRenderWithoutCluster"] is False


def test_the_readme_says_why_a_token_install_needs_the_cluster_and_names_the_gitops_route():
    start = README.index("## Giving the key yourself")
    section = " ".join(README[start : README.index("\n## ", start)].split())
    for needed in (
        "helm template --no-hooks",
        "ingest.existingSecret",
        "refuses `install.token`",
        "the uninstall hook would run at install time",
        "a failed sync deletes the key Secret",
        "Flux",
    ):
        assert needed in section, needed
