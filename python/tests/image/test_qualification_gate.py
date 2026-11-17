# SPDX-License-Identifier: Apache-2.0

"""An AI library is traced only where the image's qualification proves it, on the image (the rules are in
python/tests/unit/test_qualification.py).

A workload whose openai is a version no cell proves (here the workload's own PYTHONPATH lists an openai 9.0.0 ahead of
the openai of its image, which is what the package reads the version from) starts and serves as it would without the kit
for openai, its Anthropic calls stay traced, and the setup report says what Impact AI traces and what the workload runs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import MOUNT, PREFIX, app_image, require_images
from containers import http, wait_until
from impact_instrumentation.qualification import proven

PROMPT, ANSWER = "hi there", "Hello from the stub."
UVICORN = ["uvicorn", "app_asgi:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
QUALIFICATION = json.loads((Path(__file__).resolve().parents[2] / "qualification.json").read_text())


def proven_openai(python: str) -> str:
    """What the image's qualification proves of openai on this Python, as the report says it ('openai 1 to 3')."""
    return proven(QUALIFICATION, python=python, library="openai")


def readable(path: Path) -> Path:
    """Files made on the host, readable by the user the workload runs as."""
    for item in [path, *path.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    return path


def workload_dependencies(tmp_path: Path, name: str, version: str) -> Path:
    """A directory the workload puts on its own PYTHONPATH, with the metadata of one distribution in it."""
    info = tmp_path / "deps" / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return readable(tmp_path / "deps")


def model_calls(mock, model: str) -> list[dict]:
    return [s for s in mock.spans() if s["attrs"].get("gen_ai.request.model") == model]


@pytest.mark.image
def test_a_version_no_cell_proves_starts_as_without_the_kit_and_the_report_says_what_impact_ai_traces(
    start_app, mock, tmp_path
):
    require_images(app_image("3.12"))
    deps = workload_dependencies(tmp_path, "openai", "9.0.0")
    # The Operator puts its directories around the workload's own PYTHONPATH: <prefix>:<the workload's>:<payload>.
    app = start_app(
        app_image("3.12"),
        UVICORN,
        env={"PYTHONPATH": f"{PREFIX}:/workload-deps:{MOUNT}"},
        mounts={str(deps): "/workload-deps"},
    )
    wait_until(lambda: http("GET", f"{app.url}/healthz")[0] == 200, timeout=90, message=lambda: app.logs())

    for provider in ("openai", "anthropic"):
        assert http("POST", f"{app.url}/chat/{provider}", {"prompt": PROMPT}) == (200, {"answer": ANSWER}), app.logs()

    wait_until(
        lambda: model_calls(mock, "claude-stub"), timeout=30, message=lambda: f"the Anthropic span\n{app.logs()}"
    )
    time.sleep(1.5)  # an OpenAI span would be in the same batch or the next one
    assert model_calls(mock, "gpt-4o-mini") == [], "openai 9.0.0 is not proven: nothing of it is traced"

    report = next(s for s in mock.spans() if s["name"] == "impact.setup")["attrs"]
    assert report["impact.setup.state"] == "active"
    armed = report["impact.setup.instrumentations"].split(",")
    assert "openai" not in armed and "anthropic" in armed
    assert "openai 9.0.0" in report["impact.setup.frameworks"]
    assert report["impact.setup.untraced"] == ["openai 9.0.0"]
    assert report["impact.setup.skipped"] == (
        f"openai: Impact AI traces {proven_openai('3.12')}; this workload runs openai 9.0.0"
    )
