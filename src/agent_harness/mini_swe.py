from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.process import run_process_group
from agent_harness.runtime import AgentResult, TaskSpec


def _trajectory_usage(path: Path) -> tuple[int, float]:
    if not path.exists():
        return 0, 0.0
    payload = json.loads(path.read_text(encoding="utf-8"))
    tokens = 0
    cost = 0.0
    for message in payload.get("messages", []):
        extra = message.get("extra", {})
        cost += float(extra.get("cost", 0.0) or 0.0)
        response: dict[str, Any] = extra.get("response") or {}
        usage = response.get("usage") or {}
        tokens += int(usage.get("total_tokens", 0) or 0)
    return tokens, cost


@dataclass
class MiniSWEAgentAdapter:
    executable: str
    model: str
    output_dir: Path
    cost_limit_usd: float = 0.8
    config_specs: tuple[str, ...] = ()

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        trajectory = (self.output_dir / f"{task.task_id}-attempt-{attempt}.traj.json").resolve()
        prompt = task.prompt
        if feedback:
            prompt += f"\n\nPrevious verification failed. Fix the remaining issue:\n{feedback[-4000:]}"
        executable = self.executable
        executable_path = Path(executable)
        if executable_path.is_absolute() or executable_path.parent != Path("."):
            executable = str(executable_path.resolve())
        command = [
            executable,
            "--model",
            self.model,
            "--task",
            prompt,
            "--yolo",
            "--exit-immediately",
            "--environment-class",
            "local",
            "--cost-limit",
            str(self.cost_limit_usd),
            "--output",
            str(trajectory),
        ]
        for spec in self.config_specs:
            command.extend(["--config", spec])
        environment = os.environ.copy()
        environment["MSWEA_CONFIGURED"] = "true"
        environment["MSWEA_SILENT_STARTUP"] = "1"
        environment["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "3"
        started = time.perf_counter()
        try:
            completed = run_process_group(
                command,
                cwd=task.workspace,
                timeout=task.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"mini-swe-agent timed out after {task.timeout_seconds}s") from exc
        tokens, cost = _trajectory_usage(trajectory)
        return AgentResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            tokens=tokens,
            cost_usd=cost,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
