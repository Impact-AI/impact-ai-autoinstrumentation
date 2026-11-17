#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Build the Python layer of AWS Lambda, one archive per architecture, from the payload the Python image carries.

    python3 lambda/build_layer.py --out build/lambda [--arch amd64,arm64] [--version 0.4.0]

For each architecture it runs python/tools/assemble_payload.py (glibc, which is what Lambda runs) and lays the archive
out as Lambda extracts it into /opt:

    impact-ai-instrument               the wrapper AWS_LAMBDA_EXEC_WRAPPER names (lambda/impact-ai-instrument)
    impact-ai/lambda/sitecustomize.py  the start the wrapper puts first on PYTHONPATH (lambda/sitecustomize.py)
    impact-ai/python/                  the payload: impact_instrumentation, OpenTelemetry and the instrumentations
    impact-ai/licenses/                ours, and those of every distribution in the payload with their index
    impact-ai/layer.json               version, architecture and the Pythons it is built for

Nothing goes into /opt/python, which Lambda puts on every function's path: the function sees none of the layer's
packages unless the package starts. The payload is compiled to bytecode for every supported Python (unchecked hashes, as
in the image): /opt is read-only, and without it every cold start would compile the SDK again.

Beside each archive (impact-ai-python-<arch>.zip) it writes an SPDX 2.3 SBOM (.spdx.json) and the archive's SHA-256, and
for all of them SIZES.md and sizes.json: the archive and its unpacked size against Lambda's limits, and the bytes of
environment variables a function's change adds (lambda/environment.json) against its budget. The archive is built the
same way twice from the same inputs (sorted entries, fixed times and modes). Nothing is published.

Standard library only; uv provides the Pythons that compile the bytecode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "python"
LAMBDA_DIR = ROOT / "lambda"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "arm64"}  # the kit's names -> Lambda's
PYTHONS = ("3.11", "3.12", "3.13", "3.14")
WRAPPER = "impact-ai-instrument"
PREFIX = "impact-ai"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MIB = 1024 * 1024
# AWS Lambda's quotas (https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).
LIMITS = {
    "unzippedBytes": 250 * MIB,  # the function and all its layers, unpacked
    "zippedBytes": 50 * MIB,  # an archive uploaded directly
    "layers": 5,
    "environmentBytes": 4096,
}
PROJECT_URL = "https://github.com/Impact-AI/impact-ai-autoinstrumentation"


def archive_name(arch: str) -> str:
    return f"impact-ai-python-{ARCHITECTURES[arch]}.zip"


# --- the environment a function's change adds -----------------------------------------------------------------------


def environment_bytes(manifest: dict) -> tuple[int, list[dict]]:
    """The bytes the variables take at most in Lambda's configuration ("NAME":"VALUE", each), and one row each."""
    rows = []
    for variable in manifest["variables"]:
        value = variable.get("value")
        length = len(value.encode()) if value is not None else int(variable["maxValueBytes"])
        size = len(json.dumps(variable["name"])) + 1 + length + 2 + 1
        row = {"name": variable["name"], "valueBytes": length, "bytes": size}
        if variable.get("provisional"):
            row["provisional"] = True
        rows.append(row)
    return sum(row["bytes"] for row in rows), rows


# --- the tree --------------------------------------------------------------------------------------------------------


def assemble(arch: str, out: Path, *, version: str, pythons: list[str], uv: str) -> Path:
    """The payload of the image for this architecture (glibc), by the image's own script, unchanged."""
    command = [
        sys.executable, str(PYTHON_DIR / "tools" / "assemble_payload.py"),
        "--lock", str(PYTHON_DIR / "requirements.lock"), "--src", str(PYTHON_DIR / "src"), "--out", str(out),
        "--arch", arch, "--version", version, "--pythons", ",".join(pythons), "--variants", "glibc", "--uv", uv,
        "--license", str(ROOT / "LICENSE"), "--notice", str(ROOT / "NOTICE"),
        "--qualification", str(PYTHON_DIR / "qualification.json"),
    ]  # fmt: skip
    subprocess.run(command, check=True)  # noqa: S603 - our own script
    return out


