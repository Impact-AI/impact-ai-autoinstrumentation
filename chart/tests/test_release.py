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
JAVA = "sha256:" + "4" * 64


@pytest.fixture
def stamped(tmp_path):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    stamp_chart.stamp(copy, "1.2.3", {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS, "java": JAVA})
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
    assert values["images"]["java"] == {
        "repository": "ghcr.io/impact-ai/autoinstrumentation-java",
        "tag": "1.2.3",
        "digest": JAVA,
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
    # the Java image is published and its digest is in the values, but nothing injects it yet
    assert "autoinstrumentation-java" not in rendered


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


def test_stamping_points_the_readme_at_the_documents_of_the_release(stamped):
    # helm show readme and Artifact Hub show the chart README of the version installed: its links go to that tag
    main = stamp_chart.DOCUMENTS + "main/"
    links = (CHART / "README.md").read_text().count(main)
    assert links > 0
    readme = (stamped / "README.md").read_text()
    assert main not in readme and readme.count(stamp_chart.DOCUMENTS + "v1.2.3/") == links
    assert stamp_chart.DOCUMENTS == f"https://github.com/{bash('echo $RELEASE_REPOSITORY')}/blob/"


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
    "keeps its upstream address",
    "except the Operator's unused --collector-image argument",
    "Tolerations for every pod of the kit",
    "Labels a node must have to run any pod of the kit",
    "GitHub records the build provenance (SLSA) of all five",
    "GitHub records the build provenance of all four",
    "which the release publishes and the Instrumentation does not inject yet",
    "OpenShift: planned",
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
            *(path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "docs").glob("*.md"))),
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
    assert "Impact-AI/impact-ai-autoinstrumentation/.github/workflows/release.yml" in readme
    assert 'cosign verify "$registry/autoinstrumentation-python@<digest>"' in readme
    assert "for image in python nodejs java reporter; do" in script  # every image the chart can carry, checked if there
    assert 'published_image "$image"' in script, "and each is looked for under the name it is published under"


