# SPDX-License-Identifier: Apache-2.0

"""The image tests' container wrapper really stops a container, with docker as well as with podman.

A container that is not stopped at the end of its test keeps running, and keeps exporting into the mock the next test of
the same worker uses: a late span or setup report then counts as that test's own. Podman's `rm` takes `-t`; docker's
does not, so a call that asks for it fails on docker (where CI runs) and, because the wrapper ignores the exit status of
a cleanup, nothing says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "tests" / "image"))

import containers  # noqa: E402


def test_stop_kills_and_removes_with_options_that_both_docker_and_podman_know(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(containers, "run", lambda *args, **kwargs: calls.append(args))

    containers.Container("i019-example").stop()

    # `docker rm` has no -t/--time: the grace period is not asked for, the container is killed first
    assert calls == [("kill", "i019-example"), ("rm", "-f", "i019-example")]


def test_stop_does_not_care_that_the_container_is_already_gone(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(containers, "run", lambda *args, **kwargs: seen.append(kwargs))

    containers.Container("i019-example").stop()

    assert seen and all(kwargs.get("check") is False for kwargs in seen)
