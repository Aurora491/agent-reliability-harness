from __future__ import annotations

import subprocess
from pathlib import Path

from agent_harness.mini_swe import MiniSWEAgentAdapter
from agent_harness.runtime import TaskSpec


def test_adapter_passes_config_and_marks_mini_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_harness.mini_swe.run_process_group", fake_run)
    adapter = MiniSWEAgentAdapter(
        executable="mini",
        model="deepseek/deepseek-flash",
        output_dir=tmp_path,
        cost_limit_usd=0.08,
        config_specs=("mini.yaml", "/tmp/deepseek_agent.yaml"),
    )
    task = TaskSpec(
        task_id="mbpp-1",
        prompt="implement it",
        workspace=tmp_path,
        verify_command=["python", "-m", "pytest", "-q"],
    )

    adapter.run(task, attempt=1, feedback=None)

    output_flag = captured["command"].index("--output")
    assert Path(captured["command"][output_flag + 1]).is_absolute()
    assert captured["env"]["MSWEA_CONFIGURED"] == "true"
    assert captured["command"][captured["command"].index("--config") + 1] == "mini.yaml"
    assert "/tmp/deepseek_agent.yaml" in captured["command"]
