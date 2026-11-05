#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail when the repository, the rendered chart or an image holds something that is internal to Impact AI.

    python3 scripts/check_internal_details.py --source --chart       # the files, and `helm template` of the chart
    python3 scripts/check_internal_details.py --chart-dir DIR        # the same, for another copy of the chart
    python3 scripts/check_internal_details.py --archive image.tar    # file names and configuration of an image archive
    python3 scripts/check_internal_details.py --source --denylist FILE
    python3 scripts/check_internal_details.py --source --chart --root DIR   # the tree of another checkout
    git log -1 --format=%B | python3 scripts/check_internal_details.py --stdin   # a commit message

This repository is public, and so are the chart and the images made from it. The check looks for

  internal-host   tailnet names (*.ts.net) and the private top-level names .internal, .corp, .lan, .intranet, .local,
                  .home.arpa
  email           e-mail addresses (the documentation domains of RFC 2606 and GitHub's noreply addresses excepted)
  aws-account-id  twelve-digit numbers standing alone, which is what an AWS account ID looks like
  private-ip      addresses in 10/8, 172.16/12, 192.168/16, 100.64/10 (carrier-grade NAT, Tailscale) and 169.254/16
  token           credentials of a known shape: GitHub, AWS, Slack, OpenAI-style keys, private keys, JWTs, bearer tokens
  internal-reference
                  impact.ai's own planning and code: increment ids (the letter I, a hyphen and three digits), and in
                  Markdown the name of the private repository that holds the service. The paths of a company's private
                  organisation are an entry of its deny-list (below): what is published names only the public repository

and fails on anything not named in ALLOWLIST. The allowlist is small on purpose: the documentation account IDs, and the
fake values of test fixtures, each tied to the file that needs it.

What is internal to one company, its own hosts and environments, the names of its customers and of its private
organisation, is not written here: a public checker that names them would name them. It is in a deny-list kept outside
the repository, one entry per line, a category and a regular expression separated by a colon (read without regard to
case; # starts a comment):

    internal-host: \\bbuild-farm\\b
    customer-name: acme
    internal-reference: \\bacme-private/[A-Za-z0-9._-]*

--denylist FILE, or the file the variable INTERNAL_DETAILS_DENYLIST names, is read when it is there; without one the
check says so and runs the built-in patterns alone. A customer-name entry also looks at the names of the files.
Standard library only.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("internal-host", "email", "aws-account-id", "private-ip", "token", "customer-name", "internal-reference")
MAX_FILE_BYTES = 2_000_000

Finding = namedtuple("Finding", ["source", "line", "category", "match"])
# paths: globs of the files where the matched text is allowed ("*": anywhere); pattern: the whole matched text.
Allow = namedtuple("Allow", ["paths", "category", "pattern", "reason"])

ALLOWLIST = (
    Allow(
        ("*",),
        "aws-account-id",
        r"012345678901|123456789012|111122223333",
        "documentation account IDs: examples and tests",
    ),
    Allow(
        ("SECURITY.md", "scripts/publish-public.sh", "chart/tests/test_publish_public.py"),
        "email",
        r"security@impact\.ai",
        "the published contact for vulnerability reports, which is also the identity of a release commit",
    ),
    Allow(
        ("python/tests/image/conftest.py", "nodejs/tests/image/nodejs_support.py"),
        "private-ip",
        r"10\.244\.0\.18|10\.89\.2\.2",
        "the pod and node addresses the Operator injects, in a test fixture",
    ),
    Allow(
        ("reporter/internal/kube/client.go", "reporter/internal/kube/client_test.go"),
        "private-ip",
        r"10\.96\.0\.1",
        "a cluster's API server address, as an example and in a test of reading it from the environment",
    ),
    Allow(
        ("reporter/testdata/api/nodes.json",),
        "internal-host",
        r"ip-10-0-\d-\d+\.ec2\.internal",
        "EC2 node names in a recorded API response (AWS's own naming)",
    ),
    Allow(
        ("reporter/*_test.go", "reporter/*/*_test.go", "reporter/*/*/*_test.go", "reporter/testdata/*"),
        "token",
        r"sk-live-abcdefghij(?:klmnop)",
        "the fake key the reporter's tests plant to prove that no value is ever sent",
    ),
    Allow(
        ("e2e/manifests/workloads.yaml",),
        "token",
        r"sk-e2e-planted-[A-Za-z0-9-]+",
        "fake keys the end-to-end suite plants to prove that no value leaves the cluster",
    ),
    Allow(
        ("e2e/manifests/refuse-kit-init-container.yaml", "e2e/test_8_auto_cover.py"),
        "token",
        r"sk-live-abcdefghij(?:klmnop)",
        "the fake key a refusing policy's message carries, so that the end-to-end test can prove that no snapshot "
        "of the reporter holds it",
    ),
    Allow(
        ("testing/mock/exchange-vectors.json",),
        "token",
        r"Bearer\s+impact(?:install)?_[A-Za-z0-9_-]+|sk-abcdefghij(?:klmnopqrstuvwxyz)",
        "the fake install tokens and the credential-shaped values of the exchange's shared test vectors: each is an "
        "example of what the exchange must refuse",
    ),
)

HOSTS = (
    re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b", re.IGNORECASE),
    re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:internal|corp|lan|intranet)(?![\w-])", re.IGNORECASE),
    re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.home\.arpa(?![\w-])", re.IGNORECASE),
    # .local is mDNS; Kubernetes' own cluster.local is public knowledge.
    re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.local(?![\w-])(?<!cluster\.local)", re.IGNORECASE),
)

# The deny-list: what is internal to one company, kept in a file outside the repository (see the docstring). Empty until
# main() or a test loads one.
DENYLIST_ENV = "INTERNAL_DETAILS_DENYLIST"
Deny = namedtuple("Deny", ["category", "pattern"])
DENYLIST: tuple[Deny, ...] = ()
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
EMAIL_OK = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|users\.noreply\.github\.com)$", re.IGNORECASE
)
# Not after a hyphen: the last group of a UUID has twelve characters and is often all digits.
ACCOUNT = re.compile(r"(?<![0-9A-Za-z-])\d{12}(?![0-9A-Za-z])")
ADDRESS = re.compile(r"(?<!\d)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")
TOKENS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b"),
    re.compile(r"\bBearer\s+(?!\$\{)[A-Za-z0-9._~+/=-]{24,}"),
)


