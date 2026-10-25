from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_harness.runtime import AgentResult, TaskSpec, VerificationResult


def run_process_group(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group and kill the full group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@dataclass
class CommandAgent:
    """Adapter for an external coding agent command.

    Template values: ``{prompt}``, ``{workspace}``, ``{attempt}``, and ``{feedback}``.
    """

    command_template: list[str]
    environment: dict[str, str] = field(default_factory=dict)

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        values = {
            "prompt": task.prompt,
            "workspace": str(task.workspace),
            "attempt": str(attempt),
            "feedback": feedback or "",
        }
        command = [part.format(**values) for part in self.command_template]
        env = os.environ.copy()
        env.update(self.environment)
        started = time.perf_counter()
        try:
            completed = run_process_group(
                command,
                cwd=task.workspace,
                timeout=task.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"agent command timed out after {task.timeout_seconds}s: {shlex.join(command)}"
            ) from exc
        return AgentResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


class SubprocessVerifier:
    def verify(self, task: TaskSpec) -> VerificationResult:
        started = time.perf_counter()
        try:
            completed = run_process_group(
                task.verify_command,
                cwd=task.workspace,
                timeout=task.timeout_seconds,
            )
            output = completed.stdout + completed.stderr
            return VerificationResult(
                passed=completed.returncode == 0,
                exit_code=completed.returncode,
                output=output,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except subprocess.TimeoutExpired as exc:
            output = f"{exc.output or ''}{exc.stderr or ''}"
            return VerificationResult(
                passed=False,
                exit_code=-1,
                output=output,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