def lay_out(payload: Path, tree: Path, *, arch: str, version: str, pythons: list[str]) -> Path:
    """The layer's files under `tree`, as Lambda extracts them into /opt."""
    if tree.exists():
        shutil.rmtree(tree)
    base = tree / PREFIX
    shutil.copytree(payload / "autoinstrumentation", base / "python", symlinks=False)
    (base / "lambda").mkdir(parents=True)
    shutil.copy2(LAMBDA_DIR / "sitecustomize.py", base / "lambda" / "sitecustomize.py")
    shutil.copy2(LAMBDA_DIR / WRAPPER, tree / WRAPPER)
    licenses = base / "licenses"
    licenses.mkdir()
    shutil.copy2(ROOT / "LICENSE", licenses / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", licenses / "NOTICE")
    shutil.copytree(payload / "licenses" / "python", licenses / "python")
    (base / "layer.json").write_text(
        json.dumps(
            {
                "schema": "impact.lambda.layer.v1",
                "version": version,
                "architecture": ARCHITECTURES[arch],
                "pythons": pythons,
                "wrapper": f"/opt/{WRAPPER}",
            },
            indent=2,
        )
        + "\n"
    )
    for path in [tree, *tree.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path == tree / WRAPPER else 0o644)
    return tree


def compile_bytecode(tree: Path, pythons: list[str], *, uv: str) -> None:
    """Unchecked-hash bytecode for every Python: Lambda's /opt is read-only, so nothing would be cached there. The
    bytecode names its source where Lambda puts it (/opt/...), not where it was built: the archive is reproducible."""
    targets = [str(tree / PREFIX / "python"), str(tree / PREFIX / "lambda")]
    for python in pythons:
        command = [
            uv, "run", "--no-project", "--no-config", "--python", python, "--", "python", "-I", "-m", "compileall",
            "-q", "--invalidation-mode", "unchecked-hash", "-s", str(tree), "-p", "/opt", *targets,
        ]  # fmt: skip
        subprocess.run(command, check=True)  # noqa: S603 - fixed tool and arguments
    for path in tree.rglob("*.pyc"):
        path.chmod(0o644)
    for path in tree.rglob("__pycache__"):
        path.chmod(0o755)


def write_zip(tree: Path, archive: Path) -> None:
    """The tree as a zip that is the same for the same inputs: sorted names, one time, the modes of the tree."""
    files = sorted(p for p in tree.rglob("*") if p.is_file())
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(tree).as_posix(), date_time=ZIP_TIME)
            info.create_system = 3  # unix: the modes below are read
            info.external_attr = (0o100000 | (path.stat().st_mode & 0o777)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, path.read_bytes(), compresslevel=9)


# --- checks and reports ----------------------------------------------------------------------------------------------


def check(archive: Path, pythons: list[str]) -> list[str]:
    """What a layer must hold for the wrapper to start the package; empty when it does."""
    problems = []
    with zipfile.ZipFile(archive) as layer:
        infos = {info.filename: info for info in layer.infolist()}
        wrapper = infos.get(WRAPPER)
        if wrapper is None:
            problems.append(f"no {WRAPPER}")
        elif (wrapper.external_attr >> 16) & 0o111 != 0o111:
            problems.append(f"{WRAPPER} is not executable")
        for name in (
            f"{PREFIX}/lambda/sitecustomize.py",
            f"{PREFIX}/python/impact_instrumentation/aws_lambda.py",
            f"{PREFIX}/python/impact_instrumentation/_registry.py",
            f"{PREFIX}/python/impact_instrumentation/_qualification.py",
            f"{PREFIX}/licenses/LICENSE",
            f"{PREFIX}/licenses/python/index.json",
        ):
            if name not in infos:
                problems.append(f"no {name}")
        for python in pythons:
            tag = "cpython-" + python.replace(".", "")
            name = f"{PREFIX}/python/impact_instrumentation/__pycache__/aws_lambda.{tag}.pyc"
            if name not in infos:
                problems.append(f"no bytecode for Python {python}")
        outside = sorted({n.split("/")[0] for n in infos} - {WRAPPER, PREFIX})
        if outside:
            problems.append(f"files outside {PREFIX}/ and the wrapper: {outside}")
    return problems


