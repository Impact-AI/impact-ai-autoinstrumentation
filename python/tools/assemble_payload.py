#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Assemble the payload of the Python image: what the Operator's init container copies into the application pod.

    /autoinstrumentation        glibc
    /autoinstrumentation-musl   musl (the workload sets instrumentation.opentelemetry.io/otel-python-platform: musl)

Everything is installed from requirements.lock by hash, for the target architecture, without running any code of the
target: `uv pip install --target --python-platform` fetches the matching wheels on the build machine. Only wrapt has
a compiled extension; it is installed once per supported Python version and the extensions are merged, because the
file name carries the interpreter version. The build fails if one of them is missing.

Next to the payload it writes licenses/python: the license and notice files of every distribution in it, copied from the
distribution's own dist-info, and an index (INDEX.md and index.json) of name, version, license and authors. A
distribution that declares Apache-2.0 and ships no license file gets the Apache License text beside it; any other
distribution without a license file, or without a license name, fails the build.

Standard library only, so it runs in any Python 3.11+ build image.
"""

from __future__ import annotations

import argparse
import email.parser
import email.utils
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Libraries the application is likely to have itself. They go to _impact_shared, which the package puts at the END
# of sys.path, so the application's own copy always wins and ours is only a fallback.
SHARED = frozenset({"wrapt", "typing-extensions", "packaging", "asgiref"})
SHARED_DIR = "_impact_shared"
SITECUSTOMIZE_DIR = Path("opentelemetry/instrumentation/auto_instrumentation")

PLATFORMS = {
    ("glibc", "amd64"): "x86_64-manylinux2014",
    ("glibc", "arm64"): "aarch64-manylinux2014",
    ("musl", "amd64"): "x86_64-unknown-linux-musl",
    ("musl", "arm64"): "aarch64-unknown-linux-musl",
}
VARIANT_DIRS = {"glibc": "autoinstrumentation", "musl": "autoinstrumentation-musl"}
EXTENSION_TAGS = {"glibc": "linux-gnu", "musl": "linux-musl"}
ARCH_TAGS = {"amd64": "x86_64", "arm64": "aarch64"}

# Packages and files that must never be in the payload.
FORBIDDEN_DISTRIBUTIONS = frozenset(
    {
        "openai",
        "anthropic",
        "boto3",
        "botocore",
        "requests",
        "urllib3",
        "certifi",
        "idna",
        "charset-normalizer",
        "protobuf",
    }
)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes) from a `uv pip compile --generate-hashes` file."""
    entries: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip().rstrip("\\").strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--hash=sha256:"):
            if current is None:
                raise ValueError(f"hash without a package: {line!r}")
            entries[current][1].append(stripped.removeprefix("--hash="))
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)(?:\s*;.*)?$", stripped)
        if not match:
            raise ValueError(f"cannot parse lock line: {line!r}")
        current = normalize(match[1])
        entries[current] = (match[2], [])
    unhashed = [name for name, (_, hashes) in entries.items() if not hashes]
    if unhashed:
        raise ValueError(f"packages without hashes: {unhashed}")
    return entries


def write_requirements(entries: dict[str, tuple[str, list[str]]], path: Path) -> None:
    lines = []
    for name in sorted(entries):
        version, hashes = entries[name]
        lines.append(f"{name}=={version} \\")
        lines.append(" \\\n".join(f"    --hash={h}" for h in hashes))
    path.write_text("\n".join(lines) + "\n")


def uv_install(uv: str, requirements: Path, target: Path, platform: str, python_version: str) -> None:
    command = [
        uv, "pip", "install", "--quiet", "--no-config", "--target", str(target),
        "--python-platform", platform, "--python-version", python_version,
        "--only-binary", ":all:", "--no-deps", "--require-hashes", "--requirement", str(requirements),
    ]  # fmt: skip
    subprocess.run(command, check=True)  # noqa: S603


def merge_extensions(sources: list[Path], target: Path) -> None:
    """Copy the per-interpreter extension modules (wrapt/_wrappers.cpython-3NN-*.so) from other installs."""
    for source in sources:
        for extension in source.rglob("*.so"):
            relative = extension.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extension, destination)


def check_extensions(shared: Path, python_versions: list[str], variant: str, arch: str) -> None:
    tag = f"{ARCH_TAGS[arch]}-{EXTENSION_TAGS[variant]}"
    missing = []
    for version in python_versions:
        short = version.replace(".", "")
        if not list((shared / "wrapt").glob(f"_wrappers.cpython-{short}-{tag}.so")):
            missing.append(f"wrapt/_wrappers.cpython-{short}-{tag}.so")
    if missing:
        raise SystemExit(f"compiled extensions missing for the claimed Python versions: {missing}")


