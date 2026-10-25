from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_harness.process import run_process_group
from agent_harness.runtime import AgentResult, ReliabilityRuntime, TaskSpec, VerificationResult
from agent_harness.store import RunStore
from agent_harness.trace import verify_trace


class RecordingAgent:
    def __init__(self, failures: set[int] | None = None, interrupt: bool = False):
        self.failures = failures or set()
        self.interrupt = interrupt
        self.calls: list[int] = []

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        self.calls.append(attempt)
        if attempt in self.failures:
            if self.interrupt:
                raise KeyboardInterrupt("simulated process interruption")
            raise RuntimeError("simulated agent failure")
        return AgentResult(exit_code=0, stdout=f"attempt={attempt};feedback={feedback or ''}")


class RecordingVerifier:
    def __init__(self, passes: bool = True):
        self.passes = passes
        self.calls = 0

    def verify(self, task: TaskSpec) -> VerificationResult:
        self.calls += 1
        return VerificationResult(
            passed=self.passes,
            exit_code=0 if self.passes else 1,
            output="ok" if self.passes else "tests failed",
            duration_ms=1,
        )


def task(tmp_path: Path, max_attempts: int = 2) -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        prompt="fix the test",
        workspace=tmp_path,
        verify_command=[sys.executable, "-c", "raise SystemExit(0)"],
        max_attempts=max_attempts,
    )


def test_retries_failed_attempt_and_persists_trace(tmp_path):
    agent = RecordingAgent(failures={1})
    verifier = RecordingVerifier()
    runtime = ReliabilityRuntime(tmp_path / "state", agent, verifier)

    result = runtime.run(task(tmp_path), run_id="retry-run")

    assert result.status == "completed"
    assert result.attempts == 2
    assert agent.calls == [1, 2]
    assert verifier.calls == 1
    valid, events = verify_trace(tmp_path / "state/traces/retry-run.jsonl")
    assert valid is True
    assert events >= 6


def test_resume_after_process_interruption_without_replaying_attempt(tmp_path):
    interrupted = RecordingAgent(failures={1}, interrupt=True)
    first_runtime = ReliabilityRuntime(tmp_path / "state", interrupted, RecordingVerifier())

    with pytest.raises(KeyboardInterrupt):
        first_runtime.run(task(tmp_path), run_id="resume-run")

    resumed_agent = RecordingAgent()
    resumed_runtime = ReliabilityRuntime(tmp_path / "state", resumed_agent, RecordingVerifier())
    result = resumed_runtime.run(task(tmp_path), run_id="resume-run")

    assert result.status == "completed"
    assert result.resumed is True
    assert resumed_agent.calls == [2]


def test_completed_run_is_idempotent(tmp_path):
    agent = RecordingAgent()
    runtime = ReliabilityRuntime(tmp_path / "state", agent, RecordingVerifier())
    first = runtime.run(task(tmp_path), run_id="idempotent-run")
    second = runtime.run(task(tmp_path), run_id="idempotent-run")

    assert first.status == second.status == "completed"
    assert agent.calls == [1]
    assert second.resumed is True


def test_side_effect_is_recorded_once(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.initialize_run("run", "task", {"task_id": "task"})

    assert store.apply_effect_once("run", "publish-result", {"url": "first"}) is True
    assert store.apply_effect_once("run", "publish-result", {"url": "second"}) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group assertion")
def test_timeout_kills_full_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,time,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));"
        "time.sleep(60)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_process_group([sys.executable, "-c", script], cwd=tmp_path, timeout=0.2)

    child_pid = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"child process {child_pid} survived process-group cancellation")