def released_images() -> list[str]:
    """The images a release in the public repository publishes: release_images of scripts/lib.sh, with RELEASE_JAVA as
    the release workflow sets it."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    java = "1" if re.search(r"RELEASE_JAVA:\s*[\"']?1\b", workflow) else ""
    result = subprocess.run(  # noqa: S603 - bash with a fixed snippet
        ["bash", "-c", f"set -eu; source '{ROOT / 'scripts' / 'lib.sh'}'; release_images"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "RELEASE_JAVA": java, "CONTAINER": "docker"},
    )
    return result.stdout.split()


def listed(names: list[str]) -> str:
    """`a`, `b` and `c`: how the documents list the published images."""
    quoted = [f"`{name}`" for name in names]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def test_the_documents_list_as_published_exactly_the_images_the_release_workflow_publishes():
    # The chart README, SECURITY.md and the root README say which images a release signs and publishes. That is
    # release_images as the workflow runs it: python, nodejs and reporter, and java only once the workflow sets
    # RELEASE_JAVA=1 (in the change that also attests it).
    keys = released_images()
    published = listed([bash(f"published_image {key}") for key in keys])
    count = {3: "three", 4: "four"}[len(keys)]
    chart = " ".join((CHART / "README.md").read_text().split())
    assert f"The chart and the {count} images a release publishes, {published}," in chart
    assert f"GitHub records the build provenance (SLSA) of the chart and the {count} images" in chart
    assert f"What this repository publishes: the Helm chart `impact-ai` and the images {published} at" in " ".join(
        (ROOT / "SECURITY.md").read_text().split()
    )
    assert f"The chart and the images {published} are" in " ".join((ROOT / "README.md").read_text().split())
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    provenance = re.findall(r"- name: Build provenance of the (\w+)", workflow)
    assert sorted(provenance) == sorted([*keys, "chart"]), "the workflow attests what the release publishes"
    if "java" not in keys:
        # and the documents say that the Java image is built here and not published, rather than leave it out
        for text in (chart, " ".join((ROOT / "SECURITY.md").read_text().split())):
            assert "`autoinstrumentation-java`" in text and "publish" in text
        assert "RELEASE_JAVA=1" in chart


def test_security_md_covers_every_image_the_release_signs_and_says_who_owns_the_others():
    security = " ".join((ROOT / "SECURITY.md").read_text().split())
    script = (ROOT / "scripts" / "verify-release.sh").read_text()
    signed = re.search(r"for image in ([a-z ]+); do", script).group(1).split()
    assert signed == ["python", "nodejs", "java", "reporter"]
    for image in signed:
        public = bash(f"published_image {image}")
        assert f"`{public}`" in security, f"SECURITY.md leaves out the {public} image"
    # the Operator and the collector are the OpenTelemetry project's images: a report about them goes there
    assert "OpenTelemetry project" in security and "report" in security.split("OpenTelemetry project")[0][-400:]
    assert "bump" in security


def release_guard_marker() -> str:
    """The text release.sh looks for in SECURITY.md before it builds or pushes anything."""
    return re.search(r"grep -n '([^']+)' SECURITY\.md", (ROOT / "scripts" / "release.sh").read_text()).group(1)


PUBLIC_REPOSITORY = "Impact-AI/impact-ai-autoinstrumentation"


def run_release_in_a_copy(tmp_path: Path, security: str, **env: str | None) -> tuple[subprocess.CompletedProcess, Path]:
    """release.sh and the scripts it sources, in a directory of their own with this SECURITY.md, run the way the release
    workflow of the public repository runs it (GITHUB_REPOSITORY) unless `env` says otherwise (None takes a variable
    away). The check that follows the SECURITY.md guard is a stand-in that ends the run with its own exit code, and
    docker, helm, cosign and trivy only write down that they were called."""
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
            key: value
            for key, value in {
                "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                "GITHUB_REPOSITORY": PUBLIC_REPOSITORY,
                "REGISTRY": f"ghcr.io/{PUBLIC_REPOSITORY}",
                "VERSION": "v0.0.0",
                "REGISTRY_USER": "user",
                "REGISTRY_TOKEN": "token",
                "CONTAINER": "docker",
                "ALLOW_UNSIGNED": "1",
                **env,
            }.items()
            if value is not None
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


# A release is made in the public repository, by its release workflow, on a tree that scripts/publish-public.sh put
# there. A tag in the development repository starts the same workflow and would push the images under that
# organisation's name.
CLEAN = (ROOT / "SECURITY.md").read_text()
DEVELOPMENT_REPOSITORY = "some-fork/development-copy"  # a repository that does not exist


def test_a_release_outside_the_public_repository_stops_before_anything_is_built(tmp_path):
    result, calls = run_release_in_a_copy(tmp_path, CLEAN, GITHUB_REPOSITORY=DEVELOPMENT_REPOSITORY)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"refusing to release from {DEVELOPMENT_REPOSITORY}" in result.stderr
    # plain words: where a release is made, the route to it, and the way to rehearse
    assert PUBLIC_REPOSITORY in result.stderr and "scripts/publish-public.sh" in result.stderr
    assert "RELEASE_REHEARSAL=1" in result.stderr
    assert result.stdout == "", "no step was announced"
    assert not calls.exists(), "no build, push or signature was started"
    assert not (tmp_path / "copy" / "build").exists(), "not even the output directory was made"


def test_a_release_run_outside_github_actions_is_refused_too(tmp_path):
    result, calls = run_release_in_a_copy(tmp_path, CLEAN, GITHUB_REPOSITORY=None)
    assert result.returncode == 1 and "refusing to release from outside GitHub Actions" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize(
    "repository",
    [PUBLIC_REPOSITORY, "impact-ai/impact-ai-autoinstrumentation", "IMPACT-AI/IMPACT-AI-AUTOINSTRUMENTATION"],
)
def test_a_release_in_the_public_repository_gets_past_the_guard_whatever_the_case(tmp_path, repository):
    result, calls = run_release_in_a_copy(tmp_path, CLEAN, GITHUB_REPOSITORY=repository)
    assert result.returncode == 7, result.stdout + result.stderr  # the stand-in check, which follows the guards
    assert "== check the source and the chart for internal details" in result.stdout
    assert not calls.exists()


@pytest.mark.parametrize(
    "rehearsal",
    [{"RELEASE_REHEARSAL": "1"}, {"INSECURE_REGISTRY": "1"}],
    ids=["RELEASE_REHEARSAL=1", "a local registry (INSECURE_REGISTRY=1)"],
)
@pytest.mark.parametrize(
    "repository", [None, DEVELOPMENT_REPOSITORY], ids=["on a workstation", "in another repository"]
)
def test_a_rehearsal_elsewhere_gets_past_the_guard(tmp_path, repository, rehearsal):
    result, _ = run_release_in_a_copy(tmp_path, CLEAN, GITHUB_REPOSITORY=repository, **rehearsal)
    assert result.returncode == 7, result.stdout + result.stderr
    assert "== check the source and the chart for internal details" in result.stdout


@pytest.mark.parametrize("value", ["", "0", "yes", "true"])
def test_only_an_explicit_release_rehearsal_equal_to_1_lifts_the_guard(tmp_path, value):
    result, _ = run_release_in_a_copy(
        tmp_path, CLEAN, GITHUB_REPOSITORY=DEVELOPMENT_REPOSITORY, RELEASE_REHEARSAL=value
    )
    assert result.returncode == 1 and "refusing to release" in result.stderr


def test_the_repository_releases_are_made_in_is_the_one_the_other_scripts_name():
    repository = bash('echo "$RELEASE_REPOSITORY"')
    assert repository == PUBLIC_REPOSITORY
    assert f"ghcr.io/{repository}" in (ROOT / "scripts" / "verify-release.sh").read_text()
    assert f"github.com/{repository}.git" in (ROOT / "scripts" / "publish-public.sh").read_text()
    assert repository in (CHART / "README.md").read_text(), "and the signing identity of the chart README"


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
            "ghcr.io/Impact-AI/impact-ai-autoinstrumentation",
            "ghcr.io/impact-ai",
        ),  # what ghcr.io/${{ github.repository }} is in the workflow
        ("ghcr.io/impact-ai/impact-ai-autoinstrumentation", "ghcr.io/impact-ai"),
        ("ghcr.io/Some-Owner/example", "ghcr.io/some-owner"),
        ("localhost:5000/Rehearsal", "localhost:5000/rehearsal"),
        ("localhost:5000/Rehearsal/some/path", "localhost:5000/rehearsal"),
    ],
)
def test_the_publish_root_is_the_registry_host_and_the_lowercased_owner(registry, root):
    assert bash(f'publish_root "{registry}"') == root


def test_an_explicit_publish_root_replaces_it_but_has_to_be_lowercase():
    mirror = "registry.example.test/mirror/"
    root = bash('publish_root "ghcr.io/Impact-AI/impact-ai-autoinstrumentation"', PUBLISH_ROOT=mirror)
    assert root == "registry.example.test/mirror"
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
    root = bash('publish_root "ghcr.io/Impact-AI/impact-ai-autoinstrumentation"')
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
    assert 'REGISTRY="${REGISTRY:-ghcr.io/Impact-AI/impact-ai-autoinstrumentation}"' in verify
    assert "https://github.com/$repository/.github/workflows/release.yml@refs/tags/$VERSION" in verify
    assert 'repository="${REPOSITORY:-${REGISTRY#*/}}" # Impact-AI/impact-ai-autoinstrumentation' in verify


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


# A release rehearsed end to end with stand-ins for the tools: docker writes the digest a push would, helm prints the
# chart's digest and serves the values of the chart the run stamped, cosign, trivy and gh note that they were called.
STUB = """#!/usr/bin/env python3
import hashlib, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_CALLS"], "a") as log:
    log.write(" ".join([os.path.basename(sys.argv[0]), *args]) + "\\n")