def sizes(archive: Path, pythons: list[str]) -> dict:
    with zipfile.ZipFile(archive) as layer:
        infos = layer.infolist()
    unzipped = sum(info.file_size for info in infos)
    per_python = {}
    for python in pythons:
        tag = ".cpython-" + python.replace(".", "") + "."
        per_python[python] = sum(info.file_size for info in infos if tag in info.filename)
    return {
        "archive": archive.name,
        "zippedBytes": archive.stat().st_size,
        "unzippedBytes": unzipped,
        "files": len(infos),
        "bytecodeBytesPerPython": per_python,
        "roomLeftBytes": LIMITS["unzippedBytes"] - unzipped,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def sbom(index: list[dict], *, version: str, arch: str, archive: str, sha256: str, created: str) -> dict:
    """An SPDX 2.3 document of the layer: the archive, and every distribution in it by name, version and license."""
    layer_id = "SPDXRef-Package-impact-ai-lambda-python"
    packages = [
        {
            "SPDXID": layer_id,
            "name": f"impact-ai-lambda-python-{ARCHITECTURES[arch]}",
            "versionInfo": version,
            "packageFileName": archive,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseDeclared": "Apache-2.0",
            "licenseConcluded": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "supplier": "Organization: Impact AI",
            "checksums": [{"algorithm": "SHA256", "checksumValue": sha256}],
        }
    ]
    relationships = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": layer_id}
    ]
    for number, dist in enumerate(sorted(index, key=lambda d: (d["name"].lower(), d["version"])), start=1):
        spdx_id = f"SPDXRef-Package-python-{number}"
        packages.append(
            {
                "SPDXID": spdx_id,
                "name": dist["name"],
                "versionInfo": dist["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": dist["license"] if _simple_license(dist["license"]) else "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{dist['name'].lower()}@{dist['version']}",
                    }
                ],
            }
        )
        relationships.append({"spdxElementId": layer_id, "relationshipType": "CONTAINS", "relatedSpdxElement": spdx_id})
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"impact-ai-lambda-python-{ARCHITECTURES[arch]}-{version}",
        "documentNamespace": f"{PROJECT_URL}/lambda/python/{ARCHITECTURES[arch]}/{version}/{sha256}",
        "creationInfo": {"created": created, "creators": ["Tool: lambda/build_layer.py"]},
        "packages": packages,
        "relationships": relationships,
    }


def _simple_license(expression: str) -> bool:
    """An SPDX expression the document can carry as it is (letters, digits, '.', '-', '+' and the operators)."""
    words = expression.replace("(", " ").replace(")", " ").split()
    return bool(words) and all(w in ("AND", "OR", "WITH") or all(c.isalnum() or c in ".-+" for c in w) for w in words)


def render(report: dict) -> str:
    def mib(value: int) -> str:
        return f"{value / MIB:.1f} MiB"

    rows = []
    for layer in report["layers"]:
        python_bytes = ", ".join(f"{p}: {mib(b)}" for p, b in layer["bytecodeBytesPerPython"].items())
        rows.append(
            f"| {layer['architecture']} | {mib(layer['zippedBytes'])} | {mib(layer['unzippedBytes'])} | "
            f"{mib(layer['roomLeftBytes'])} | {python_bytes} |"
        )
    environment = report["environment"]
    variables = "\n".join(
        f"| `{row['name']}`{' (provisional)' if row.get('provisional') else ''} | {row['valueBytes']} | "
        f"{row['bytes']} |"
        for row in environment["rows"]
    )
    limits = report["limits"]
    return (
        f"# The Python layer for AWS Lambda, {report['version']}\n\n"
        "One archive per architecture, for Python "
        f"{' to '.join([report['pythons'][0], report['pythons'][-1]])}, with the bytecode of every one of them. Lambda "
        f"allows {limits['layers']} layers and {mib(limits['unzippedBytes'])} unpacked for a function and all its "
        f"layers together, and {mib(limits['zippedBytes'])} for an archive uploaded directly. The room left is what "
        "the function's own code and its other layers can take beside this one.\n\n"
        "| Architecture | Archive | Unpacked | Room left | Bytecode per Python |\n|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n\n## Environment variables a function's change adds\n\n"
        f"At most {environment['bytes']} bytes of Lambda's {limits['environmentBytes']} (budget "
        f"{environment['budgetBytes']}), counting each value at the longest it may be.\n\n"
        "| Variable | Value bytes | Bytes |\n|---|---|---|\n"
        + variables
        + "\n"
        + (
            "\nA provisional variable is not for a customer's function yet: lambda/environment.json says why.\n"
            if any(row.get("provisional") for row in environment["rows"])
            else ""
        )
    )


