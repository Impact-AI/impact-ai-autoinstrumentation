# SPDX-License-Identifier: Apache-2.0
"""scripts/check_internal_details.py: nothing internal reaches the repository, the chart or the images.

The check has two parts. The built-in one finds what is internal to anybody: private top-level names, tailnet names,
e-mail addresses, account IDs, private addresses, tokens, the ids and words of impact.ai's planning and the other
repositories of the organisation.
The names that are internal to one company (its hosts, its environments, its customers) are in a deny-list that lives
outside the repository, and the check reads it when it is given (--denylist FILE, or the variable named in
check.DENYLIST_ENV) and says so when it is not.

This file holds only fictitious values: account numbers that belong to nobody, example domains, a company and a
customer that do not exist. What the built-in patterns would find in this file is put together at run time
("db.office" + ".lan"), so that the file holds no literal the check would find in it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load():
    spec = importlib.util.spec_from_file_location(
        "check_internal_details", ROOT / "scripts" / "check_internal_details.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load()

A, B, C = "10", "244", "0"
INTERNAL_HOST = "build01.office" + ".lan"
EMAIL = "someone" + "@" + "corp-example.io"
ACCOUNT = "2109" + "8765" + "4321"  # twelve digits, and not one of the documentation account IDs the allowlist names
PRIVATE = {
    "ten": ".".join((A, B, C, "18")),
    "172": ".".join(("172", "20", "1", "9")),
    "192": ".".join(("192", "168", "0", "7")),
    "cgnat": ".".join(("100", "101", "2", "3")),
}
TOKENS = {
    "github": "ghp_" + "a1B2c3D4e5" * 4,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "openai": "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0",
    "slack": "xoxb-" + "1234567890-abcdefghij",
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0",
}


def categories(text: str, source: str = "somewhere/file.txt") -> set[str]:
    return {f.category for f in check.scan_text(source, text)}


@pytest.mark.parametrize(
    "text",
    [
        INTERNAL_HOST,
        "x.tailnet-example" + ".ts.net",
        "host.corp-network" + ".internal",
        "host" + ".corp",
        "db.office" + ".lan",
        "gw" + ".intranet",
        "printer.home" + ".arpa",
        "build" + ".local",
    ],
)
def test_internal_hosts_are_found(text):
    assert categories(f"url = https://{text}/v1") >= {"internal-host"}


@pytest.mark.parametrize(
    "text",
    [
        "https://ingest.example.test",
        "https://impact.ai/docs",
        "https://ingest.impact.ai",
        "http://impact-ai-gateway.impact-ai.svc:4318",
        "kubernetes.default.svc.cluster.local",
        "http://127.0.0.1:8080 and 0.0.0.0",
        "localhost",
        "ghcr.io/impact-ai/autoinstrumentation-python",
    ],
)
def test_public_and_cluster_local_hosts_are_not_flagged(text):
    assert "internal-host" not in categories(text)


def test_a_private_top_level_name_is_a_whole_label():
    assert "internal-host" not in categories("host" + ".corp-network is not a host")
    assert "internal-host" not in categories("see storage" + ".local-path-provisioner")
    assert "internal-host" in categories("db" + ".corp.")  # the end of a sentence still ends the name


def test_email_addresses_are_found_but_not_the_reserved_documentation_ones():
    assert categories(f"contact {EMAIL} for access") == {"email"}
    assert categories("someone" + "@" + "example.com") == set()
    assert categories("bot" + "@" + "users.noreply.github.com") == set()
    assert categories("a@b") == set()


def test_twelve_digit_account_ids_are_found_wherever_they_stand():
    assert categories(f"arn:aws:eks:eu-west-1:{ACCOUNT}:cluster/prod") == {"aws-account-id"}
    assert categories(f"accountId: '{ACCOUNT}'") == {"aws-account-id"}
    assert categories(ACCOUNT + "1") == set()  # 13 digits: a timestamp, not an account
    assert categories("v" + ACCOUNT[:11]) == set()  # 11 digits
    assert categories("0x" + ACCOUNT) == set()  # inside a larger token
    assert categories("instance 5b1d0f4c-0000-4000-8000-000000000001") == set()  # the last group of a UUID


@pytest.mark.parametrize("address", PRIVATE.values())
def test_private_addresses_are_found(address):
    assert categories(f"host: {address}") == {"private-ip"}
    assert categories(f"({address}).") == {"private-ip"}  # at the end of a sentence


@pytest.mark.parametrize(
    "text",
    [
        "8.8.8.8",
        "127.0.0.1",
        "0." + "0.0.0",  # not a bind address: the text a scan must not mistake for a private one
        "172.32.0.1",
        "192.169.0.1",
        "100.200.1.1",
        "1.10.0.1",
        "300.1.1.1",
        "1.2.3.4.5",
    ],
)
def test_other_addresses_and_version_numbers_are_not_flagged(text):
    assert "private-ip" not in categories(f"x {text} y")


@pytest.mark.parametrize("token", TOKENS.values(), ids=TOKENS.keys())
def test_tokens_are_found(token):
    assert categories(f"value = {token}") == {"token"}


def test_a_bearer_reference_to_an_environment_variable_is_not_a_token():
    assert categories("authorization: Bearer ${env:IMPACT_INGEST_KEY}") == set()
    assert categories("Bearer " + "x" * 30) == {"token"}


# A company and a customer that do not exist, and a deny-list the way a company keeps its own outside the repository.
CUSTOMER = "Zorblax"
DENYLIST = """\
# What is internal to one company: a category, a colon, a regular expression (read without regard to case).
internal-host: \\bbuild-farm\\b
internal-host: \\b(?:[a-z0-9-]+\\.)*(?:dev|staging)\\.example-company\\.test\\b

