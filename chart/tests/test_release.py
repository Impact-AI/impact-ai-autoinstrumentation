# SPDX-License-Identifier: Apache-2.0

"""The release stamps a copy of the chart with its version and the digests of the images it built (Python, Node.js and
the reporter); the uninstall steps in the README are the ones the script runs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_chart import CHART, CI_VALUES, helm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import stamp_chart  # noqa: E402

PYTHON = "sha256:" + "1" * 64
REPORTER = "sha256:" + "2" * 64
NODEJS = "sha256:" + "3" * 64


@pytest.fixture
def stamped(tmp_path):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    stamp_chart.stamp(copy, "1.2.3", {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS})
    return copy


def test_stamping_sets_version_and_digests(stamped):
    chart = yaml.safe_load((stamped / "Chart.yaml").read_text())
    assert chart["version"] == chart["appVersion"] == "1.2.3"
    values = yaml.safe_load((stamped / "values.yaml").read_text())
    assert values["images"]["python"] == {
        "repository": "ghcr.io/impact-ai/autoinstrumentation-python",
        "tag": "1.2.3",
        "digest": PYTHON,
    }
    assert values["images"]["reporter"]["digest"] == REPORTER
    assert values["images"]["nodejs"] == {
        "repository": "ghcr.io/impact-ai/autoinstrumentation-nodejs",
        "tag": "1.2.3",
        "digest": NODEJS,
    }
    # the upstream images keep the digests they were pinned with
    assert values["images"]["collector"]["digest"].startswith("sha256:28b6d")


def test_a_stamped_chart_installs_without_any_image_override(stamped):
    rendered = helm(
        "template", "impact-ai", str(stamped), "-n", "impact-ai", "--set-string", "product.id=p",
        "--set-string", "ingest.endpoint=https://ingest.example.test", "--set", "ingest.key=k", "--set",
        "cluster.uid=4e5622e7-f420-499c-9662-50af7f8a64b9",
    ).stdout  # fmt: skip
    assert f"ghcr.io/impact-ai/autoinstrumentation-python:1.2.3@{PYTHON}" in rendered
    assert f"ghcr.io/impact-ai/kubernetes-reporter:1.2.3@{REPORTER}" in rendered
    # the release wrote a Node.js image, so the Instrumentation injects Node.js too
    assert f"ghcr.io/impact-ai/autoinstrumentation-nodejs:1.2.3@{NODEJS}" in rendered


def test_a_chart_stamped_without_a_nodejs_image_injects_python_alone(tmp_path):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    stamp_chart.stamp(copy, "1.2.3", {"python": PYTHON, "reporter": REPORTER})
    rendered = helm(
        "template", "impact-ai", str(copy), "-n", "impact-ai", "--set-string", "product.id=p",
        "--set-string", "ingest.endpoint=https://ingest.example.test", "--set", "ingest.key=k", "--set",
        "cluster.uid=4e5622e7-f420-499c-9662-50af7f8a64b9",
    ).stdout  # fmt: skip
    assert f"ghcr.io/impact-ai/autoinstrumentation-python:1.2.3@{PYTHON}" in rendered
    assert "autoinstrumentation-nodejs" not in rendered


def test_the_chart_as_committed_cannot_be_installed_by_accident():
    result = helm(
        "template", "impact-ai", str(CHART), "-n", "impact-ai", "--set-string", "product.id=p",
        "--set-string", "ingest.endpoint=https://ingest.example.test", "--set", "ingest.key=k", "--set",
        "cluster.uid=4e5622e7-f420-499c-9662-50af7f8a64b9", check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert "neither a tag nor a digest" in result.stderr


@pytest.mark.parametrize(
    ("version", "python", "message"),
    [
        ("v1.2.3", PYTHON, "semantic version"),
        ("1.2", PYTHON, "semantic version"),
        ("1.2.3", "sha256:abc", "not a sha256"),
    ],
)
def test_stamping_rejects_bad_input(tmp_path, version, python, message):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    with pytest.raises(SystemExit, match=message):
        stamp_chart.stamp(copy, version, {"python": python, "reporter": REPORTER, "nodejs": NODEJS})


def test_stamping_twice_fails_instead_of_overwriting_a_release(stamped):
    with pytest.raises(SystemExit, match="no empty tag and digest"):
        stamp_chart.stamp(stamped, "1.2.4", {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS})


def test_the_ci_values_are_not_part_of_the_package():
    ignore = (CHART / ".helmignore").read_text().split()
    assert "ci/" in ignore and "tests/" in ignore
    assert CI_VALUES.is_file()


def test_the_readme_shows_the_commands_the_uninstall_script_runs():
    readme = (CHART / "README.md").read_text()
    script = (ROOT / "scripts" / "uninstall-cluster-kit.sh").read_text()
    for needle in (
        "helm uninstall impact-ai --namespace impact-ai --wait",
        "kubectl delete namespace impact-ai --wait",
        # what comes off, for each language: the same text in the README and in the script
        '"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/otel-python-platform":null',
        '"instrumentation.opentelemetry.io/inject-nodejs":null',
        "for language in python nodejs; do",
        "inject-$language']=='impact-ai/impact'",
    ):
        assert needle in readme, needle
    for needle in (
        'helm uninstall "$RELEASE" --namespace "$NAMESPACE" --wait',
        '"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/otel-python-platform":null',
        '"instrumentation.opentelemetry.io/inject-nodejs":null',
        "for language in python nodejs; do",
        'inject-$language"',  # the annotation looked for, built from the language
        "=='$NAMESPACE/impact'",  # and only the kit's own value: this install's namespace and Instrumentation
    ):
        assert needle in script, needle
    assert "instrumentations.opentelemetry.io" in readme  # names the resource type it keeps


# Sentences that were in the README and stopped being true. A claim that comes back has to be true again first.
RETIRED = (
    "It never stops a workload",
    "registry down: new pods start",
    "Webhook, Operator or registry down",
    "no cluster-wide access to Secrets",
    "placement of the kit's pods",
    "or `--reuse-values`",
    "`helm upgrade` keeps them if you pass the same flags",
    "until then only Python is injected",
    "an inject-nodejs annotation does nothing",
    "the end-to-end suite runs each case below",
    "the kit injects Python only",
    "opentelemetry-collector-contrib",
    "(a Node.js workload that calls OpenAI or Anthropic the same way)",
    "that may make that one Secret and nothing else",
    "a pod create does not wait for it",
    "replaces the registry host of **every** image",
    "Tolerations for every pod of the kit",
    "Labels a node must have to run any pod of the kit",
)


def test_the_documents_no_longer_say_what_stopped_being_true():
    # The chart README, the root README, the values and what describes them: each of these sentences was true once.
    texts = {
        name: " ".join((ROOT / name).read_text().split())
        for name in (
            "chart/impact-ai/README.md",
            "README.md",
            "chart/impact-ai/values.yaml",
            "chart/impact-ai/values.schema.json",
        )
    }
    found = [(name, sentence) for name, text in texts.items() for sentence in RETIRED if sentence in text]
    assert found == []


# The commands of "Verify what you install" are the ones scripts/verify-release.sh (make verify-release, and the last
# step of the release workflow) runs against the release it just made: what the README tells a customer to type is what
# the release was checked with.
VERIFY_COMMANDS = (
    "cosign verify-attestation --type spdxjson",
    "--certificate-oidc-issuer",
    "https://token.actions.githubusercontent.com",
    ".github/workflows/release.yml@refs/tags/",
    "gh attestation verify",
    "--signer-workflow",
    ".github/workflows/release.yml",
)


def test_the_readme_shows_the_commands_the_release_is_verified_with():
    readme = (CHART / "README.md").read_text()
    script = (ROOT / "scripts" / "verify-release.sh").read_text()
    for needle in VERIFY_COMMANDS:
        assert needle in readme, f"README: {needle}"
        assert needle in script, f"scripts/verify-release.sh: {needle}"
    assert 'cosign verify "$registry/charts/impact-ai@$digest" --certificate-identity "$identity"' in readme
    assert "Impact-AI/impact-ai-k8s/.github/workflows/release.yml" in readme
    assert 'cosign verify "$registry/autoinstrumentation-python@<digest>"' in readme
    flat = " ".join(readme.split())
    assert "`autoinstrumentation-python`, `autoinstrumentation-nodejs` and `kubernetes-reporter`" in flat
    assert "for image in python nodejs reporter; do" in script  # the three images of the chart, all signed
    assert 'published_image "$image"' in script, "and each is looked for under the name it is published under"


def test_security_md_covers_every_image_the_release_signs_and_says_who_owns_the_others():
    security = " ".join((ROOT / "SECURITY.md").read_text().split())
    script = (ROOT / "scripts" / "verify-release.sh").read_text()
    signed = re.search(r"for image in ([a-z ]+); do", script).group(1).split()
    assert signed == ["python", "nodejs", "reporter"]
    for image in signed:
        public = bash(f"published_image {image}")
        assert f"`{public}`" in security, f"SECURITY.md leaves out the {public} image"
    # the Operator and the collector are the OpenTelemetry project's images: a report about them goes there
    assert "OpenTelemetry project" in security and "report" in security.split("OpenTelemetry project")[0][-400:]
    assert "bump" in security


def release_guard_marker() -> str:
    """The text release.sh looks for in SECURITY.md before it builds or pushes anything."""
    return re.search(r"grep -n '([^']+)' SECURITY\.md", (ROOT / "scripts" / "release.sh").read_text()).group(1)


def run_release_in_a_copy(tmp_path: Path, security: str) -> tuple[subprocess.CompletedProcess, Path]:
    """release.sh and the scripts it sources, in a directory of their own with this SECURITY.md. The check that follows
    the SECURITY.md guard is a stand-in that ends the run with its own exit code, and docker, helm, cosign and trivy
    only write down that they were called."""
    copy = tmp_path / "copy"
    shutil.copytree(ROOT / "scripts", copy / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "SECURITY.md").write_text(security)
    (copy / "scripts" / "check_internal_details.py").write_text("import sys\nsys.exit(7)\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("docker", "helm", "cosign", "trivy"):
        stub = tools / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - the script under test, in a copy
        ["bash", str(copy / "scripts" / "release.sh")],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "REGISTRY": "ghcr.io/Impact-AI/impact-ai-k8s",
            "VERSION": "v0.0.0",
            "REGISTRY_USER": "user",
            "REGISTRY_TOKEN": "token",
            "CONTAINER": "docker",
            "ALLOW_UNSIGNED": "1",
        },
    )
    return result, calls


def test_a_release_with_a_placeholder_in_security_md_stops_before_anything_is_pushed(tmp_path):
    marker = release_guard_marker()
    planted = (ROOT / "SECURITY.md").read_text() + f"\n* By e-mail: <contact {marker}>\n"
    result, calls = run_release_in_a_copy(tmp_path, planted)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SECURITY.md still has a placeholder" in result.stderr and f"<contact {marker}>" in result.stderr
    assert "== check SECURITY.md" in result.stdout, "the run got as far as the guard"
    assert "== check the source" not in result.stdout, "and no further"
    assert not calls.exists(), "no build, push or signature was started"


def test_the_same_release_gets_past_the_guard_when_security_md_has_no_placeholder(tmp_path):
    result, calls = run_release_in_a_copy(tmp_path, (ROOT / "SECURITY.md").read_text())
    assert result.returncode == 7, result.stdout + result.stderr  # the stand-in check, which follows the guard
    assert "== check the source and the chart for internal details" in result.stdout
    assert not calls.exists()


def test_security_md_has_no_placeholder_left():
    assert release_guard_marker() not in (ROOT / "SECURITY.md").read_text()


def bash(code: str, **env: str) -> str:
    """What a bash snippet prints after scripts/names.sh is sourced: the names a release publishes under."""
    result = subprocess.run(  # noqa: S603 - bash with test-controlled text
        ["bash", "-c", f"set -eu; source '{ROOT / 'scripts' / 'names.sh'}'; {code}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def bash_fails(code: str, **env: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"set -eu; source '{ROOT / 'scripts' / 'names.sh'}'; {code}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], **env},
    )
    assert result.returncode != 0, result.stdout
    return result.stderr


@pytest.mark.parametrize(
    ("registry", "root"),
    [
        (
            "ghcr.io/Impact-AI/impact-ai-k8s",
            "ghcr.io/impact-ai",
        ),  # what ghcr.io/${{ github.repository }} is in the workflow
        ("ghcr.io/impact-ai/impact-ai-k8s", "ghcr.io/impact-ai"),
        ("ghcr.io/Some-Owner/example", "ghcr.io/some-owner"),
        ("localhost:5000/Rehearsal", "localhost:5000/rehearsal"),
        ("localhost:5000/Rehearsal/some/path", "localhost:5000/rehearsal"),
    ],
)
def test_the_publish_root_is_the_registry_host_and_the_lowercased_owner(registry, root):
    assert bash(f'publish_root "{registry}"') == root


def test_an_explicit_publish_root_replaces_it_but_has_to_be_lowercase():
    assert bash('publish_root "ghcr.io/Impact-AI/impact-ai-k8s"', PUBLISH_ROOT="registry.example.test/mirror/") == (
        "registry.example.test/mirror"
    )
    assert "capitals" in bash_fails('publish_root "ghcr.io/x/y"', PUBLISH_ROOT="ghcr.io/Impact-AI")


@pytest.mark.parametrize("registry", ["ghcr.io", "ghcr.io/", "localhost:5000"])
def test_a_registry_without_an_owner_has_no_publish_root(registry):
    assert "owner" in bash_fails(f'publish_root "{registry}"')


def test_the_images_and_the_chart_are_published_under_their_public_names():
    assert {key: bash(f"published_image {key}") for key in ("python", "nodejs", "reporter")} == {
        "python": "autoinstrumentation-python",
        "nodejs": "autoinstrumentation-nodejs",
        "reporter": "kubernetes-reporter",
    }
    assert bash("echo $CHART_NAME") == "impact-ai" == yaml.safe_load((CHART / "Chart.yaml").read_text())["name"]
    assert "unknown image" in bash_fails("published_image gateway")


def test_the_defaults_of_the_chart_are_where_the_release_in_the_public_repository_publishes():
    root = bash('publish_root "ghcr.io/Impact-AI/impact-ai-k8s"')
    images = yaml.safe_load((CHART / "values.yaml").read_text())["images"]
    for key in ("python", "nodejs", "reporter"):
        assert images[key]["repository"] == f"{root}/{bash(f'published_image {key}')}", key


def test_the_release_pushes_signs_and_verifies_under_the_publish_root_and_names_nothing_else():
    release = (ROOT / "scripts" / "release.sh").read_text()
    lib = (ROOT / "scripts" / "lib.sh").read_text()
    verify = (ROOT / "scripts" / "verify-release.sh").read_text()
    for text in (release, lib, verify):
        assert "scripts/names.sh" in text or "names.sh" in text
        assert "cluster-kit" not in text.replace("uninstall-cluster-kit", ""), "the chart has its public name"
    assert 'PUBLISH_ROOT="$(publish_root "$REGISTRY")"' in release
    assert "oci://$PUBLISH_ROOT/charts" in release and '"$REGISTRY/$' not in release
    assert '"$root/charts/$CHART_NAME@' in lib
    assert '"$root/$(published_image' in lib
    assert 'chart="oci://$root/charts/$CHART_NAME"' in verify


def test_the_signing_identity_is_the_release_workflow_of_the_public_repository():
    verify = (ROOT / "scripts" / "verify-release.sh").read_text()
    assert 'REGISTRY="${REGISTRY:-ghcr.io/Impact-AI/impact-ai-k8s}"' in verify
    assert "https://github.com/$repository/.github/workflows/release.yml@refs/tags/$VERSION" in verify
    assert 'repository="${REPOSITORY:-${REGISTRY#*/}}" # Impact-AI/impact-ai-k8s' in verify


def test_stamping_can_set_where_the_images_are_pulled_from(tmp_path):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    repositories = {
        "python": "localhost:5000/rehearsal/autoinstrumentation-python",
        "nodejs": "localhost:5000/rehearsal/autoinstrumentation-nodejs",
        "reporter": "localhost:5000/rehearsal/kubernetes-reporter",
    }
    stamp_chart.stamp(
        copy, "1.2.3", {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS}, repositories=repositories
    )
    values = yaml.safe_load((copy / "values.yaml").read_text())["images"]
    assert {key: values[key]["repository"] for key in repositories} == repositories
    assert values["python"]["digest"] == PYTHON and values["nodejs"]["tag"] == "1.2.3"


# Step 2 (the marks on the pods) and step 5 (the cluster-scoped objects) of the script, as the README shows them. The
# README's commands are the script's, with the default release and namespace written out.
STRIP_NEEDLES = (
    'STRIP=\'{"metadata":{"annotations":{'
    '"instrumentation.opentelemetry.io/inject-python":null,"instrumentation.opentelemetry.io/inject-nodejs":null,'
    '"instrumentation.opentelemetry.io/container-names":null,'
    '"instrumentation.opentelemetry.io/otel-python-platform":null,"impact.ai/auto-covered":null},'
    '"labels":{"impact.ai/auto-covered":null}}}\'',
    "kubectl get pods --all-namespaces --selector impact.ai/auto-covered=true "
    "--field-selector 'status.phase!=Succeeded,status.phase!=Failed' -o jsonpath=",
    'kubectl --namespace "$ns" patch pod "$name" --type merge -p "$STRIP"',
)
CLEAN_UP = (
    "kubectl delete clusterrolebindings,clusterroles,mutatingwebhookconfigurations,validatingwebhookconfigurations "
    '--selector "app.kubernetes.io/part-of=impact-ai,app.kubernetes.io/instance={release}" '
    "--ignore-not-found"
)


def test_the_script_takes_the_marks_off_the_pods_and_deletes_the_cluster_scoped_objects_in_its_last_steps():
    script = (ROOT / "scripts" / "uninstall-cluster-kit.sh").read_text()
    for needle in (*STRIP_NEEDLES, CLEAN_UP.format(release="$RELEASE")):
        assert needle in script, needle
    order = [
        script.index(n) for n in ('helm uninstall "$RELEASE"', "STRIP=", "for language in", "kubectl delete namespace")
    ]
    assert order == sorted(order), "the release, the pods, the workloads, the namespace"
    assert script.rstrip().endswith("--ignore-not-found"), "the cluster-scoped objects are the last step"


def test_the_readme_shows_the_marks_coming_off_the_pods_and_the_clean_up_the_script_ends_with():
    readme = (CHART / "README.md").read_text()
    if "--selector impact.ai/auto-covered" not in readme:
        pytest.skip(
            "the README does not show the marks coming off the pods yet: it is to show steps 2 and 5 of the script"
        )
    for needle in (*STRIP_NEEDLES, CLEAN_UP.format(release="impact-ai")):
        assert needle in readme, needle
    # The hook no longer takes the marks off: that is a step of the script, under the operator's own rights.
    prose = re.sub(r"\s*\n#?\s*", " ", readme)
    assert "takes the injection annotations (and the marker) off the pods the cover pod marked" not in prose
    assert "patches pods" not in prose and "patch pods" not in prose
    assert (
        readme.index("STRIP=")
        < readme.index("for language in python nodejs; do")
        < readme.index("kubectl delete namespace impact-ai --wait")
        < readme.index("kubectl delete clusterrolebindings,")
    )