_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>\(?[^;()]*\)?)\s*(?:;\s*(?P<marker>.*))?$"
)
_EXTRA = re.compile(r"extra\s*==\s*['\"](?P<extra>[A-Za-z0-9_.-]+)['\"]")


def generate_registry(root: Path) -> str:
    """Source of impact_instrumentation/_registry.py: the instrumentations installed under `root`.

    Reading package metadata at process start would import importlib.metadata (~200 ms) in every Python process of the
    container, so the metadata is read once, here. Each entry is
    (name, module, class, ((library, specifier), ...) all required, ((library, specifier), ...) any of).
    """
    import importlib.metadata

    entries = []
    for dist in importlib.metadata.distributions(path=[str(root)]):
        points = [ep for ep in dist.entry_points if ep.group == "opentelemetry_instrumentor"]
        if not points:
            continue
        required: list[tuple[str, str]] = []
        any_of: list[tuple[str, str]] = []
        for line in dist.requires or ():
            match = _REQUIREMENT.match(line)
            extra = _EXTRA.search(match["marker"] or "") if match else None
            if not match or not extra:
                continue
            requirement = (match["name"], match["spec"].strip().strip("()").replace(" ", ""))
            if extra["extra"] == "instruments":
                required.append(requirement)
            elif extra["extra"] == "instruments-any":
                any_of.append(requirement)
        for point in points:
            module, _, cls = point.value.partition(":")
            entries.append((point.name, module, cls, tuple(required), tuple(any_of)))
    entries.sort()
    body = "".join(f"    {entry!r},\n" for entry in entries)
    return f"# Generated by tools/assemble_payload.py; do not edit.\nINSTRUMENTATIONS = (\n{body})\n"


LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)", re.IGNORECASE)
CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}
APACHE = "Apache-2.0"


def distributions(root: Path) -> list[Path]:
    """The dist-info directories of a payload: its own and those of the shared libraries."""
    return sorted([*root.glob("*.dist-info"), *(root / SHARED_DIR).glob("*.dist-info")])


