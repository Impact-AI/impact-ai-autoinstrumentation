#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Every commit of a pull request is signed off by its author (the Developer Certificate of Origin, CONTRIBUTING.md).

    python3 scripts/check_dco.py <base> <head>       for example  python3 scripts/check_dco.py origin/main HEAD

A commit passes when its message has a trailer `Signed-off-by: Name <email>` with the author's e-mail address (in any
case). Merge commits are not checked. Exits 1, naming each commit that fails, when one does.
"""

from __future__ import annotations

import re
import subprocess
import sys

SIGN_OFF = re.compile(r"^Signed-off-by: .+ <([^<>\s]+)>\s*$", re.MULTILINE)


def commits(base: str, head: str) -> list[tuple[str, str, str]]:
    """(sha, author e-mail, message) of each commit in head and not in base, merges left out."""
    out = subprocess.run(  # noqa: S603 - git with the caller's revisions
        ["git", "log", "--no-merges", "--format=%H%x00%ae%x00%B%x01", f"{base}..{head}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for record in out.split("\x01"):
        if record.strip():
            sha, email, message = record.lstrip("\n").split("\x00", 2)
            found.append((sha, email, message))
    return found


def unsigned(base: str, head: str) -> list[str]:
    missing = []
    for sha, email, message in commits(base, head):
        if email.lower() not in {signed.lower() for signed in SIGN_OFF.findall(message)}:
            missing.append(f"{sha[:12]} by {email}: no 'Signed-off-by: ... <{email}>' (git commit -s)")
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    missing = unsigned(*argv)
    for line in missing:
        print(f"check-dco: {line}", file=sys.stderr)
    if missing:
        print("check-dco: sign off every commit: git rebase --signoff <base>, then push again", file=sys.stderr)
        return 1
    print("every commit is signed off by its author")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