customer-name: zorblax
"""


@pytest.fixture
def denylist(tmp_path):
    path = tmp_path / "denylist.txt"
    path.write_text(DENYLIST)
    return path


def test_the_checker_names_nothing_that_is_internal_to_one_company():
    # The built-in host patterns are the ones that mean "internal" to anybody; the company's own hosts, environments
    # and customers are in a deny-list that lives outside the repository.
    generic = ("ts\\.net", "internal|corp|lan|intranet", "home\\.arpa", "local")
    assert all(any(g in pattern.pattern for g in generic) for pattern in check.HOSTS), [p.pattern for p in check.HOSTS]
    assert not hasattr(check, "CUSTOMERS")
    assert check.DENYLIST == (), "no deny-list is loaded until one is asked for"


def test_a_denylist_names_what_is_internal_to_one_company(denylist):
    entries = check.load_denylist(denylist)
    assert [(e.category, e.pattern.pattern) for e in entries] == [
        ("internal-host", "\\bbuild-farm\\b"),
        ("internal-host", "\\b(?:[a-z0-9-]+\\.)*(?:dev|staging)\\.example-company\\.test\\b"),
        ("customer-name", "zorblax"),
    ]
    sample = "see build-farm and https://api.STAGING.example-company.test/v1 for " + CUSTOMER
    assert categories(sample) == set(), "without the deny-list the names mean nothing to the check"
    with check.denying(entries):
        found = check.scan_text("docs/notes.md", sample)
    assert [(f.category, f.match) for f in found] == [
        ("internal-host", "build-farm"),
        ("internal-host", "api.STAGING.example-company.test"),
        ("customer-name", CUSTOMER),
    ]


def test_a_host_the_builtin_patterns_and_the_denylist_both_find_is_one_finding(denylist):
    with check.denying(check.load_denylist(denylist)):
        found = check.scan_text("a.txt", "host build-farm.office" + ".lan here")
    assert [(f.category, f.match) for f in found] == [("internal-host", "build-farm.office" + ".lan")]


@pytest.mark.parametrize(
    "text",
    [
        CUSTOMER,
        CUSTOMER.lower(),
        CUSTOMER.upper(),
        CUSTOMER + "'s agent service",
        "my-" + CUSTOMER.lower() + "-prod",
        "test_" + CUSTOMER.lower() + ".py",
        "app_" + CUSTOMER.lower() + "_chat_model",
    ],
)
def test_a_customers_name_in_the_denylist_is_found_in_any_case_and_inside_words(text, denylist):
    with check.denying(check.load_denylist(denylist)):
        assert categories(f"example: {text} and more") == {"customer-name"}


def test_words_that_only_look_like_the_name_are_not_flagged(denylist):
    with check.denying(check.load_denylist(denylist)):
        assert categories("a zorb, a blax, Zor and Blax, z-o-r-b-l-a-x") == set()


def test_the_name_is_found_in_a_file_name_too_with_line_zero(tmp_path, denylist):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)  # noqa: S603, S607
    name = "test_" + CUSTOMER.lower() + ".py"
    (repository / name).write_text("clean = True\n")
    (repository / "other.py").write_text("clean = True\n")
    with check.denying(check.load_denylist(denylist)):
        assert [(f.source, f.line, f.category) for f in check.scan_source(repository)] == [(name, 0, "customer-name")]


def test_no_allowlist_entry_excuses_a_customers_name():
    assert [e for e in check.ALLOWLIST if e.category == "customer-name"] == []


@pytest.mark.parametrize(
    "line",
    [
        "customer-name zorblax",  # no colon
        "nonsense: zorblax",  # not a category
        "customer-name: ",  # nothing after the colon
        "customer-name: (unclosed",  # not a regular expression
    ],
)
def test_a_line_of_the_denylist_that_cannot_be_read_is_an_error_that_names_the_line(line, tmp_path):
    path = tmp_path / "denylist.txt"
    path.write_text("# fine\n\n" + line + "\n")
    with pytest.raises(ValueError, match=r"denylist\.txt:3"):
        check.load_denylist(path)


def test_the_denylist_comes_from_the_flag_or_the_variable_and_is_skipped_when_there_is_none(tmp_path):
    here = tmp_path / "here.txt"
    here.write_text(DENYLIST)
    env = {check.DENYLIST_ENV: str(here)}
    assert check.denylist_path(["--source"], {}) is None
    assert check.denylist_path(["--source"], env) == here
    other = tmp_path / "other.txt"
    other.write_text(DENYLIST)
    assert check.denylist_path(["--source", "--denylist", str(other)], env) == other  # the flag wins
    # a variable that names a file that is not there is skipped; a flag that does, is not
    assert check.denylist_path(["--source"], {check.DENYLIST_ENV: str(tmp_path / "missing.txt")}) is None
    with pytest.raises(FileNotFoundError):
        check.denylist_path(["--source", "--denylist", str(tmp_path / "missing.txt")], {})


def test_the_check_says_when_it_ran_without_a_denylist_and_with_one(denylist, monkeypatch, capsys, tmp_path):
    archive = tmp_path / "clean.tar"
    archive.write_bytes(docker_archive({"licenses/LICENSE": ""}))
    monkeypatch.delenv(check.DENYLIST_ENV, raising=False)
    assert check.main(["--archive", str(archive)]) == 0
    assert "no deny-list" in capsys.readouterr().err
    assert check.main(["--archive", str(archive), "--denylist", str(denylist)]) == 0
    err = capsys.readouterr().err
    assert "no deny-list" not in err and "3 entries" in err
    assert check.DENYLIST == (), "main leaves no deny-list loaded behind it"


def test_an_unreadable_denylist_stops_the_check_before_it_scans(tmp_path, capsys):
    path = tmp_path / "bad.txt"
    path.write_text("nonsense: x\n")
    assert check.main(["--source", "--denylist", str(path)]) == 2
    assert "bad.txt:1" in capsys.readouterr().err


def test_the_repository_holds_nothing_the_denylist_of_the_company_names(monkeypatch):
    # Run with the variable set to the company's own deny-list, outside the repository, before a release.
    import os

    path = os.environ.get(check.DENYLIST_ENV)
    if not path or not Path(path).is_file():
        pytest.skip(f"{check.DENYLIST_ENV} names no deny-list: only the built-in checks ran")
    with check.denying(check.load_denylist(Path(path))):
        found = check.violations(check.scan_source(ROOT) + check.scan_chart(ROOT))
    assert [(f.source, f.line, f.category) for f in found] == []


# An organisation that does not exist, and the entry a company keeps in its deny-list for its own: every path of it.
# The checker names no organisation: it is published with the tree it checks.
PRIVATE_ORG = "example-private-org"
ORG_ENTRY = f"internal-reference: \\b{PRIVATE_ORG}/[A-Za-z0-9._-]*\n"
PLAN_ID = "I-" + "123"  # the letter I, a hyphen and three digits; not an id of any plan
PRIVATE_REPOSITORIES = [
    PRIVATE_ORG + "/" + name for name in ("example-kit", "example-service", "example-docs", "x")
]  # the development repository included: nothing of the private organisation is in what is published


@pytest.fixture
def private_org_file(tmp_path):
    path = tmp_path / "private-org.txt"
    path.write_text(ORG_ENTRY)
    return path


@pytest.fixture
def private_org(private_org_file):
    """The company's entry for its own organisation, in force for the test."""
    with check.denying(check.load_denylist(private_org_file)):
        yield