def report_for(layers: list[dict], manifest: dict, *, version: str, pythons: list[str]) -> dict:
    total, rows = environment_bytes(manifest)
    return {
        "schema": "impact.lambda.sizes.v1",
        "version": version,
        "pythons": pythons,
        "limits": LIMITS,
        "layers": layers,
        "environment": {"bytes": total, "budgetBytes": manifest["budgetBytes"], "rows": rows},
        "ok": total <= manifest["budgetBytes"]
        and all(layer["unzippedBytes"] < LIMITS["unzippedBytes"] for layer in layers)
        and all(layer["zippedBytes"] < LIMITS["zippedBytes"] for layer in layers),
    }


def build(arch: str, out: Path, *, version: str, pythons: list[str], uv: str, created: str) -> dict:
    with tempfile.TemporaryDirectory() as scratch:
        payload = assemble(arch, Path(scratch) / "payload", version=version, pythons=pythons, uv=uv)
        tree = lay_out(payload, Path(scratch) / "layer", arch=arch, version=version, pythons=pythons)
        compile_bytecode(tree, pythons, uv=uv)
        archive = out / archive_name(arch)
        write_zip(tree, archive)
        index = json.loads((tree / PREFIX / "licenses" / "python" / "index.json").read_text())
    problems = check(archive, pythons)
    if problems:
        raise SystemExit(f"{archive}: {'; '.join(problems)}")
    found = sizes(archive, pythons)
    found["architecture"] = ARCHITECTURES[arch]
    document = sbom(index, version=version, arch=arch, archive=archive.name, sha256=found["sha256"], created=created)
    archive.with_suffix(".spdx.json").write_text(json.dumps(document, indent=2) + "\n")
    archive.with_name(archive.name + ".sha256").write_text(f"{found['sha256']}  {archive.name}\n")
    return found


def created_time() -> str:
    """SOURCE_DATE_EPOCH when the build sets it, so that the SBOM is reproducible too; else now."""
    import datetime

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = (
        datetime.datetime.fromtimestamp(int(epoch), tz=datetime.UTC) if epoch else datetime.datetime.now(datetime.UTC)
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--arch", default="amd64,arm64", help="comma-separated: amd64, arm64")
    parser.add_argument("--version", default="0.0.0+dev")
    parser.add_argument("--pythons", default=",".join(PYTHONS))
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args(argv)
    pythons = args.pythons.split(",")
    args.out.mkdir(parents=True, exist_ok=True)
    created = created_time()
    layers = [
        build(arch, args.out, version=args.version, pythons=pythons, uv=args.uv, created=created)
        for arch in args.arch.split(",")
    ]
    manifest = json.loads((LAMBDA_DIR / "environment.json").read_text())
    report = report_for(layers, manifest, version=args.version, pythons=pythons)
    (args.out / "sizes.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.out / "SIZES.md").write_text(render(report))
    for layer in layers:
        print(
            f"{layer['archive']}: {layer['zippedBytes'] / MIB:.1f} MiB, {layer['unzippedBytes'] / MIB:.1f} MiB "
            f"unpacked, {layer['files']} files",
            file=sys.stderr,
        )
    print(f"environment: at most {report['environment']['bytes']} bytes of {manifest['budgetBytes']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
