# SPDX-License-Identifier: Apache-2.0

"""The image itself: the layout the Operator needs, non-root, no provider SDK, no native code, every package
licensed."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile

import pytest

from containers import CONTAINER
from nodejs_support import IMAGE, QUALIFICATION, ROOT, require_images, run

PAYLOAD_LIMIT_MIB = 40  # the init container copies this into every pod: it must not grow unnoticed


def listing(path: str, *args: str) -> list[str]:
    return sorted(run("run", "--rm", IMAGE, "find", path, *args).stdout.split())


def licenses() -> dict[str, bytes]:
    """The files of /licenses, read out of the image without running it."""
    created = subprocess.run(  # noqa: S603
        [CONTAINER, "create", IMAGE], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        archive = subprocess.run(  # noqa: S603
            [CONTAINER, "cp", f"{created}:/licenses", "-"], capture_output=True, check=True
        ).stdout
    finally:
        subprocess.run([CONTAINER, "rm", created], capture_output=True, check=False)  # noqa: S603
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


@pytest.mark.nodejs
def test_the_image_runs_as_a_non_root_user_and_has_cp():
    require_images(IMAGE)
    config = json.loads(run("image", "inspect", IMAGE, "--format", "{{json .Config}}").stdout)
    assert config["User"] == "65532:65532"
    assert run("run", "--rm", IMAGE, "sh", "-c", "command -v cp").stdout.strip().endswith("cp")
    assert config["Labels"]["org.opencontainers.image.version"]


@pytest.mark.nodejs
def test_the_layout_is_the_operators_preload_one_tree_and_the_qualification():
    require_images(IMAGE)
    top = [p.rsplit("/", 1)[1] for p in listing("/autoinstrumentation", "-maxdepth", "1", "-mindepth", "1")]
    assert top == ["autoinstrumentation.js", "impact", "node22", "qualification.json"]
    assert "node_modules" in listing("/autoinstrumentation/node22", "-maxdepth", "1", "-mindepth", "1")[0]
    modules = [p.rsplit("/", 1)[1] for p in listing("/autoinstrumentation/impact", "-maxdepth", "1", "-type", "f")]
    assert {"bootstrap.js", "detect.js", "exporter.js", "runtime.js", "setup-report.js", "version.js"} <= set(modules)


@pytest.mark.nodejs
def test_the_qualification_in_the_image_is_the_committed_one():
    require_images(IMAGE)
    inside = run("run", "--rm", IMAGE, "cat", "/autoinstrumentation/qualification.json").stdout
    assert json.loads(inside) == json.loads(QUALIFICATION.read_text())


@pytest.mark.nodejs
def test_the_version_is_the_releases():
    require_images(IMAGE)
    out = run("run", "--rm", IMAGE, "cat", "/autoinstrumentation/impact/version.js").stdout
    label = json.loads(run("image", "inspect", IMAGE, "--format", "{{json .Config.Labels}}").stdout)[
        "org.opencontainers.image.version"
    ]
    assert f'exports.VERSION = "{label}";' in out


@pytest.mark.nodejs
@pytest.mark.parametrize("tree", ["node22"])
def test_no_provider_sdk_and_no_native_code_is_in_a_tree(tree):
    """The only package called openai is the stand-in; the exporter speaks JSON on Node's own modules, so no protocol
    buffer
    or gRPC stack comes with it; nothing is a link out of the tree or a compiled add-on."""
    require_images(IMAGE)
    base = f"/autoinstrumentation/{tree}/node_modules"
    names = [p.removeprefix(base + "/") for p in listing(base, "-maxdepth", "2", "-name", "package.json")]
    packages = {n.removesuffix("/package.json") for n in names}
    for forbidden in ("@anthropic-ai/sdk", "protobufjs", "@grpc/grpc-js", "undici", "node-fetch", "boto3"):
        assert forbidden not in packages, forbidden
    shim = json.loads(run("run", "--rm", IMAGE, "cat", f"{base}/openai/package.json").stdout)
    assert shim["version"] == "0.0.0-impact-shim" and shim["private"] is True
    assert listing(f"{base}/openai", "-type", "f") == [f"{base}/openai/index.js", f"{base}/openai/package.json"]
    assert listing(base, "-type", "l") == []
    assert (
        listing(base, "-type", "f", "(", "-name", "*.node", "-o", "-name", "*.so", "-o", "-name", "*.wasm", ")") == []
    )


@pytest.mark.nodejs
def test_the_payload_stays_small():
    require_images(IMAGE)
    kib = int(run("run", "--rm", IMAGE, "sh", "-c", "du -sk /autoinstrumentation | cut -f1").stdout.split()[0])
    assert kib < PAYLOAD_LIMIT_MIB * 1024, f"{kib / 1024:.1f} MiB"


@pytest.mark.nodejs
@pytest.mark.parametrize("path", ["/autoinstrumentation", "/licenses"])
def test_every_file_can_be_read_by_any_user_and_none_written_by_others(path):
    # /licenses too: COPY --chmod onto a directory that COPY creates gives it the files' mode on some builders (GitHub's
    # Ubuntu 26.04 runners), which no one can enter, so the Dockerfile sets modes in a build stage instead.
    require_images(IMAGE)
    assert listing(path, "-not", "-perm", "-o+r") == []
    assert listing(path, "-type", "d", "-not", "-perm", "-o+x") == []
    assert listing(path, "-perm", "-o+w") == []


@pytest.mark.nodejs
def test_the_image_carries_our_license_and_notice_and_those_of_every_package_it_bundles():
    require_images(IMAGE)
    files = licenses()
    assert files["licenses/LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert files["licenses/NOTICE"] == (ROOT / "NOTICE").read_bytes()
    index = json.loads(files["licenses/nodejs/index.json"])
    names = {d["name"] for d in index}
    assert {
        "@opentelemetry/api",
        "@opentelemetry/instrumentation",
        "@arizeai/openinference-instrumentation-openai",
        "import-in-the-middle",
        "openai",
    } <= names
    assert [d["version"] for d in index if d["name"] == "openai"] == ["0.0.0-impact-shim"]
    for entry in index:
        folder = f"licenses/nodejs/{entry['name'].replace('/', '__')}@{entry['version']}"
        assert entry["files"] and all(f"{folder}/{name}" in files for name in entry["files"]), entry
        assert entry["license"], entry
    assert b"| Package | Version | License | Authors | Files |" in files["licenses/nodejs/INDEX.md"]
    # names only: nobody's e-mail address or home page in what we publish
    assert not [a for d in index for a in d["authors"] if "@" in a or "<" in a or "://" in a]


@pytest.mark.nodejs
def test_the_image_carries_the_license_of_the_busybox_it_runs_and_where_its_source_is():
    require_images(IMAGE)
    files = licenses()
    assert b"GNU GENERAL PUBLIC LICENSE" in files["licenses/busybox/COPYING"]
    assert b"https://busybox.net/downloads/busybox-1.37.0.tar.bz2" in files["licenses/busybox/README"]