@pytest.mark.parametrize("text", [f"The first one is {PLAN_ID}.", f"({PLAN_ID}: the cluster kit)"])
def test_plan_ids_are_found(text):
    assert categories(text) == {"internal-reference"}


@pytest.mark.parametrize("repository", PRIVATE_REPOSITORIES)
def test_every_repository_of_the_private_organisation_is_found_by_the_denylist(repository, private_org):
    assert categories(f"definition in {repository}, internal/research") == {"internal-reference"}
    assert categories(f"https://github.com/{repository}/pull/3") == {"internal-reference"}


@pytest.mark.parametrize("repository", PRIVATE_REPOSITORIES)
def test_without_the_denylist_an_organisation_means_nothing_to_the_checker(repository):
    assert categories(f"definition in {repository}, internal/research") == set()


def test_the_checker_holds_no_pattern_for_any_organisation():
    # It is published with the tree it checks: the references it knows by itself are the ids and words of a plan.
    assert [pattern.pattern for pattern in check.REFERENCES] == [
        r"\bI-\d{3}\b",
        r"\bAE-\d+\b",
        r"(?<![0-9A-Za-z])test_ae\d+_",
        r"\bincrements?\b",
        r"\bmonorepo\b",
    ]
    assert check.DENYLIST == ()


@pytest.mark.parametrize(
    "text",
    [
        "ghcr.io/impact-ai/autoinstrumentation-python:0.2.2",
        "oci://ghcr.io/impact-ai/charts/impact-ai",
        "https://github.com/Impact-AI/impact-autoinstrumentation/security/advisories/new",
        "Impact-AI/impact-autoinstrumentation/.github/workflows/release.yml",
        "I-" + "1230",  # not an id of three digits
        "I-" + "12",
        "AI-" + "123",  # the letter has to stand alone
        PRIVATE_ORG,  # the organisation alone
    ],
)
def test_this_repository_and_ordinary_text_are_not_internal_references(text, private_org):
    assert "internal-reference" not in categories(text)