# Increment ids belong to impact.ai's planning. The repositories of its private organisation are private too, the one
# the kit is developed in included, but this file is published with the tree it checks and names no organisation: the
# paths of that one are an internal-reference entry of the deny-list.
REFERENCES = (re.compile(r"\bI-\d{3}\b"),)
# In the documentation the private repository is just called the monorepo.
DOC_REFERENCES = (re.compile(r"\bmonorepo\b", re.IGNORECASE),)


def load_denylist(path: Path) -> tuple[Deny, ...]:
    """The entries of a deny-list file: `category: regular expression` per line, blank lines and # comments left out."""
    entries = []
    for number, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        category, colon, expression = line.partition(":")
        category, expression = category.strip(), expression.strip()
        if not colon or category not in CATEGORIES or not expression:
            raise ValueError(
                f"{path}:{number}: expected `category: regular expression` with a category of {CATEGORIES}"
            )
        try:
            entries.append(Deny(category, re.compile(expression, re.IGNORECASE)))
        except re.error as error:
            raise ValueError(f"{path}:{number}: {expression!r} is not a regular expression ({error})") from error
    return tuple(entries)


@contextlib.contextmanager
def denying(entries):
    """Run a block with these deny-list entries in force, and put back what was there."""
    global DENYLIST
    before, DENYLIST = DENYLIST, tuple(entries)
    try:
        yield
    finally:
        DENYLIST = before


def denylist_path(argv: list[str], environ=os.environ) -> Path | None:
    """The deny-list to read: the file --denylist names (which must be there), or else the one the variable names when
    it is there, or none."""
    if "--denylist" in argv:
        path = Path(argv[argv.index("--denylist") + 1])
        if not path.is_file():
            raise FileNotFoundError(f"the deny-list {path} is not there")
        return path
    named = environ.get(DENYLIST_ENV)
    return Path(named) if named and Path(named).is_file() else None


