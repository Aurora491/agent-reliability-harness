from __future__ import annotations

import json
from pathlib import Path

from agent_harness.demo import run_demo


def test_demo_recovers_after_restart_and_emits_valid_trace(tmp_path: Path):
    output = tmp_path / "demo"
    summary = run_demo(output)

    assert summary["crash_observed"] is True
    assert summary["status"] == "completed"
    assert summary["resumed"] is True
    assert summary["attempts"] == 2
    assert summary["trace_valid"] is True
    assert summary["trace_events"] == 8
    assert (output / "workspace/artifact.txt").read_text().startswith("recovered")
    assert json.loads((output / "summary.json").read_text()) == summary


def test_demo_can_be_rerun_safely(tmp_path: Path):
    output = tmp_path / "demo"
    first = run_demo(output)
    second = run_demo(output)

    assert first["trace_events"] == second["trace_events"] == 8
