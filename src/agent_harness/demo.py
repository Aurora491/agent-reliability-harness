from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from agent_harness.runtime import AgentResult, ReliabilityRuntime, TaskSpec, VerificationResult
from agent_harness.trace import verify_trace

DEMO_MARKER = ".arh-demo"
RUN_ID = "crash-recovery-demo"


class CrashOnceAgent:
    """Inject an abrupt controller interruption into attempt one."""

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        raise KeyboardInterrupt("injected controller crash after checkpoint")


class RecoveryAgent:
    """Finish the task after a new runtime instance resumes it."""

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        artifact = task.workspace / "artifact.txt"
        artifact.write_text("recovered without replaying attempt 1\n", encoding="utf-8")
        return AgentResult(
            exit_code=0,
            stdout=f"resumed_attempt={attempt}; feedback={feedback or ''}",
        )


class ArtifactVerifier:
    def verify(self, task: TaskSpec) -> VerificationResult:
        artifact = task.workspace / "artifact.txt"
        passed = artifact.exists() and artifact.read_text(encoding="utf-8").startswith("recovered")
        return VerificationResult(
            passed=passed,
            exit_code=0 if passed else 1,
            output="artifact verified" if passed else "artifact missing or invalid",
            duration_ms=0.0,
        )


def _prepare_output(output: Path) -> None:
    if output.exists():
        marker = output / DEMO_MARKER
        if not marker.is_file():
            raise SystemExit(f"refusing to replace unmarked directory: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / DEMO_MARKER).write_text("safe to replace on the next demo run\n", encoding="utf-8")


def run_demo(output: Path) -> dict[str, object]:
    """Run a deterministic crash, restart, resume, and trace-verification scenario."""
    output = output.resolve()
    _prepare_output(output)
    workspace = output / "workspace"
    workspace.mkdir()
    state = output / "state"
    task = TaskSpec(
        task_id="demo-task",
        prompt="Create a verified artifact.",
        workspace=workspace,
        verify_command=[],
        max_attempts=2,
    )

    crash_observed = False
    try:
        ReliabilityRuntime(state, CrashOnceAgent(), ArtifactVerifier()).run(task, run_id=RUN_ID)
    except KeyboardInterrupt:
        crash_observed = True

    result = ReliabilityRuntime(state, RecoveryAgent(), ArtifactVerifier()).run(task, run_id=RUN_ID)
    trace_path = state / "traces" / f"{RUN_ID}.jsonl"
    trace_valid, trace_events = verify_trace(trace_path)
    if not crash_observed or result.status != "completed" or not result.resumed or not trace_valid:
        raise RuntimeError("crash-recovery demo did not satisfy its safety invariants")

    summary: dict[str, object] = {
        "scenario": "checkpoint -> injected crash -> new runtime -> resume attempt 2 -> verify",
        "crash_observed": crash_observed,
        "status": result.status,
        "resumed": result.resumed,
        "attempts": result.attempts,
        "trace_valid": trace_valid,
        "trace_events": trace_events,
        "trace": str(trace_path.relative_to(output)),
        "artifact": str((workspace / "artifact.txt").relative_to(output)),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the deterministic ARH crash-recovery demo")
    parser.add_argument("--output", type=Path, default=Path("demo-output"))
    return parser


def main() -> int:
    summary = run_demo(build_parser().parse_args().output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
