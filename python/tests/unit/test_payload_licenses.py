# SPDX-License-Identifier: Apache-2.0
"""The licenses of what the Python image bundles are collected by the build, from each distribution's own metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import assemble_payload as assemble  # noqa: E402

APACHE = "Apache License\nVersion 2.0, January 2004\n(the canonical text, here a stand-in)\n"


def dist(root: Path, name: str, version: str, *, files: dict[str, str] | None = None, **metadata: str) -> Path:
    """A site-packages directory entry: <name>-<version>.dist-info with METADATA and the given files."""
    info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    lines += [f"{key.replace('_', '-')}: {value}" for key, value in metadata.items()]
    (info / "METADATA").write_text("\n".join(lines) + "\n")
    for relative, text in (files or {}).items():
        target = info / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return info


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "autoinstrumentation"
    (root / assemble.SHARED_DIR).mkdir(parents=True)
    return root


def collect(payloads, tmp_path, **kwargs):
    out = tmp_path / "licenses"
    index = assemble.write_licenses(payloads, out, apache_text=APACHE, **kwargs)
    return out, index


def test_a_pep_639_license_file_is_copied_and_indexed(payload, tmp_path):
    dist(payload, "wrapt", "2.5.2", files={"licenses/LICENSE": "BSD text"}, License_Expression="BSD-2-Clause")
    out, index = collect([payload], tmp_path)
    assert (out / "python" / "wrapt-2.5.2" / "LICENSE").read_text() == "BSD text"
    assert index == [
        {
            "name": "wrapt",
            "version": "2.5.2",
            "license": "BSD-2-Clause",
            "authors": [],
            "files": ["LICENSE"],
            "supplied": False,
        }
    ]


def test_legacy_top_level_license_and_notice_files_are_copied_too(payload, tmp_path):
    dist(
        payload,
        "legacy",
        "1.0",
        files={"LICENSE.txt": "MIT text", "NOTICE": "a notice", "RECORD": "not a license", "top_level.txt": "x"},
        License="MIT",
    )
    out, index = collect([payload], tmp_path)
    assert sorted(p.name for p in (out / "python" / "legacy-1.0").iterdir()) == ["LICENSE.txt", "NOTICE"]
    assert index[0]["files"] == ["LICENSE.txt", "NOTICE"]


def test_the_shared_libraries_directory_is_scanned_as_well(payload, tmp_path):
    dist(
        payload / assemble.SHARED_DIR,
        "asgiref",
        "3.13.0",
        files={"licenses/LICENSE": "x"},
        License_Expression="BSD-3-Clause",
    )
    dist(payload, "core", "1.0", files={"licenses/LICENSE": "y"}, License_Expression="Apache-2.0")
    _, index = collect([payload], tmp_path)
    assert [(d["name"], d["version"]) for d in index] == [("asgiref", "3.13.0"), ("core", "1.0")]


def test_an_apache_distribution_without_a_license_file_gets_the_canonical_text(payload, tmp_path):
    # OpenLLMetry's wheels declare Apache-2.0 and ship no file.
    dist(payload, "opentelemetry-instrumentation-bedrock", "0.62.4", License_Expression="Apache-2.0")
    out, index = collect([payload], tmp_path)
    assert (out / "python" / "opentelemetry_instrumentation_bedrock-0.62.4" / "LICENSE").read_text() == APACHE
    assert index[0]["supplied"] is True
    assert index[0]["files"] == ["LICENSE"]


def test_a_distribution_without_a_license_file_and_a_license_we_have_no_text_for_fails_the_build(payload, tmp_path):
    dist(payload, "mystery", "1.0", License_Expression="MIT")
    with pytest.raises(SystemExit, match="mystery 1.0: no license file"):
        collect([payload], tmp_path)


def test_a_distribution_that_names_no_license_fails_the_build(payload, tmp_path):
    dist(payload, "silent", "1.0", files={"LICENSE": "text"})
    with pytest.raises(SystemExit, match="silent 1.0: no license"):
        collect([payload], tmp_path)


def test_a_license_field_that_holds_the_whole_text_is_not_taken_for_a_name(payload, tmp_path):
    dist(
        payload, "wordy", "1.0", files={"LICENSE": "x"}, License="Permission is hereby granted,\n        free of charge"
    )
    with pytest.raises(SystemExit, match="wordy 1.0: no license"):
        collect([payload], tmp_path)


@pytest.mark.parametrize(
    ("classifier", "spdx"),
    [
        ("License :: OSI Approved :: MIT License", "MIT"),
        ("License :: OSI Approved :: Apache Software License", "Apache-2.0"),
        ("License :: OSI Approved :: BSD License", "BSD-3-Clause"),
        ("License :: OSI Approved :: Python Software Foundation License", "PSF-2.0"),
    ],
)
def test_a_classifier_names_the_license_when_nothing_else_does(payload, tmp_path, classifier, spdx):
    dist(payload, "classified", "1.0", files={"LICENSE": "x"}, Classifier=classifier)
    _, index = collect([payload], tmp_path)
    assert index[0]["license"] == spdx


def test_authors_are_names_without_their_email_addresses(payload, tmp_path):
    dist(
        payload,
        "authored",
        "1.0",
        files={"LICENSE": "x"},
        License_Expression="MIT",
        Author_email="Gal Kleinman <gal@example.org>, Nir Gazit <nir@example.org>",
        Author="Jane Doe",
    )
    out, index = collect([payload], tmp_path)
    assert index[0]["authors"] == ["Gal Kleinman", "Jane Doe", "Nir Gazit"]
    assert "@" not in (out / "python" / "INDEX.md").read_text()
    assert "@" not in (out / "python" / "index.json").read_text()


def test_the_index_is_sorted_and_written_as_markdown_and_json(payload, tmp_path):
    for name in ("zeta", "alpha"):
        dist(payload, name, "1.0", files={"LICENSE": "x"}, License_Expression="MIT")
    out, index = collect([payload], tmp_path)
    assert [d["name"] for d in index] == ["alpha", "zeta"]
    assert json.loads((out / "python" / "index.json").read_text()) == index
    markdown = (out / "python" / "INDEX.md").read_text()
    assert markdown.index("alpha") < markdown.index("zeta")
    assert "| Package | Version | License | Authors | Files |" in markdown


def test_the_own_distribution_is_listed_with_the_license_and_notice_it_ships(tmp_path):
    root = tmp_path / "autoinstrumentation"
    root.mkdir()
    assemble.write_own_distribution(root, "1.2.3", license_text=APACHE, notice_text="Impact AI cluster kit\n")
    info = root / "impact_instrumentation-1.2.3.dist-info"
    assert "License-Expression: Apache-2.0" in (info / "METADATA").read_text()
    assert (info / "licenses" / "LICENSE").read_text() == APACHE
    assert (info / "licenses" / "NOTICE").read_text() == "Impact AI cluster kit\n"
    _, index = collect([root], tmp_path)
    assert index[0]["name"] == "impact-instrumentation" and index[0]["license"] == "Apache-2.0"
    assert index[0]["files"] == ["LICENSE", "NOTICE"] and index[0]["supplied"] is False


def test_the_glibc_and_musl_payloads_must_hold_the_same_distributions(tmp_path):
    glibc, musl = tmp_path / "glibc", tmp_path / "musl"
    for root in (glibc, musl):
        (root / assemble.SHARED_DIR).mkdir(parents=True)
        dist(root, "same", "1.0", files={"LICENSE": "x"}, License_Expression="MIT")
    dist(musl, "extra", "1.0", files={"LICENSE": "x"}, License_Expression="MIT")
    with pytest.raises(SystemExit, match="differ"):
        assemble.write_licenses([glibc, musl], tmp_path / "out", apache_text=APACHE)
