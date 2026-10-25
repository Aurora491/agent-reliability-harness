from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

MBPP_URL = "https://raw.githubusercontent.com/google-research/google-research/master/mbpp/mbpp.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--root", type=Path, default=Path("benchmark/mbpp"))
    parser.add_argument("--manifest", type=Path, default=Path("benchmark/tasks.json"))
    args = parser.parse_args()

    with urllib.request.urlopen(MBPP_URL, timeout=30) as response:  # noqa: S310 - fixed HTTPS source
        records = [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]

    tasks = []
    for record in records[: args.count]:
        task_id = f"mbpp-{record['task_id']}"
        workspace = args.root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "solution.py").write_text(
            f'"""Implement the following task.\n\n{record["text"]}\n"""\n',
            encoding="utf-8",
        )
        tests = "\n".join(f"    {test}" for test in record["test_list"])
        (workspace / "test_solution.py").write_text(
            "from solution import *  # noqa: F403\n\n\n"
            "def test_solution():\n"
            f"{tests}\n",
            encoding="utf-8",
        )
        tasks.append(
            {
                "task_id": task_id,
                "prompt": (
                    f"{record['text']}\n"
                    "Implement the solution in solution.py. "
                    "Run the tests and make the smallest correct change."
                ),
                "workspace": str(workspace),
                "verify_command": ["python", "-m", "pytest", "-q"],
                "max_attempts": 2,
                "timeout_seconds": 300,
                "metadata": {"source": "MBPP", "source_task_id": str(record["task_id"])},
            }
        )

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(tasks)} tasks to {args.manifest}")


if __name__ == "__main__":
    main()