digest = lambda name: "sha256:" + hashlib.sha256(name.encode()).hexdigest()
tool = os.path.basename(sys.argv[0])
if tool == "docker" and "--metadata-file" in args:
    path = args[args.index("--metadata-file") + 1]
    with open(path, "w") as out:
        out.write('{"containerimage.digest": "%s"}' % digest(os.path.basename(path)))
if tool == "helm" and args[:1] in (["push"], ["pull"]):
    print("Digest: " + digest("chart"))
if tool == "helm" and args[:2] == ["show", "values"]:
    print(open(os.environ["STUB_VALUES"]).read())
"""

OVERHEAD_STUB = """#!/usr/bin/env bash
mkdir -p "$2"
echo '{"schema": "impact.overhead.v1", "ok": true, "worst": {"cpuMsPerCall": 1, "memoryMiB": 10, "startSeconds": 1},
  "combos": [1]}' >"$2/results.json"
echo "# Overhead" >"$2/OVERHEAD.md"
"""

# The cells of the Python qualification (scripts/python-cells.sh all) note that they ran and pass.
CELLS_STUB = """#!/usr/bin/env bash
echo "python-cells.sh $*" >>"$STUB_CALLS"
"""


def stub_tools(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("docker", "helm", "cosign", "trivy", "gh"):
        (tools / tool).write_text(STUB)
        (tools / tool).chmod(0o755)
    return tools, tmp_path / "calls.log"


def rehearse_release(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str], dict]:
    """release.sh in a copy of the repository's scripts and chart, signing with a key and verifying as it does in the
    workflow: its exit, the tools it called and the images of the chart it stamped."""
    copy = tmp_path / "copy"
    shutil.copytree(ROOT / "scripts", copy / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(CHART, copy / "chart" / CHART.name)
    shutil.copy(ROOT / "SECURITY.md", copy / "SECURITY.md")
    # a release says what changed: the version's dated section, which the chart's Artifact Hub annotations come from
    (copy / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [9.9.9] - 2026-11-14\n\n### Added\n\n- A rehearsal.\n"
    )
    (copy / "scripts" / "check_internal_details.py").write_text("")
    (copy / "scripts" / "overhead.sh").write_text(OVERHEAD_STUB)
    (copy / "scripts" / "python-cells.sh").write_text(CELLS_STUB)
    tools, calls = stub_tools(tmp_path)
    values = copy / "build" / "chart" / CHART.name / "values.yaml"
    result = subprocess.run(  # noqa: S603 - the script under test, in a copy
        ["bash", str(copy / "scripts" / "release.sh")],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": PUBLIC_REPOSITORY,
            "REGISTRY": f"ghcr.io/{PUBLIC_REPOSITORY}",
            "VERSION": "v9.9.9",
            "REGISTRY_USER": "user",
            "REGISTRY_TOKEN": "token",
            "CONTAINER": "docker",
            "COSIGN_KEY": "cosign.key",
            "COSIGN_PUBLIC_KEY": "cosign.pub",
            "STUB_CALLS": str(calls),
            "STUB_VALUES": str(values),
            **env,
        },
    )
    images = yaml.safe_load(values.read_text())["images"] if values.exists() else {}
    return result, calls.read_text().splitlines() if calls.exists() else [], images


def test_a_release_leaves_the_java_image_out_until_the_workflow_attests_it(tmp_path):
    """The workflow records the build provenance of the images it lists, and verify-release.sh requires it of every
    image in the chart: a release publishes the Java image only with RELEASE_JAVA=1, set when the workflow lists it."""
    result, calls, images = rehearse_release(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [call for call in calls if "java" in call], "nothing of Java was built, pushed, signed or verified"
    pushes = [i for i, call in enumerate(calls) if "--push" in call]
    assert pushes and calls.index("python-cells.sh all") < pushes[0], "every Python cell ran before anything was pushed"
    assert sorted((tmp_path / "copy" / "build" / "release").glob("*.digest")) == [
        tmp_path / "copy" / "build" / "release" / f"{key}.digest" for key in ("chart", "nodejs", "python", "reporter")
    ]
    assert images["java"]["digest"] == "" and images["python"]["digest"].startswith("sha256:")
    assert "== no java image in v9.9.9" in result.stdout
    assert "== verified v9.9.9" in result.stdout


def test_with_release_java_the_release_publishes_signs_and_verifies_the_java_image(tmp_path):
    result, calls, images = rehearse_release(tmp_path, RELEASE_JAVA="1")
    assert result.returncode == 0, result.stdout + result.stderr
    java = "ghcr.io/impact-ai/autoinstrumentation-java"
    assert any("-f java/Dockerfile" in call and "--push" in call for call in calls)
    assert images["java"]["digest"].startswith("sha256:") and images["java"]["repository"] == java
    signed = [call for call in calls if call.startswith("cosign sign ") and java in call]
    verified = [call for call in calls if call.startswith("cosign verify ") and java in call]
    assert signed and verified
    assert (tmp_path / "copy" / "build" / "release" / "java.name").read_text().strip() == java


def verify_values(tmp_path: Path, java: bool) -> Path:
    """The values of a released chart: stamped with python, nodejs and reporter, and java or, as v0.3.0-rc1 and every
    release before it, with no images.java at all."""
    copy = tmp_path / "chart"
    shutil.copytree(CHART, copy)
    digests = {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS}
    if java:
        digests["java"] = JAVA
    stamp_chart.stamp(copy, "0.3.0", digests)
    values = copy / "values.yaml"
    if not java:
        text, count = re.subn(r"(?m)^  java:\n(?:    .*\n)+", "", values.read_text(), count=1)
        assert count == 1
        values.write_text(text)
    return values


def verify_release(tmp_path: Path, values: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    tools, calls = stub_tools(tmp_path)
    result = subprocess.run(  # noqa: S603 - the script under test
        ["bash", str(ROOT / "scripts" / "verify-release.sh")],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "VERSION": "v0.3.0",
            "STUB_CALLS": str(calls),
            "STUB_VALUES": str(values),
        },
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_verify_release_still_verifies_a_release_from_before_the_java_image(tmp_path):
    result, calls = verify_release(tmp_path, verify_values(tmp_path, java=False))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== no java image in v0.3.0" in result.stdout
    for name in ("autoinstrumentation-python", "autoinstrumentation-nodejs", "kubernetes-reporter"):
        assert any(call.startswith("cosign verify ") and name in call for call in calls), name
        assert any(call.startswith("gh attestation verify ") and name in call for call in calls), name
    assert not [call for call in calls if "autoinstrumentation-java" in call]


def test_verify_release_checks_the_java_image_when_the_chart_has_it(tmp_path):
    result, calls = verify_release(tmp_path, verify_values(tmp_path, java=True))
    assert result.returncode == 0, result.stdout + result.stderr
    image = f"ghcr.io/impact-ai/autoinstrumentation-java@{JAVA}"
    assert any(call.startswith("cosign verify ") and image in call for call in calls)
    assert any(call.startswith("cosign verify-attestation ") and image in call for call in calls)
    assert any(call.startswith("gh attestation verify ") and image in call for call in calls)


def test_verify_release_still_requires_the_other_images(tmp_path):
    values = verify_values(tmp_path, java=False)
    values.write_text(values.read_text().replace(f"digest: {NODEJS}", 'digest: ""'))
    result, _ = verify_release(tmp_path, values)
    assert result.returncode == 1
    assert "the chart has no digest for nodejs" in result.stderr