def test_the_private_repository_is_found_by_its_plain_name_in_any_case_and_in_any_file():
    word = "mono" + "repo"
    assert categories(f"the {word} drops an entry", "README.md") == {"internal-reference"}
    assert categories(f"The {word.capitalize()} stores it", "chart/impact-ai/README.md") == {"internal-reference"}
    assert categories(f"the {word} drops an entry", "python/src/frameworks.py") == {"internal-reference"}


# The acceptance examples of a plan and its word for a unit of delivery, put together at run time like the rest.
EXAMPLE = "AE" + "-17"
WORD = "incr" + "ement"


@pytest.mark.parametrize(
    ("text", "source"),
    [
        (f"The overhead gate ({EXAMPLE}): the budget", "python/tests/overhead/gate.py"),
        (f"kind end to end ({EXAMPLE}, AE-{3})", "Makefile"),
        (f"def test_{EXAMPLE.lower().replace('-', '')}_ingest_down_does_not_touch_the_workload(world):", "e2e/x.py"),
        (f"The values this {WORD} adds", "chart/tests/test_chart_schema.py"),
        (f"one {WORD.upper()}S list", "README.md"),
    ],
)
def test_the_ids_and_words_of_a_plan_are_found_in_any_file(text, source):
    assert categories(text, source) == {"internal-reference"}


