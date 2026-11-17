# SPDX-License-Identifier: Apache-2.0

"""The assembled payload, for every target the image is built for (needs network for the wheels, no containers).

glibc and musl, amd64 and arm64: the right wheels, nothing forbidden, every claimed Python version has its compiled
wrapt extension, files readable by any user.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import assemble_payload as assemble  # noqa: E402

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
TARGETS = [(variant, arch) for variant in ("glibc", "musl") for arch in ("amd64", "arm64")]


@pytest.fixture(scope="session")
def payloads(tmp_path_factory):
    out = tmp_path_factory.mktemp("payload")
    built = {}
    for arch in ("amd64", "arm64"):
        target = out / arch
        subprocess.run(  # noqa: S603
            [
                sys.executable, str(ROOT / "tools" / "assemble_payload.py"),
                "--lock", str(ROOT / "requirements.lock"), "--src", str(ROOT / "src"),
                "--out", str(target), "--arch", arch, "--version", "9.9.9", "--pythons", ",".join(PYTHONS),
                "--license", str(ROOT.parent / "LICENSE"), "--notice", str(ROOT.parent / "NOTICE"),
                "--qualification", str(ROOT / "qualification.json"),
            ],
            check=True,
            cwd=ROOT,
        )  # fmt: skip
        built[("glibc", arch)] = target / "autoinstrumentation"
        built[("musl", arch)] = target / "autoinstrumentation-musl"
        built[("licenses", arch)] = target / "licenses" / "python"
    return built


def dist_names(root: Path) -> set[str]:
    names = set()
    for info in root.glob("**/*.dist-info"):
        names.add(assemble.normalize(re.sub(r"-\d.*$", "", info.name.removesuffix(".dist-info"))))
    return names


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_no_provider_sdk_and_no_shadowing_http_stack(payloads, variant, arch):
    root = payloads[(variant, arch)]
    assert dist_names(root) & assemble.FORBIDDEN_DISTRIBUTIONS == set()
    top_level = {p.name for p in root.rglob("*") if p.is_dir() and p.parent in (root, root / assemble.SHARED_DIR)}
    for module in ("openai", "anthropic", "boto3", "botocore", "requests", "urllib3", "certifi", "idna", "google"):
        assert module not in top_level


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_layout_matches_the_operator_contract(payloads, variant, arch):
    root = payloads[(variant, arch)]
    assert (root / "opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py").is_file()
    assert (
        "impact_instrumentation"
        in (root / "opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py").read_text()
    )
    assert (root / "impact_instrumentation/bootstrap.py").is_file()
    assert (root / "impact_instrumentation/_version.py").read_text().strip() == '__version__ = "9.9.9"'
    assert (root / "impact_instrumentation-9.9.9.dist-info/METADATA").is_file()
    assert not (root / "bin").exists()
    assert not list(root.rglob("__pycache__"))


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_shared_libraries_are_separated_from_the_core(payloads, variant, arch):
    root = payloads[(variant, arch)]
    shared = root / assemble.SHARED_DIR
    assert dist_names(shared) == assemble.SHARED
    for name in ("wrapt", "packaging", "asgiref", "typing_extensions.py"):
        assert (shared / name).exists()
        assert not (root / name).exists()


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_compiled_wrapt_exists_for_every_claimed_python(payloads, variant, arch):
    wrapt = payloads[(variant, arch)] / assemble.SHARED_DIR / "wrapt"
    tag = f"{assemble.ARCH_TAGS[arch]}-{assemble.EXTENSION_TAGS[variant]}"
    for version in PYTHONS:
        assert (wrapt / f"_wrappers.cpython-{version.replace('.', '')}-{tag}.so").is_file()
    # And nothing of another architecture or libc leaked in.
    assert {p.name for p in wrapt.glob("*.so")} == {f"_wrappers.cpython-{v.replace('.', '')}-{tag}.so" for v in PYTHONS}


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_every_file_is_readable_by_any_user_and_nothing_is_writable_by_others(payloads, variant, arch):
    root = payloads[(variant, arch)]
    for path in [root, *root.rglob("*")]:
        mode = path.stat().st_mode
        assert mode & stat.S_IROTH, path
        assert not mode & (stat.S_IWGRP | stat.S_IWOTH), path
        if path.is_dir():
            assert mode & stat.S_IXOTH, path


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_payload_is_small(payloads, variant, arch):
    total = sum(p.stat().st_size for p in payloads[(variant, arch)].rglob("*") if p.is_file())
    assert total < 15 * 1024 * 1024


def test_versions_match_the_lock(payloads):
    lock = assemble.parse_lock((ROOT / "requirements.lock").read_text())
    root = payloads[("glibc", "amd64")]
    for name, (version, _) in lock.items():
        folder = root if name not in assemble.SHARED else root / assemble.SHARED_DIR
        found = [
            p.name
            for p in folder.glob("*.dist-info")
            if assemble.normalize(re.sub(r"-\d.*$", "", p.name[:-10])) == name
        ]
        assert found, name
        assert found[0].endswith(f"-{version}.dist-info"), (name, found)


def test_missing_extension_fails_the_build(tmp_path):
    shared = tmp_path / "shared"
    (shared / "wrapt").mkdir(parents=True)
    (shared / "wrapt" / "_wrappers.cpython-312-x86_64-linux-gnu.so").write_text("")
    with pytest.raises(SystemExit, match="cpython-311"):
        assemble.check_extensions(shared, ["3.11", "3.12"], "glibc", "amd64")


def test_the_payload_imports_on_this_interpreter(payloads):
    """The glibc amd64 payload loads under the interpreter running the tests (3.13) with a clean environment."""
    root = payloads[("glibc", "amd64")]
    code = (
        "import sys, json\n"
        f"sys.path[:0] = [{str(root)!r}]\n"
        f"sys.path.append({str(root / assemble.SHARED_DIR)!r})\n"
        "import wrapt, opentelemetry.sdk.trace\n"
        "import opentelemetry.instrumentation.asgi, opentelemetry.instrumentation.wsgi\n"
        "print(json.dumps({'wrapt_c': wrapt.ObjectProxy.__module__, 'ext': 'wrapt._wrappers' in sys.modules}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ext"] is True


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_the_registry_lists_every_instrumentation_with_its_library_range(payloads, variant, arch):
    source = (payloads[(variant, arch)] / "impact_instrumentation" / "_registry.py").read_text()
    namespace: dict = {}
    exec(source, namespace)  # noqa: S102 - generated by the assembler under test
    got = {entry[0]: entry[3] for entry in namespace["INSTRUMENTATIONS"]}
    assert got == {
        "anthropic": (("anthropic", "<3,>=0.51.0"),),
        "boto3": (("boto3", ""),),  # OpenLLMetry's metadata names the library without a range
        "django": (("django", ">=2.0"),),
        "falcon": (("falcon", "<5.0.0,>=1.4.1"),),
        "fastapi": (("fastapi", "~=0.92"),),
        "flask": (("flask", ">=1.0"),),
        "google-genai": (("google-genai", "<3,>=1.32.0"),),  # imported as google.genai: runtime.MODULES
        "langchain": (("langchain", "<2,>=0.3.22"),),  # runtime.ALTERNATIVES adds langchain-core
        "openai": (("openai", "<4,>=1.26.0"),),
        "openai_agents": (("openai-agents", "<1,>=0.3.3"),),  # imported as `agents`: runtime.MODULES
        "pyramid": (("pyramid", ">=1.7"),),
        "starlette": (("starlette", ">=0.13"),),
    }


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_the_payload_carries_the_qualification_and_the_package_reads_the_same_table(payloads, variant, arch):
    root = payloads[(variant, arch)]
    committed = json.loads((ROOT / "qualification.json").read_text())
    assert json.loads((root / "qualification.json").read_text()) == committed
    namespace: dict = {}
    exec((root / "impact_instrumentation" / "_qualification.py").read_text(), namespace)  # noqa: S102 - generated
    assert namespace["TABLE"] == committed


def test_a_qualification_that_is_not_one_stops_the_build(tmp_path):
    broken = tmp_path / "qualification.json"
    for text in ("{", "[]", '{"schema": "impact.nodejs.qualification.v1", "cells": [{}]}', '{"schema": "x"}'):
        broken.write_text(text)
        with pytest.raises(SystemExit, match="qualification"):
            assemble.read_qualification(broken)
    broken.write_text(json.dumps({"schema": assemble.QUALIFICATION_SCHEMA, "cells": []}))
    with pytest.raises(SystemExit, match="with cells"):
        assemble.read_qualification(broken)


def test_the_generated_table_is_the_qualification_as_a_literal():
    table = {"schema": assemble.QUALIFICATION_SCHEMA, "cells": [{"steps": None, "traced": True}], "excluded": []}
    namespace: dict = {}
    exec(assemble.generate_qualification(table), namespace)  # noqa: S102 - the generator's own output
    assert namespace["TABLE"] == table


@pytest.mark.parametrize(("variant", "arch"), TARGETS)
def test_the_agent_instrumentations_load_without_the_frameworks(payloads, variant, arch):
    """The payload has the instrumentation of the Agents SDK and of LangChain, not the frameworks: the application's own
    copies are the only ones, and importing an instrumentor before the framework exists is an ImportError we catch."""
    root = payloads[(variant, arch)]
    for module in ("openai_agents", "langchain"):
        assert (root / "opentelemetry" / "instrumentation" / "genai" / module / "__init__.py").is_file()
    for framework in ("agents", "langchain", "langchain_core", "langgraph", "openai"):
        assert not (root / framework).exists()


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_every_distribution_in_the_payload_has_its_license_and_an_index_entry(payloads, arch):
    licenses = payloads[("licenses", arch)]
    index = json.loads((licenses / "index.json").read_text())
    listed = {(assemble.normalize(d["name"]), d["version"]) for d in index}
    for variant in ("glibc", "musl"):
        root = payloads[(variant, arch)]
        found = set()
        for info in assemble.distributions(root):
            name, _, version = info.name[: -len(".dist-info")].partition("-")
            found.add((assemble.normalize(name), version))
        assert found == listed, variant
    for entry in index:
        folder = licenses / f"{entry['name'].replace('-', '_')}-{entry['version']}"
        assert entry["files"] and all((folder / f).is_file() for f in entry["files"]), entry
        assert entry["license"] != "UNKNOWN", entry
    assert (licenses / "INDEX.md").read_text().startswith("# Python packages in this image")


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_the_index_names_our_own_distribution_and_supplies_the_text_openllmetry_leaves_out(payloads, arch):
    by_name = {d["name"]: d for d in json.loads((payloads[("licenses", arch)] / "index.json").read_text())}
    assert by_name["impact-instrumentation"]["license"] == "Apache-2.0"
    assert by_name["impact-instrumentation"]["files"] == ["LICENSE", "NOTICE"]
    for name in ("opentelemetry-instrumentation-bedrock", "opentelemetry-semantic-conventions-ai"):
        assert by_name[name]["supplied"] is True, name
    assert by_name["opentelemetry-instrumentation-genai-langchain"]["files"] == ["LICENSE", "NOTICE"]
    assert {"wrapt", "packaging", "asgiref", "typing-extensions"} <= {n.replace("_", "-") for n in by_name}


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_no_email_address_is_in_the_license_index(payloads, arch):
    for name in ("INDEX.md", "index.json"):
        assert "@" not in (payloads[("licenses", arch)] / name).read_text()
