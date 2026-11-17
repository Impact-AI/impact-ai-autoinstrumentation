#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Every source file of ours carries `SPDX-License-Identifier: Apache-2.0` in its first lines.

    python3 scripts/check_spdx.py          # list the files without it, exit 1 if there are any
    python3 scripts/check_spdx.py --add    # put the header into those files

Ours means the files (tracked, or new and not ignored) whose comment syntax is known: Python, Go, Java, JavaScript,
shell, Dockerfiles, Makefile, TOML, requirement inputs, YAML, and the chart's templates (as template comments, which
render to nothing).
Not ours, or without comments: the vendored Operator chart and its CRD, license and notice files, JSON, lock files,
ignore files, Markdown, the workflows (the code owner's) and go.mod. Standard library only.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "SPDX-License-Identifier: Apache-2.0"
HEADER_LINES = 10  # the identifier must be in the first lines
TEMPLATE_COMMENT = "{{- /* " + IDENTIFIER + " */ -}}"

# Not ours, generated, or unable to hold a comment.
EXCLUDED = (
    "LICENSE",
    "NOTICE",
    "*/LICENSE",
    "*/NOTICE",
    ".github/*",
    "chart/impact-ai/charts/*",
    "chart/impact-ai/crds/*",
    "*/node_modules/*",
    "*.json",
    "*.lock",
    "*.md",
    "*.mod",
    "*ignore",
    ".gitignore",
    ".python-version",
    "*/requirements*.txt",
    "*/requirements.lock",
)


def kind_of(path: str) -> str | None:
    """How the file writes a comment: hash, slash or template; None when it does not carry the header."""
    if any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDED):
        return None
    name = path.rsplit("/", 1)[-1]
    if path.startswith("chart/impact-ai/templates/"):
        return "template" if name.endswith((".yaml", ".tpl", ".txt")) else None
    if name.endswith((".go", ".java", ".js", ".cjs", ".mjs")):
        return "slash"
    if name.endswith((".py", ".sh", ".toml", ".in", ".yaml", ".yml")) or name in ("Makefile",):
        return "hash"
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return "hash"
    return None


def has_header(text: str) -> bool:
    return IDENTIFIER in "\n".join(text.splitlines()[:HEADER_LINES])


def with_header(text: str, kind: str, name: str = "") -> str:
    """The file's text with the header in the place its syntax allows (after a shebang or a `# syntax=` directive)."""
    lines = text.splitlines(keepends=True)
    comment = {"hash": f"# {IDENTIFIER}\n", "slash": f"// {IDENTIFIER}\n", "template": TEMPLATE_COMMENT + "\n"}[kind]
    at = 0
    if lines and kind == "hash" and (lines[0].startswith("#!") or re.match(r"#\s*syntax=", lines[0])):
        at = 1
    elif lines and kind == "slash" and lines[0].startswith("#!"):  # a Node script: the header follows its shebang
        at = 1
    header = [comment]
    # A blank line after it keeps the header from becoming the documentation of what follows (Go) or from gluing to it.
    if kind in ("slash", "hash") and at < len(lines) and lines[at].strip() and not name.endswith(".yaml"):
        header.append("\n")
    return "".join(lines[:at] + header + lines[at:])


def tracked(root: Path = ROOT) -> list[str]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed command
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            capture_output=True,
            check=True,
        ).stdout.decode()
        return [p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def missing(root: Path = ROOT) -> list[str]:
    found = []
    for path in tracked(root):
        kind = kind_of(path)
        file = root / path
        if kind and file.is_file() and not has_header(file.read_text(errors="replace")):
            found.append(path)
    return found


def main(argv: list[str]) -> int:
    add = "--add" in argv
    files = missing()
    for path in files:
        if add:
            file = ROOT / path
            file.write_text(with_header(file.read_text(), kind_of(path) or "hash", path.rsplit("/", 1)[-1]))
        else:
            print(f"{path}: no {IDENTIFIER}")
    if files and not add:
        print(f"{len(files)} file(s) without the header: python3 scripts/check_spdx.py --add", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