@pytest.mark.parametrize(
    "text",
    [
        "the setting is incremental",  # another word
        "MAE-17 of the model",  # not an id that stands alone
        "def test_aes_key_is_refused():",  # no number after test_ae
        "contest_ae7_x",  # not the start of a test name
        "sha256:0d7639c3287d4efcdc2c8837f1854d1ecf27aae761714ba3d95e61fe8eb48db3",  # ae7 inside a digest
    ],
)
def test_ordinary_text_that_looks_like_a_plan_is_not_found(text):
    assert "internal-reference" not in categories(text)


def test_the_paths_of_the_private_organisation_are_found_in_image_names_urls_and_module_paths(private_org):
    for text in (
        f"ghcr.io/{PRIVATE_ORG}/example-kit/python:1.0",
        f"oci://ghcr.io/{PRIVATE_ORG}/example-kit/charts/example-chart",
        f"module github.com/{PRIVATE_ORG}/example-kit/reporter",
        f"--repo {PRIVATE_ORG}/example-kit",
    ):
        assert categories(text) == {"internal-reference"}, text


def test_the_check_can_scan_the_tree_of_another_repository(tmp_path, private_org_file):
    published = tmp_path / "published"
    published.mkdir()
    subprocess.run(["git", "init", "-q", str(published)], check=True)  # noqa: S603, S607
    (published / "README.md").write_text("see github.com/impact-ai/impact-autoinstrumentation\n")
    arguments = ["--source", "--root", str(published), "--denylist", str(private_org_file)]
    assert check.main(arguments) == 0
    (published / "NOTES.md").write_text(f"built in {PRIVATE_ORG}/example-kit\n")
    assert check.main(arguments) == 1
    assert check.main(["--source", "--root", str(published)]) == 0, "without the deny-list it is not known"


def test_the_check_can_scan_a_commit_message_on_standard_input(monkeypatch, capsys, private_org_file):
    arguments = ["--stdin", "--denylist", str(private_org_file)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("Release 0.3.0\n"))
    assert check.main(arguments) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"Release 0.3.0\n\nfrom {PLAN_ID} in {PRIVATE_ORG}/x\n"))
    assert check.main(arguments) == 1
    out = capsys.readouterr().out
    assert "stdin:3: internal-reference: " + PLAN_ID in out and f"stdin:3: internal-reference: {PRIVATE_ORG}/" in out


def test_findings_say_where_and_what():
    (finding,) = check.scan_text("a/b.md", "one\ntwo " + EMAIL + "\nthree")
    assert (finding.source, finding.line, finding.category, finding.match) == ("a/b.md", 2, "email", EMAIL)


def test_the_documentation_account_ids_are_allowed_everywhere_but_a_real_one_is_not():
    assert check.violations(check.scan_text("README.md", "cloud.accountId=012345678901")) == []
    assert check.violations(check.scan_text("README.md", "arn:aws:eks:eu-west-1:123456789012:cluster/x")) == []
    assert [f.category for f in check.violations(check.scan_text("README.md", ACCOUNT))] == ["aws-account-id"]


