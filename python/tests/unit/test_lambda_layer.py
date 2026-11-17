# SPDX-License-Identifier: Apache-2.0

"""The layer's archive (lambda/build_layer.py): its layout, that it is the same for the same inputs, its SBOM, the size
report against Lambda's limits, and the environment a function's change adds (at most 512 bytes)."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PYTHONS = ["3.11", "3.12", "3.13", "3.14"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_layer = load("build_layer", ROOT / "lambda" / "build_layer.py")
check_internal = load("check_internal_details", ROOT / "scripts" / "check_internal_details.py")


def fake_payload(tmp: Path) -> Path:
    """What assemble_payload.py leaves for one architecture: the glibc payload and the licenses of what it holds."""
    payload = tmp / "payload"
    package = payload / "autoinstrumentation" / "impact_instrumentation"
    package.mkdir(parents=True)
    for name in ("__init__.py", "aws_lambda.py", "_registry.py", "_qualification.py"):
        (package / name).write_text(f"# {name}\n")
    (package / "aws_lambda.py").chmod(0o444)  # the payload's modes are read-only; the layer's are its own
    licenses = payload / "licenses" / "python"
    licenses.mkdir(parents=True)
    index = [
        {"name": "opentelemetry-sdk", "version": "1.45.2", "license": "Apache-2.0", "authors": [], "files": []},
        {"name": "wrapt", "version": "2.0.1", "license": "BSD-2-Clause", "authors": [], "files": []},
        {"name": "odd", "version": "1.0", "license": "Some License, see file", "authors": [], "files": []},
    ]
    (licenses / "index.json").write_text(json.dumps(index))
    return payload


def fake_bytecode(tree: Path) -> None:
    cache = tree / "impact-ai" / "python" / "impact_instrumentation" / "__pycache__"
    cache.mkdir()
    for python in PYTHONS:
        (cache / f"aws_lambda.cpython-{python.replace('.', '')}.pyc").write_bytes(b"\x00" * 64)


@pytest.fixture
def tree(tmp_path):
    layer = build_layer.lay_out(
        fake_payload(tmp_path), tmp_path / "layer", arch="amd64", version="1.2.3", pythons=PYTHONS
    )
    fake_bytecode(layer)
    return layer


def test_the_layer_is_laid_out_as_lambda_extracts_it_into_opt(tree, tmp_path):
    archive = tmp_path / "out" / build_layer.archive_name("amd64")
    build_layer.write_zip(tree, archive)
    assert archive.name == "impact-ai-python-x86_64.zip"
    with zipfile.ZipFile(archive) as layer:
        infos = {info.filename: info for info in layer.infolist()}
        assert (infos["impact-ai-instrument"].external_attr >> 16) & 0o777 == 0o755
        assert (infos["impact-ai/python/impact_instrumentation/aws_lambda.py"].external_attr >> 16) & 0o777 == 0o644
        assert layer.read("impact-ai/lambda/sitecustomize.py") == (ROOT / "lambda" / "sitecustomize.py").read_bytes()
        assert layer.read("impact-ai/licenses/LICENSE") == (ROOT / "LICENSE").read_bytes()
        assert json.loads(layer.read("impact-ai/layer.json")) == {
            "schema": "impact.lambda.layer.v1",
            "version": "1.2.3",
            "architecture": "x86_64",
            "pythons": PYTHONS,
            "wrapper": "/opt/impact-ai-instrument",
        }
        # Nothing in /opt/python, which Lambda puts on every function's path.
        assert {name.split("/")[0] for name in infos} == {"impact-ai-instrument", "impact-ai"}
    assert build_layer.check(archive, PYTHONS) == []


def test_the_archive_is_the_same_for_the_same_inputs(tree, tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    build_layer.write_zip(tree, first)
    (tree / "impact-ai" / "layer.json").touch()  # a new time on a file changes nothing
    build_layer.write_zip(tree, second)
    assert first.read_bytes() == second.read_bytes()


def test_a_layer_without_its_wrapper_or_bytecode_fails_the_check(tree, tmp_path):
    (tree / "impact-ai-instrument").chmod(0o644)
    (tree / "impact-ai" / "python" / "impact_instrumentation" / "__pycache__" / "aws_lambda.cpython-314.pyc").unlink()
    (tree / "python").mkdir()
    (tree / "python" / "stray.py").write_text("")
    archive = tmp_path / "bad.zip"
    build_layer.write_zip(tree, archive)
    assert build_layer.check(archive, PYTHONS) == [
        "impact-ai-instrument is not executable",
        "no bytecode for Python 3.14",
        "files outside impact-ai/ and the wrapper: ['python']",
    ]


def test_the_environment_a_functions_change_adds_stays_within_512_bytes():
    manifest = json.loads((ROOT / "lambda" / "environment.json").read_text())
    total, rows = build_layer.environment_bytes(manifest)
    assert manifest["budgetBytes"] == 512
    assert total <= 512
    assert [row["name"] for row in rows] == [
        "AWS_LAMBDA_EXEC_WRAPPER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ]
    # The wrapper's value is the one the layer's archive puts there.
    assert manifest["variables"][0]["value"] == "/opt/" + build_layer.WRAPPER


def test_the_key_in_a_functions_configuration_is_provisional_until_its_delivery_is_decided():
    manifest = json.loads((ROOT / "lambda" / "environment.json").read_text())
    headers = next(v for v in manifest["variables"] if v["name"] == "OTEL_EXPORTER_OTLP_HEADERS")
    assert "must not write the key into the function's configuration" in headers["provisional"]
    assert [v["name"] for v in manifest["variables"] if v.get("provisional")] == ["OTEL_EXPORTER_OTLP_HEADERS"]
    _, rows = build_layer.environment_bytes(manifest)
    assert [row["name"] for row in rows if row.get("provisional")] == ["OTEL_EXPORTER_OTLP_HEADERS"]


def test_a_variable_is_counted_as_lambdas_configuration_holds_it():
    total, rows = build_layer.environment_bytes({"variables": [{"name": "AB", "value": "xyz"}]})
    assert total == len('"AB":"xyz",')
    assert rows == [{"name": "AB", "valueBytes": 3, "bytes": 11}]  # not provisional: no mark


def test_the_size_report_holds_each_archive_against_lambdas_limits(tree, tmp_path):
    archive = tmp_path / build_layer.archive_name("arm64")
    build_layer.write_zip(tree, archive)
    found = build_layer.sizes(archive, PYTHONS)
    assert found["zippedBytes"] == archive.stat().st_size
    assert found["bytecodeBytesPerPython"] == dict.fromkeys(PYTHONS, 64)
    assert found["roomLeftBytes"] == 250 * 1024 * 1024 - found["unzippedBytes"]
    found["architecture"] = "arm64"
    manifest = json.loads((ROOT / "lambda" / "environment.json").read_text())
    report = build_layer.report_for([found], manifest, version="1.2.3", pythons=PYTHONS)
    assert report["ok"] is True
    text = build_layer.render(report)
    assert "| arm64 |" in text
    assert "Python 3.11 to 3.14" in text
    assert f"At most {report['environment']['bytes']} bytes" in text
    assert "| `OTEL_EXPORTER_OTLP_HEADERS` (provisional) |" in text
    too_big = {**found, "unzippedBytes": 251 * 1024 * 1024}
    assert build_layer.report_for([too_big], manifest, version="1.2.3", pythons=PYTHONS)["ok"] is False


def test_the_sbom_names_the_archive_and_every_distribution_in_it(tmp_path):
    index = json.loads((fake_payload(tmp_path) / "licenses" / "python" / "index.json").read_text())
    document = build_layer.sbom(
        index, version="1.2.3", arch="arm64", archive="impact-ai-python-arm64.zip", sha256="ab" * 32, created="T"
    )
    assert document["spdxVersion"] == "SPDX-2.3"
    layer, *dists = document["packages"]
    assert layer["checksums"] == [{"algorithm": "SHA256", "checksumValue": "ab" * 32}]
    assert layer["packageFileName"] == "impact-ai-python-arm64.zip"
    assert [(d["name"], d["versionInfo"], d["licenseDeclared"]) for d in dists] == [
        ("odd", "1.0", "NOASSERTION"),
        ("opentelemetry-sdk", "1.45.2", "Apache-2.0"),
        ("wrapt", "2.0.1", "BSD-2-Clause"),
    ]
    assert dists[1]["externalRefs"][0]["referenceLocator"] == "pkg:pypi/opentelemetry-sdk@1.45.2"
    contains = [r for r in document["relationships"] if r["relationshipType"] == "CONTAINS"]
    assert len(contains) == 3


# Written in two pieces, so that this file holds no literal the check would find in it.
HOST = "build01.office" + ".lan"


def test_the_internal_details_check_reads_the_layers_own_files_and_every_name(tmp_path):
    archive = tmp_path / "layer.zip"
    with zipfile.ZipFile(archive, "w") as layer:
        layer.writestr("impact-ai/lambda/sitecustomize.py", f"HOST = '{HOST}'\n")
        layer.writestr("impact-ai/python/thirdparty/module.py", f"HOST = '{HOST}'\n")  # not ours: names only
        layer.writestr(f"impact-ai/python/thirdparty/{HOST}.txt", "")
    found = check_internal.scan_zip(archive)
    assert sorted((f.source.split(": ", 1)[1], f.match) for f in found) == [
        ("impact-ai/lambda/sitecustomize.py", HOST),
        (f"impact-ai/python/thirdparty/{HOST}.txt", HOST),
    ]
