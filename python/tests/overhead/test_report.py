# SPDX-License-Identifier: Apache-2.0

"""The report of a release's overhead, and its stamp on the chart: OVERHEAD.md beside Chart.yaml and the worst numbers
as an
annotation, from the records the combinations leave."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "chart" / "impact-ai"
sys.path.insert(0, str(ROOT / "scripts"))

import overhead_report  # noqa: E402
import stamp_chart  # noqa: E402


def record(runtime, version, server, cpu, memory, start, *, ok=True, attempts=1, pinned=True) -> dict:
    return {
        "id": f"{runtime}{version}-{server}",
        "runtime": runtime,
        "version": version,
        "server": server,
        "rounds": 3,
        "calls": 300,
        "warmup": 100,
        "machine": {"pinned": pinned, "cpus": [2, 9] if pinned else None},
        "attempts": [{"ok": ok}] * attempts,
        "median": {"cpu_ms_per_call": cpu, "memory_mib": memory, "start_s": start},
        "init_copy_s": 0.8,
        "ok": ok,
    }


@pytest.fixture
def directory(tmp_path):
    combos = tmp_path / "combos"
    combos.mkdir()
    for rec in (
        record("python", "3.12", "uvicorn", 0.62, 14.3, 0.41),
        record("python", "3.11", "gunicorn", 0.9, 22.0, 0.55, attempts=2),
        record("node", "22", "express", 0.41, 17.2, 0.62),
        record("python", "3.14", "uvicorn", 0.55, 15.1, 0.47),
    ):
        (combos / f"{rec['id']}.json").write_text(json.dumps(rec))
    return tmp_path


def test_the_summary_orders_the_workloads_and_keeps_the_worst_of_each_number(directory):
    summary = overhead_report.summarize(overhead_report.load(directory), "0.3.0")
    assert [c["workload"] for c in summary["combos"]] == [
        "Node.js 22, Express",
        "Python 3.11, gunicorn",
        "Python 3.12, uvicorn",
        "Python 3.14, uvicorn",
    ]
    assert summary["worst"] == {"cpuMsPerCall": 0.9, "memoryMiB": 22.0, "startSeconds": 0.62}
    assert summary["measures"] == {
        "kit.overhead_per_llm_call_ms": 0.9,
        "kit.overhead_memory_mib": 22.0,
        "kit.overhead_start_s": 0.62,
    }
    assert summary["ok"] is True and summary["schema"] == "impact.overhead.v1"
    assert [c["rerun"] for c in summary["combos"]] == [False, True, False, False]


def test_a_workload_that_calls_a_library_of_its_own_is_named_by_it_after_the_plain_one(directory):
    rec = record("python", "3.12", "uvicorn", 1.1, 19.0, 0.9)
    rec.update(id="python3.12-uvicorn-google-genai", library="google-genai")
    (directory / "combos" / f"{rec['id']}.json").write_text(json.dumps(rec))
    summary = overhead_report.summarize(overhead_report.load(directory), "0.3.0")
    names = [c["workload"] for c in summary["combos"]]
    assert names.index("Python 3.12, uvicorn, calling google-genai") == names.index("Python 3.12, uvicorn") + 1
    assert summary["worst"]["cpuMsPerCall"] == 1.1
    assert "| Python 3.12, uvicorn, calling google-genai | +1.10 ms |" in overhead_report.render(summary)


def test_the_markdown_is_a_table_with_the_budget_and_how_it_was_measured(directory):
    text = overhead_report.render(overhead_report.summarize(overhead_report.load(directory), "0.3.0"))
    assert text.startswith("# Overhead of the injected instrumentation, release 0.3.0")
    assert "| Python 3.12, uvicorn | +0.62 ms | +14.3 MiB | +0.41 s | 0.80 s |  |" in text
    assert "| Python 3.11, gunicorn | +0.90 ms | +22.0 MiB | +0.55 s | 0.80 s | re-run |" in text
    assert "| Budget | +1.50 ms | +40.0 MiB | +2.00 s |  |  |" in text
    assert "each pinned to a core of its own" in text and "median of the differences" in text


def test_the_markdown_says_which_workloads_were_pinned_and_why_node_js_is_left_free(directory):
    (directory / "combos" / "node22-express.json").write_text(
        json.dumps(record("node", "22", "express", 0.41, 17.2, 0.62, pinned=False))
    )
    summary = overhead_report.summarize(overhead_report.load(directory), "0.3.0")
    assert summary["method"]["pinnedRuntimes"] == ["python"] and summary["method"]["unpinnedRuntimes"] == ["node"]
    assert summary["method"]["pinned"] is False
    text = overhead_report.render(summary)
    assert "the Python ones each pinned to a core of its own and the Node.js ones not" in text
    assert "garbage collector" in text and "single pinned core" in text
    assert "read after both sides have collected all their garbage" in text


def test_the_markdown_says_how_node_js_memory_is_read_why_what_is_kept_and_what_the_budget_is(directory):
    (directory / "combos" / "node22-express.json").write_text(
        json.dumps(record("node", "22", "express", 0.41, 17.2, 0.62, pinned=False))
    )
    text = " ".join(overhead_report.render(overhead_report.summarize(overhead_report.load(directory), "0.3.0")).split())
    # the method, and the reason for it
    assert "two identical plain Node.js processes differ by -22 to +23 MiB before a collection" in text
    # the raw reading is kept, and the budget did not move
    assert "kept in the record of each workload (memory_unsettled_mib) beside the one judged" in text
    assert "The budget is unchanged at +40 MiB." in text
    # what five runs in a row gave when the method was adopted
    assert "five runs in a row put the median difference at" in text
    assert "+5.5 to +7.4 MiB on Node.js 22, +6.4 to +9.9 MiB on Node.js 24 and +6.3 to +10.8 MiB on Node.js 26" in text


def test_the_paragraph_on_how_node_js_memory_is_read_is_only_in_a_report_that_left_it_free(directory):
    text = overhead_report.render(overhead_report.summarize(overhead_report.load(directory), "0.3.0"))
    assert "garbage" not in text


def test_a_report_of_workloads_that_were_not_pinned_says_nothing_about_pinning(directory):
    for path in (directory / "combos").glob("*.json"):
        data = json.loads(path.read_text())
        data["machine"] = {"pinned": False, "cpus": None}
        path.write_text(json.dumps(data))
    text = overhead_report.render(overhead_report.summarize(overhead_report.load(directory), "0.3.0"))
    assert "pinned to a core" not in text and "side by side." in text


def test_a_report_over_the_budget_says_so_and_the_command_fails(directory, capsys):
    (directory / "combos" / "python3.12-uvicorn.json").write_text(
        json.dumps(record("python", "3.12", "uvicorn", 2.0, 14.3, 0.41, ok=False))
    )
    assert overhead_report.main([str(directory), "--version", "0.3.0"]) == 1
    assert "OVER the budget" in capsys.readouterr().err
    assert json.loads((directory / "results.json").read_text())["ok"] is False


def test_the_command_writes_the_results_and_the_markdown(directory):
    assert overhead_report.main([str(directory), "--version", "1.2.3"]) == 0
    assert json.loads((directory / "results.json").read_text())["version"] == "1.2.3"
    assert (
        (directory / "OVERHEAD.md").read_text().startswith("# Overhead of the injected instrumentation, release 1.2.3")
    )


def test_no_records_is_an_error_that_says_what_to_run(tmp_path):
    with pytest.raises(SystemExit, match="make overhead"):
        overhead_report.load(tmp_path)


@pytest.fixture
def stamped(directory, tmp_path):
    overhead_report.main([str(directory), "--version", "1.2.3"])
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    results = json.loads((directory / "results.json").read_text())
    stamp_chart.stamp_overhead(copy, results, (directory / "OVERHEAD.md").read_text())
    return copy


def test_the_stamp_puts_the_worst_numbers_in_a_chart_annotation_and_the_report_beside_the_chart(stamped):
    chart = yaml.safe_load((stamped / "Chart.yaml").read_text())
    assert json.loads(chart["annotations"]["impact.ai/overhead"]) == {
        "cpuMsPerCall": 0.9,
        "memoryMiB": 22.0,
        "startSeconds": 0.62,
        "workloads": 4,
    }
    assert chart["annotations"]["artifacthub.io/license"] == "Apache-2.0"  # what was there stays
    assert chart["dependencies"]  # and so does the rest of the file
    assert (stamped / "OVERHEAD.md").read_text().startswith("# Overhead of the injected instrumentation, release 1.2.3")


def test_the_report_is_part_of_the_package(stamped):
    ignored = (stamped / ".helmignore").read_text().split()
    assert "OVERHEAD.md" not in ignored and not [p for p in ignored if p in ("*.md", "*.MD")]


def test_stamping_the_overhead_twice_fails_instead_of_overwriting_a_release(stamped, directory):
    results = json.loads((directory / "results.json").read_text())
    with pytest.raises(SystemExit, match="already carries"):
        stamp_chart.stamp_overhead(stamped, results, "x")


def test_a_release_does_not_publish_an_overhead_over_the_budget(tmp_path):
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    with pytest.raises(SystemExit, match="over the budget"):
        stamp_chart.stamp_overhead(copy, {"schema": "impact.overhead.v1", "ok": False}, "x")
    assert "impact.ai/overhead" not in (copy / "Chart.yaml").read_text()


def test_results_of_another_schema_are_refused(tmp_path):
    with pytest.raises(SystemExit, match="schema"):
        stamp_chart.stamp_overhead(tmp_path, {"schema": "other"}, "x")


def test_a_chart_without_annotations_has_nowhere_to_put_it(tmp_path):
    (tmp_path / "Chart.yaml").write_text("name: x\nversion: 1.0.0\n")
    results = {
        "schema": "impact.overhead.v1",
        "ok": True,
        "worst": {"cpuMsPerCall": 1, "memoryMiB": 1, "startSeconds": 1},
        "combos": [],
    }
    with pytest.raises(SystemExit, match="no annotations"):
        stamp_chart.stamp_overhead(tmp_path, results, "x")


def test_the_command_line_stamps_the_version_the_digests_and_the_overhead(directory, tmp_path):
    overhead_report.main([str(directory), "--version", "1.2.3"])
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    digest = "sha256:" + "a" * 64
    assert (
        stamp_chart.main(
            [
                str(copy),
                "1.2.3",
                "--python",
                digest,
                "--reporter",
                digest,
                "--overhead",
                str(directory / "results.json"),
            ]
        )
        == 0
    )
    chart = yaml.safe_load((copy / "Chart.yaml").read_text())
    assert chart["version"] == "1.2.3" and "impact.ai/overhead" in chart["annotations"]


def test_a_nodejs_digest_needs_the_block_the_chart_has_for_it(tmp_path):
    # The chart has the block, empty until a release fills it in (chart/tests/test_release.py stamps it); a copy
    # without it refuses the digest instead of dropping it.
    copy = tmp_path / "impact-ai"
    shutil.copytree(CHART, copy)
    digest = "sha256:" + "b" * 64
    values = copy / "values.yaml"
    block = re.compile(r'(?m)^  nodejs:\n    repository: .*\n    tag: ""\n    digest: ""\n')
    text = values.read_text()
    assert block.search(text), "the chart carries the empty images.nodejs block"
    values.write_text(block.sub("", text, count=1))
    with pytest.raises(SystemExit, match="values.yaml: no empty tag and digest for images.nodejs"):
        stamp_chart.stamp(copy, "1.2.3", {"python": digest, "reporter": digest, "nodejs": digest})