def test_a_fixture_value_is_allowed_in_its_own_file_only():
    fixture = "python/tests/image/conftest.py"
    text = "OTEL_POD_IP = " + ".".join((A, B, C, "18"))
    assert check.violations(check.scan_text(fixture, text)) == []
    assert [f.category for f in check.violations(check.scan_text("README.md", text))] == ["private-ip"]
    assert [f.category for f in check.violations(check.scan_text(fixture, "OTHER = " + PRIVATE["172"]))] == [
        "private-ip"
    ]


def test_the_allowlist_is_small_explicit_and_explained():
    allow = check.ALLOWLIST
    assert 0 < len(allow) <= 14
    for entry in allow:
        assert entry.reason.strip(), entry
        assert entry.category in check.CATEGORIES, entry
        assert entry.paths != ("*",) or entry.category == "aws-account-id", (
            entry
        )  # only a placeholder is allowed anywhere


def test_every_allowlist_entry_is_needed_by_something_in_the_repository():
    # An entry for a fixture that is gone is a hole nobody watches.
    assert check.stale(check.scan_source(ROOT) + check.scan_chart(ROOT)) == []


def test_an_entry_nothing_needs_is_reported():
    assert [e.pattern for e in check.stale([])] == [e.pattern for e in check.ALLOWLIST]


def test_the_repository_holds_nothing_internal():
    found = check.violations(check.scan_source(ROOT))
    assert [(f.source, f.line, f.category, f.match) for f in found] == []


def test_a_new_file_is_scanned_before_it_is_added_but_an_ignored_one_is_not(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "new.txt").write_text("contact " + EMAIL)
    (tmp_path / "ignored.txt").write_text("contact " + EMAIL)
    assert [(f.source, f.category) for f in check.scan_source(tmp_path)] == [("new.txt", "email")]


def test_another_copy_of_the_chart_is_rendered_and_scanned_too(tmp_path):
    # The release stamps a copy of the chart and checks that copy before it is pushed.
    chart = tmp_path / "impact-ai"
    shutil.copytree(ROOT / "chart" / "impact-ai", chart)
    assert check.violations(check.scan_chart(chart=chart)) == []
    leak = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: leak\ndata:\n  host: " + INTERNAL_HOST + "\n"
    (chart / "templates" / "leak.yaml").write_text(leak)
    assert {f.category for f in check.violations(check.scan_chart(chart=chart))} == {"internal-host"}


def docker_archive(files: dict[str, str]) -> bytes:
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        for name in files:
            info = tarfile.TarInfo(name)
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w") as tar:
        for name, data in {
            "abc/layer.tar": layer.getvalue(),
            "cfg.json": json.dumps({"config": {"Env": ["PATH=/usr/bin"], "Labels": {"a": "b"}}}).encode(),
            "manifest.json": json.dumps(
                [{"Config": "cfg.json", "Layers": ["abc/layer.tar"], "RepoTags": ["x:1"]}]
            ).encode(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return outer.getvalue()


def test_the_file_list_of_an_image_archive_is_scanned(tmp_path):
    clean = tmp_path / "clean.tar"
    clean.write_bytes(docker_archive({"licenses/LICENSE": "", "autoinstrumentation/x.py": ""}))
    assert check.scan_archive(clean) == []
    dirty = tmp_path / "dirty.tar"
    dirty.write_bytes(docker_archive({"home/build/" + INTERNAL_HOST + "/cache": "", "licenses/LICENSE": ""}))
    found = check.scan_archive(dirty)
    assert [f.category for f in found] == ["internal-host"]
    assert found[0].source.startswith("image dirty.tar:")


def test_the_configuration_of_an_image_is_scanned_too(tmp_path):
    archive = tmp_path / "config.tar"
    data = docker_archive({"a": ""})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    members["cfg.json"] = json.dumps({"config": {"Env": ["API=" + TOKENS["github"]]}}).encode()
    with tarfile.open(archive, "w") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    assert [f.category for f in check.scan_archive(archive)] == ["token"]
