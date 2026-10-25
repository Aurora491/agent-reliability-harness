"""Paired MBPP ablation: one attempt versus verifier-feedback retry.

The API key is read only from DEEPSEEK_API_KEY. This script refuses to start
without it and never writes the key into the result file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

from agent_harness.cli import load_tasks
from agent_harness.mini_swe import MiniSWEAgentAdapter
from agent_harness.process import SubprocessVerifier
from agent_harness.runtime import ReliabilityRuntime, RunResult, TaskSpec

# Peak deepseek-flash rates, cache-miss input. Used only as a conservative
# local stop so a missing LiteLLM price cannot run past the requested budget.
PEAK_INPUT_USD_PER_TOKEN = 0.30 / 1_000_000
PEAK_OUTPUT_USD_PER_TOKEN = 1.20 / 1_000_000
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]+")


def scrub(text: str | None) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


def reset_solution(task: TaskSpec) -> None:
    marker = "\nImplement the solution in solution.py."
    text = task.prompt.split(marker)[0]
    (task.workspace / "solution.py").write_text(
        f'"""Implement the following task.\n\n{text}\n"""\n',
        encoding="utf-8",
    )
    cache = task.workspace / "__pycache__"
    if cache.exists():
        for child in cache.iterdir():
            child.unlink()


def trajectory_usage(path: Path) -> tuple[int, int, float]:
    if not path.exists():
        return 0, 0, 0.0
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompt_tokens = 0
    completion_tokens = 0
    reported_cost = 0.0
    for message in payload.get("messages", []):
        extra = message.get("extra") or {}
        reported_cost += float(extra.get("cost") or 0.0)
        usage = (extra.get("response") or {}).get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
    return prompt_tokens, completion_tokens, reported_cost


def estimate_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return prompt_tokens * PEAK_INPUT_USD_PER_TOKEN + completion_tokens * PEAK_OUTPUT_USD_PER_TOKEN


def attempt_usage(directory: Path, task_id: str) -> tuple[int, int, float, float]:
    prompt_tokens = 0
    completion_tokens = 0
    reported_cost = 0.0
    for path in sorted(directory.glob(f"{task_id}-attempt-*.traj.json")):
        prompt, completion, cost = trajectory_usage(path)
        prompt_tokens += prompt
        completion_tokens += completion
        reported_cost += cost
    return prompt_tokens, completion_tokens, reported_cost, estimate_usd(prompt_tokens, completion_tokens)


def run_one(
    runtime: ReliabilityRuntime,
    task: TaskSpec,
    *,
    variant: str,
    max_attempts: int,
    completed_ids: set[str],
) -> RunResult:
    run_id = f"{variant}-{task.task_id}"
    spec = replace(task, max_attempts=max_attempts)
    if run_id not in completed_ids:
        reset_solution(spec)
    return runtime.run(spec, run_id=run_id)


def row_from_result(result: RunResult, usage: tuple[int, int, float, float]) -> dict:
    prompt_tokens, completion_tokens, reported_cost, estimated_cost = usage
    return {
        "task_id": result.task_id,
        "status": result.status,
        "attempts": result.attempts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": prompt_tokens + completion_tokens,
        "reported_cost_usd": round(reported_cost, 6),
        "estimated_peak_usd": round(estimated_cost, 6),
        "error": scrub(result.error)[:500] if result.error else None,
    }


def summarize(rows: list[dict]) -> dict:
    completed = sum(row["status"] == "completed" for row in rows)
    first_pass = sum(row["status"] == "completed" and row["attempts"] == 1 for row in rows)
    recovered = sum(row["status"] == "completed" and row["attempts"] >= 2 for row in rows)
    return {
        "tasks": len(rows),
        "completed": completed,
        "completion_rate": completed / len(rows) if rows else 0.0,
        "first_attempt_passes": first_pass,
        "recovered_after_retry": recovered,
        "mean_attempts": sum(row["attempts"] for row in rows) / len(rows) if rows else 0.0,
        "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["completion_tokens"] for row in rows),
        "tokens": sum(row["tokens"] for row in rows),
        "reported_cost_usd": round(sum(row["reported_cost_usd"] for row in rows), 6),
        "estimated_peak_usd": round(sum(row["estimated_peak_usd"] for row in rows), 6),
    }


def write_summary(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("benchmark/tasks.json"))
    parser.add_argument("--output", type=Path, default=Path("results/deepseek-ablation"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--budget-usd", type=float, default=6.0)
    parser.add_argument("--model", default="deepseek/deepseek-flash")
    parser.add_argument("--mini-executable", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("benchmark/deepseek_agent.yaml"))
    parser.add_argument("--cost-limit", type=float, default=0.08)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit(
            "DEEPSEEK_API_KEY is not set. Export it in the shell; do not pass it as an argument."
        )
    os.environ["MSWEA_CONFIGURED"] = "true"
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    venv_bin = Path(sys.executable).resolve().parent
    os.environ["PATH"] = str(venv_bin) + os.pathsep + os.environ.get("PATH", "")

    tasks = [
        replace(
            task,
            verify_command=[sys.executable, *task.verify_command[1:]]
            if task.verify_command and task.verify_command[0] == "python"
            else task.verify_command,
            timeout_seconds=args.timeout_seconds,
        )
        for task in load_tasks(args.manifest, args.limit)
    ]
    config = str(args.config.resolve())
    variants = {
        "baseline": MiniSWEAgentAdapter(
            executable=str(args.mini_executable),
            model=args.model,
            output_dir=args.output / "baseline" / "trajectories",
            cost_limit_usd=args.cost_limit,
            config_specs=("mini.yaml", config),
        ),
        "hardened": MiniSWEAgentAdapter(
            executable=str(args.mini_executable),
            model=args.model,
            output_dir=args.output / "hardened" / "trajectories",
            cost_limit_usd=args.cost_limit,
            config_specs=("mini.yaml", config),
        ),
    }
    runtimes = {
        name: ReliabilityRuntime(args.output / name / "state", adapter, SubprocessVerifier())
        for name, adapter in variants.items()
    }
    done = {name: runtime.store.completed_run_ids() for name, runtime in runtimes.items()}
    per_task: list[dict] = []
    spent = 0.0
    stopped = None

    try:
        return _run_tasks(args, tasks, variants, runtimes, done, per_task, spent, stopped)
    finally:
        for task in tasks:
            reset_solution(task)


def _run_tasks(args, tasks, variants, runtimes, done, per_task, spent, stopped) -> int:
    for task in tasks:
        record: dict = {"task_id": task.task_id}
        for variant, attempts in (("baseline", 1), ("hardened", 2)):
            projected = spent
            if projected >= args.budget_usd:
                stopped = (
                    f"stopped before {variant} {task.task_id}: "
                    f"estimated ${spent:.4f} reached budget ${args.budget_usd:.2f}"
                )
                break
            run_id = f"{variant}-{task.task_id}"
            already = run_id in done[variant]
            result = run_one(
                runtimes[variant],
                task,
                variant=variant,
                max_attempts=attempts,
                completed_ids=done[variant],
            )
            usage = attempt_usage(variants[variant].output_dir, task.task_id)
            record[variant] = row_from_result(result, usage)
            if not already:
                spent += usage[3]
            print(
                f"{variant} {task.task_id}: {result.status} attempts={result.attempts} "
                f"estimated_peak_usd={usage[3]:.4f} running=${spent:.4f}",
                flush=True,
            )
        per_task.append(record)
        payload = {
            "model": args.model,
            "thinking": "disabled",
            "temperature": 0,
            "step_limit": 6,
            "per_attempt_cost_limit_usd": args.cost_limit,
            "budget_usd": args.budget_usd,
            "pricing_assumption": "peak deepseek-flash, cache-miss input $0.30/1M, output $1.20/1M",
            "estimated_peak_usd": round(spent, 6),
            "stopped": stopped,
            "baseline": summarize([row["baseline"] for row in per_task if "baseline" in row]),
            "hardened": summarize([row["hardened"] for row in per_task if "hardened" in row]),
            "per_task": per_task,
        }
        write_summary(args.output / "summary.json", payload)
        if stopped:
            print(stopped, flush=True)
            return 0

    print(f"finished estimated_peak_usd={spent:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