def private(address: str) -> bool:
    octets = [int(part) for part in address.split(".")]
    if any(part > 255 for part in octets):
        return False
    a, b = octets[0], octets[1]
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 100 and 64 <= b <= 127)
        or (a == 169 and b == 254)
    )


def scan_text(source: str, text: str) -> list[Finding]:
    """Everything in the text that looks internal, with its line number, before the allowlist."""
    found = []
    host_patterns = HOSTS + tuple(entry.pattern for entry in DENYLIST if entry.category == "internal-host")
    for number, line in enumerate(text.splitlines(), start=1):
        merged: list[list[int]] = []
        for a, b in sorted(m.span() for pattern in host_patterns for m in pattern.finditer(line)):
            if merged and a < merged[-1][1]:  # a development host of impact.ai is one host, found by two patterns
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        found += [Finding(source, number, "internal-host", line[a:b]) for a, b in merged]
        found += [
            Finding(source, number, "email", m.group(0))
            for m in EMAIL.finditer(line)
            if not EMAIL_OK.search(m.group(1))
        ]
        found += [Finding(source, number, "aws-account-id", m.group(0)) for m in ACCOUNT.finditer(line)]
        found += [
            Finding(source, number, "private-ip", m.group(0)) for m in ADDRESS.finditer(line) if private(m.group(0))
        ]
        for pattern in TOKENS:
            found += [Finding(source, number, "token", m.group(0)) for m in pattern.finditer(line)]
        for entry in DENYLIST:
            if entry.category != "internal-host":
                found += [Finding(source, number, entry.category, m.group(0)) for m in entry.pattern.finditer(line)]
        for pattern in REFERENCES + (DOC_REFERENCES if source.endswith(".md") else ()):
            found += [Finding(source, number, "internal-reference", m.group(0)) for m in pattern.finditer(line)]
    return found


def allowing(finding: Finding) -> list[Allow]:
    """The allowlist entries that name this finding."""
    return [
        entry
        for entry in ALLOWLIST
        if entry.category == finding.category
        and re.fullmatch(entry.pattern, finding.match)
        and ("*" in entry.paths or any(fnmatch.fnmatch(finding.source, glob) for glob in entry.paths))
    ]


def allowed(finding: Finding) -> bool:
    return bool(allowing(finding))


def stale(findings: list[Finding]) -> list[Allow]:
    """Allowlist entries that no finding needed: the fixture is gone, so the entry should go too."""
    used = {entry for finding in findings for entry in allowing(finding)}
    return [entry for entry in ALLOWLIST if entry not in used]


def violations(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if not allowed(f)]


def tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed command
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            capture_output=True,
            check=True,
        ).stdout.decode()
        return [p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def scan_source(root: Path = ROOT) -> list[Finding]:
    """The tracked text files, and the names of all tracked files for the deny-list's customer names (line 0)."""
    found = []
    for path in tracked(root):
        found += [Finding(path, 0, f.category, f.match) for f in scan_text("", path) if f.category == "customer-name"]
        file = root / path
        if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
            continue
        data = file.read_bytes()
        if b"\0" in data:
            continue
        found += scan_text(path, data.decode(errors="replace"))
    return found


CLOUD = ["cloud.provider=aws", "cloud.accountId=012345678901", "cloud.region=eu-west-1"]
CHART_CASES = {
    "defaults": [],
    "with a cloud identity": [
        arg
        for value in [*CLOUD, "cluster.arn=arn:aws:eks:eu-west-1:012345678901:cluster/c"]
        for arg in ("--set-string", value)
    ],
    "beside another injector": ["--set", "operator.install=false"],
    "with a Secret of the customer's": ["--set", "ingest.key=", "--set", "ingest.existingSecret=their-key"],
    # The exchange hook, the cover pod, the uninstall role that patches pods and the Node.js injection.
    "with an install token and the Node.js image": [
        "--set",
        "ingest.key=",
        "--set-string",
        "install.token=impactinstall_ci_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "--set-string",
        "images.nodejs.tag=v0.0.0-test",
        "--set-string",
        "images.nodejs.digest=sha256:" + "c" * 64,
    ],
}


def scan_chart(root: Path = ROOT, chart: Path | None = None) -> list[Finding]:
    """What `helm template` renders for the cases above: the repository's chart, or the chart in the directory given."""
    chart = chart or root / "chart" / "impact-ai"
    found = []
    for label, extra in CHART_CASES.items():
        command = [
            "helm",
            "template",
            "check",
            str(chart),
            "-n",
            "impact-ai",
            "-f",
            str(chart / "ci" / "values.yaml"),
            *extra,
        ]
        rendered = subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603
        found += scan_text(f"chart ({label})", rendered)
    return found


def image_members(archive: Path) -> tuple[list[str], str]:
    """The file names of all layers of an image archive (docker or OCI layout), and its configuration as text."""
    with tarfile.open(archive) as outer:
        files = {m.name: m for m in outer.getmembers() if m.isfile()}

        def read(name: str) -> bytes:
            return outer.extractfile(files[name]).read()

        if "manifest.json" in files:
            manifest = json.loads(read("manifest.json"))[0]
            layers, config = manifest["Layers"], manifest["Config"]
        else:
            index = json.loads(read("index.json"))
            blob = "blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]
            manifest = json.loads(read(blob))
            layers = ["blobs/sha256/" + layer["digest"].split(":")[1] for layer in manifest["layers"]]
            config = "blobs/sha256/" + manifest["config"]["digest"].split(":")[1]
        names = []
        for layer in layers:
            with tarfile.open(fileobj=io.BytesIO(read(layer))) as inner:  # tarfile reads gzip layers transparently
                names += [member.name for member in inner.getmembers()]
        return names, read(config).decode(errors="replace")


def scan_archive(archive: Path) -> list[Finding]:
    names, config = image_members(archive)
    label = f"image {archive.name}"
    found = scan_text(f"{label}: configuration", config)
    for number, name in enumerate(names, start=1):
        found += [Finding(f"{label}: {name}", number, f.category, f.match) for f in scan_text("", name)]
    return found


def main(argv: list[str]) -> int:
    try:
        path = denylist_path(argv)
        entries = load_denylist(path) if path else ()
    except (OSError, ValueError, IndexError) as error:
        print(f"deny-list: {error}", file=sys.stderr)
        return 2
    if path:
        print(f"deny-list: {len(entries)} entries from {path}", file=sys.stderr)
    else:
        print(
            f"no deny-list: only the built-in patterns ran (--denylist FILE or {DENYLIST_ENV} adds a company's names)",
            file=sys.stderr,
        )
    with denying(entries):
        return run(argv)


def run(argv: list[str]) -> int:
    found: list[Finding] = []
    args = iter(argv)
    asked = False
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else ROOT
    for arg in args:
        if arg == "--source":
            found += scan_source(root)
        elif arg == "--chart":
            found += scan_chart(root)
        elif arg == "--chart-dir":
            found += scan_chart(chart=Path(next(args)))
        elif arg == "--archive":
            found += scan_archive(Path(next(args)))
        elif arg == "--stdin":
            found += scan_text("stdin", sys.stdin.read())
        elif arg in ("--denylist", "--root"):
            next(args)  # read above
        else:
            print(__doc__, file=sys.stderr)
            return 2
        asked = True
    if not asked:
        print(__doc__, file=sys.stderr)
        return 2
    bad = violations(found)
    for f in bad:
        print(f"{f.source}:{f.line}: {f.category}: {f.match}")
    if bad:
        print(
            f"{len(bad)} internal detail(s): remove them, or name a fixture value in ALLOWLIST with its reason",
            file=sys.stderr,
        )
        return 1
    unused = (
        stale(found) if "--source" in argv and root == ROOT else []
    )  # only a scan of the whole tree of this repository can say that an entry is not used
    for entry in unused:
        print(f"allowlist entry no longer needed: {entry.category} {entry.pattern} in {', '.join(entry.paths)}")
    return 1 if unused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