def license_name(meta) -> str | None:
    """The SPDX name the distribution gives itself: its expression, a short License field, else a classifier."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 100 and field.upper() != "UNKNOWN":
        return field
    for classifier in meta.get_all("Classifier", []):
        if classifier in CLASSIFIER_LICENSES:
            return CLASSIFIER_LICENSES[classifier]
    return None


def author_names(meta) -> list[str]:
    """Who wrote it, as names only: an email address of a third party does not belong in an index we publish."""
    names = set()
    for key in ("Author", "Maintainer"):
        names.update(value.strip() for value in meta.get_all(key, []) if value.strip() and "@" not in value)
    for key in ("Author-email", "Maintainer-email"):
        for name, _address in email.utils.getaddresses(meta.get_all(key, [])):
            if name.strip():
                names.add(name.strip())
    return sorted(names)


def license_files(info: Path) -> dict[str, Path]:
    """The license and notice files of a dist-info (PEP 639's licenses/ directory first, then the top level)."""
    found: dict[str, Path] = {}
    for path in sorted((info / "licenses").rglob("*")) if (info / "licenses").is_dir() else []:
        if path.is_file():
            found[str(path.relative_to(info / "licenses"))] = path
    for path in sorted(info.iterdir()):
        if path.is_file() and LICENSE_FILE.match(path.name):
            if path.name in found and found[path.name].read_bytes() != path.read_bytes():
                raise SystemExit(f"{info.name}: two different files called {path.name}")
            found.setdefault(path.name, path)
    return found


def write_licenses(payloads: list[Path], out: Path, *, apache_text: str) -> list[dict]:
    """Copy the license files of every distribution in the payloads to out/python and write the index."""
    listings = []
    for root in payloads:
        listing = {}
        for info in distributions(root):
            meta = email.parser.Parser().parsestr((info / "METADATA").read_text(errors="replace"), headersonly=True)
            listing[(normalize(meta["Name"]), meta["Version"])] = (info, meta)
        listings.append(listing)
    for other in listings[1:]:
        if other.keys() != listings[0].keys():
            raise SystemExit(f"the payloads differ in their distributions: {sorted(other.keys() ^ listings[0].keys())}")

    target = out / "python"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    index = []
    for key in sorted(listings[0]):
        info, meta = listings[0][key]
        name, version = meta["Name"], meta["Version"]
        spdx = license_name(meta)
        if not spdx:
            raise SystemExit(
                f"{name} {version}: no license name in its metadata (License-Expression, License or a classifier)"
            )
        files = license_files(info)
        supplied = False
        folder = target / info.name[: -len(".dist-info")]
        folder.mkdir()
        if not files:
            if spdx != APACHE:
                raise SystemExit(
                    f"{name} {version}: no license file in its dist-info, and no text to supply for {spdx}"
                )
            (folder / "LICENSE").write_text(apache_text)
            supplied, written = True, ["LICENSE"]
        else:
            written = sorted(files)
            for relative, source in files.items():
                (folder / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, folder / relative)
        index.append(
            {
                "name": name,
                "version": version,
                "license": spdx,
                "authors": author_names(meta),
                "files": written,
                "supplied": supplied,
            }
        )
    (target / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    rows = [
        f"| {d['name']} | {d['version']} | {d['license']} | {', '.join(d['authors']) or '-'} | "
        f"{', '.join(d['files'])}{' (supplied by the build)' if d['supplied'] else ''} |"
        for d in index
    ]
    (target / "INDEX.md").write_text(
        "# Python packages in this image\n\n"
        "The license and notice files of each package are in the directory named after it, next to this index.\n\n"
        "| Package | Version | License | Authors | Files |\n|---|---|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    return index


def write_own_distribution(root: Path, version: str, *, license_text: str, notice_text: str) -> None:
    info = root / f"impact_instrumentation-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: impact-instrumentation\n"
        f"Version: {version}\nSummary: impact.ai Python auto-instrumentation for the OpenTelemetry Operator\n"
        "Author: The Impact AI Authors\nLicense-Expression: Apache-2.0\nLicense-File: LICENSE\nLicense-File: NOTICE\n"
    )
    (info / "licenses").mkdir()
    (info / "licenses" / "LICENSE").write_text(license_text)
    (info / "licenses" / "NOTICE").write_text(notice_text)
    (info / "INSTALLER").write_text("assemble_payload\n")
    (info / "RECORD").write_text("")


def set_modes(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        mode = path.stat().st_mode
        path.chmod((mode | (0o555 if path.is_dir() else 0o444)) & ~0o022)


def assemble(args: argparse.Namespace, variant: str, scratch: Path) -> Path:
    lock = parse_lock(Path(args.lock).read_text())
    forbidden = FORBIDDEN_DISTRIBUTIONS & set(lock)
    if forbidden:
        raise SystemExit(f"the lock contains packages that must not be in the payload: {sorted(forbidden)}")
    platform = PLATFORMS[(variant, args.arch)]
    versions = args.pythons.split(",")

    core = {n: e for n, e in lock.items() if n not in SHARED}
    shared = {n: e for n, e in lock.items() if n in SHARED}
    work = scratch / variant
    work.mkdir()
    core_requirements, shared_requirements = work / "core.txt", work / "shared.txt"
    write_requirements(core, core_requirements)
    write_requirements(shared, shared_requirements)

    root = work / VARIANT_DIRS[variant]
    uv_install(args.uv, core_requirements, root, platform, versions[-1])
    shared_dir = root / SHARED_DIR
    uv_install(args.uv, shared_requirements, shared_dir, platform, versions[-1])
    extra_installs = []
    for version in versions[:-1]:
        extra = work / f"shared-{version}"
        uv_install(args.uv, shared_requirements, extra, platform, version)
        extra_installs.append(extra)
    merge_extensions(extra_installs, shared_dir)
    check_extensions(shared_dir, versions, variant, args.arch)

    shutil.rmtree(root / "bin", ignore_errors=True)
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache)

    package = root / "impact_instrumentation"
    shutil.copytree(Path(args.src) / "impact_instrumentation", package, ignore=shutil.ignore_patterns("__pycache__"))
    (package / "_version.py").write_text(f'__version__ = "{args.version}"\n')
    (package / "_registry.py").write_text(generate_registry(root))
    write_own_distribution(
        root, args.version, license_text=Path(args.license).read_text(), notice_text=Path(args.notice).read_text()
    )
    sitecustomize = root / SITECUSTOMIZE_DIR / "sitecustomize.py"
    sitecustomize.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(args.src) / "sitecustomize.py", sitecustomize)
    set_modes(root)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", default="requirements.lock")
    parser.add_argument("--src", default="src")
    parser.add_argument("--out", required=True, help="directory that receives autoinstrumentation[-musl]")
    parser.add_argument("--arch", choices=sorted(ARCH_TAGS), required=True)
    parser.add_argument("--version", default="0.0.0+dev")
    parser.add_argument("--pythons", default="3.11,3.12,3.13,3.14", help="comma-separated CPython versions to support")
    parser.add_argument("--variants", default="glibc,musl")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--license", required=True, help="our LICENSE: for our own dist-info, and the text to supply")
    parser.add_argument("--notice", required=True, help="our NOTICE, for our own dist-info")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for variant in args.variants.split(","):
            root = assemble(args, variant, Path(scratch))
            destination = out / VARIANT_DIRS[variant]
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(root), destination)
            print(f"assembled {destination}", file=sys.stderr)
        built = [out / VARIANT_DIRS[variant] for variant in args.variants.split(",")]
        index = write_licenses(built, out / "licenses", apache_text=Path(args.license).read_text())
        print(f"licenses of {len(index)} distributions in {out / 'licenses' / 'python'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
