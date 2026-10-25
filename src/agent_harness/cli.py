from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_harness.demo import run_demo
from agent_harness.faults import Fault, FaultInjectingAgent, FaultPlan
from agent_harness.mini_swe import MiniSWEAgentAdapter
from agent_harness.process import SubprocessVerifier
from agent_harness.runtime import ReliabilityRuntime, TaskSpec
from agent_harness.trace import verify_trace


def load_tasks(path: Path, limit: int | None) -> list[TaskSpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload["tasks"] if isinstance(payload, dict) else payload
    if limit is not None:
        records = records[:limit]
    return [
        TaskSpec(
            task_id=record["task_id"],
            prompt=record["prompt"],
            workspace=Path(record["workspace"]).expanduser().resolve(),
            verify_command=record["verify_command"],
            max_attempts=record.get("max_attempts", 2),
            timeout_seconds=record.get("timeout_seconds", 300),
            metadata=record.get("metadata", {}),
        )
        for record in records
    ]


def run_manifest(args: argparse.Namespace) -> int:
    if args.model.startswith("deepseek/") and not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit(
            "DEEPSEEK_API_KEY is not set. Export your own key locally; do not commit it to the repository."
        )
    tasks = load_tasks(args.manifest, args.limit)
    adapter = MiniSWEAgentAdapter(
        executable=args.mini_executable,
        model=args.model,
        output_dir=args.output / "trajectories",
        cost_limit_usd=args.cost_limit,
    )
    if args.inject_crash_every:
        faults = [
            Fault(task.task_id, 1, "before_agent", "raise", "injected agent process crash")
            for index, task in enumerate(tasks, start=1)
            if index % args.inject_crash_every == 0
        ]
        agent = FaultInjectingAgent(adapter, FaultPlan(faults))
    else:
        agent = adapter
    runtime = ReliabilityRuntime(args.output / "state", agent, SubprocessVerifier())

    results = []
    for task in tasks:
        result = runtime.run(task, run_id=f"{args.variant}-{task.task_id}")
        results.append(result.serialize())
        print(f"{task.task_id}: {result.status} attempts={result.attempts}")
    summary = {
        "variant": args.variant,
        "model": args.model,
        "tasks": len(results),
        "completed": sum(row["status"] == "completed" for row in results),
        "completion_rate": sum(row["status"] == "completed" for row in results) / len(results),
        "tokens": sum((row["agent_result"] or {}).get("tokens", 0) for row in results),
        "cost_usd": sum((row["agent_result"] or {}).get("cost_usd", 0.0) for row in results),
        "results": results,
    }
    output = args.output / f"{args.variant}-summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {output}")
    return 0 if summary["completed"] == summary["tasks"] else 1


def check_trace(args: argparse.Namespace) -> int:
    valid, events = verify_trace(args.path)
    print(json.dumps({"valid": valid, "events": events}))
    return 0 if valid else 1


def demo(args: argparse.Namespace) -> int:
    print(json.dumps(run_demo(args.output), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arh")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run a JSON task manifest through mini-swe-agent")
    run.add_argument("manifest", type=Path)
    run.add_argument("--output", type=Path, default=Path("results/live"))
    run.add_argument("--variant", default="hardened")
    run.add_argument("--model", default="deepseek/deepseek-chat")
    run.add_argument("--mini-executable", default="mini")
    run.add_argument("--cost-limit", type=float, default=0.8)
    run.add_argument("--limit", type=int)
    run.add_argument("--inject-crash-every", type=int, default=0)
    run.set_defaults(handler=run_manifest)

    trace = subparsers.add_parser("verify-trace", help="Verify a hash-chained JSONL trace")
    trace.add_argument("path", type=Path)
    trace.set_defaults(handler=check_trace)

    recovery_demo = subparsers.add_parser("demo", help="Run a deterministic crash-and-resume demo")
    recovery_demo.add_argument("--output", type=Path, default=Path("demo-output"))
    recovery_demo.set_defaults(handler=demo)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
