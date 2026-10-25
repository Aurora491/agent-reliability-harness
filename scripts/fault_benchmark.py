from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from agent_harness.runtime import AgentResult, ReliabilityRuntime, TaskSpec, VerificationResult


class ScheduledCrashAgent:
    def __init__(self, fault_every: int):
        self.fault_every = fault_every

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        task_number = int(task.task_id.rsplit("-", 1)[1])
        if attempt == 1 and task_number % self.fault_every == 0:
            raise RuntimeError("injected agent process crash")
        return AgentResult(exit_code=0, stdout="recovered")


class AlwaysPassVerifier:
    def verify(self, task: TaskSpec) -> VerificationResult:
        return VerificationResult(passed=True, exit_code=0, output="ok", duration_ms=0)


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def run_variant(root: Path, tasks: int, attempts: int, fault_every: int) -> dict:
    runtime = ReliabilityRuntime(root, ScheduledCrashAgent(fault_every), AlwaysPassVerifier())
    latencies = []
    completed = 0
    duplicate_effects = 0
    for index in range(tasks):
        task = TaskSpec(
            task_id=f"fault-{index:04d}",
            prompt="fault injection",
            workspace=root,
            verify_command=[],
            max_attempts=attempts,
        )
        started = time.perf_counter()
        result = runtime.run(task, run_id=task.task_id)
        latencies.append((time.perf_counter() - started) * 1000)
        completed += result.status == "completed"
        if runtime.store.apply_effect_once(result.run_id, "publish", {"status": result.status}):
            duplicate_effects += 0
        if runtime.store.apply_effect_once(result.run_id, "publish", {"status": result.status}):
            duplicate_effects += 1
    return {
        "tasks": tasks,
        "injected_faults": sum(index % fault_every == 0 for index in range(tasks)),
        "completed": completed,
        "completion_rate": completed / tasks,
        "duplicate_effects": duplicate_effects,
        "mean_ms": statistics.mean(latencies),
        "p95_ms": p95(latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--fault-every", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("results/fault-benchmark.json"))
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="agent-harness-benchmark-"))
    try:
        baseline = run_variant(temp_root / "baseline", args.tasks, attempts=1, fault_every=args.fault_every)
        hardened = run_variant(temp_root / "hardened", args.tasks, attempts=2, fault_every=args.fault_every)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    payload = {
        "schema_version": 1,
        "fault": f"first_attempt_agent_process_crash_every_{args.fault_every}_tasks",
        "baseline": baseline,
        "hardened": hardened,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
