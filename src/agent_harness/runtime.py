from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from agent_harness.store import RunStore
from agent_harness.trace import TraceWriter


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    workspace: Path
    verify_command: list[str]
    max_attempts: int = 2
    timeout_seconds: float = 300
    metadata: dict[str, str] = field(default_factory=dict)

    def serialize(self) -> dict:
        payload = asdict(self)
        payload["workspace"] = str(self.workspace)
        return payload


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    stdout: str
    stderr: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0

    def serialize(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    exit_code: int
    output: str
    duration_ms: float

    def serialize(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    status: str
    attempts: int
    resumed: bool
    agent_result: AgentResult | None
    verification: VerificationResult | None
    error: str | None = None

    def serialize(self) -> dict:
        payload = asdict(self)
        return payload


class AgentAdapter(Protocol):
    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult: ...


class Verifier(Protocol):
    def verify(self, task: TaskSpec) -> VerificationResult: ...


class ReliabilityRuntime:
    """Crash-safe orchestration around any coding-agent adapter."""

    def __init__(self, root: Path, agent: AgentAdapter, verifier: Verifier):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = RunStore(root / "runs.sqlite3")
        self.agent = agent
        self.verifier = verifier

    def run(self, task: TaskSpec, run_id: str | None = None) -> RunResult:
        run_id = run_id or f"{task.task_id}-{uuid.uuid4().hex[:12]}"
        existing = self.store.initialize_run(run_id, task.task_id, task.serialize())
        trace = TraceWriter(self.root / "traces" / f"{run_id}.jsonl")
        resumed = existing["status"] != "pending"

        if existing["status"] == "completed" and existing["result_json"]:
            payload = json.loads(existing["result_json"])
            return self._decode_result(payload, resumed=True)

        prior_attempts = self.store.completed_attempts(run_id)
        feedback = next(
            (attempt["error"] for attempt in reversed(prior_attempts) if attempt["error"]),
            None,
        )
        trace.append("run_started", run_id=run_id, task_id=task.task_id, resumed=resumed)

        last_agent_result: AgentResult | None = None
        last_verification: VerificationResult | None = None
        last_error: str | None = None
        start_attempt = len(prior_attempts) + 1

        for attempt in range(start_attempt, task.max_attempts + 1):
            if not self.store.start_attempt(run_id, attempt):
                continue
            trace.append("attempt_started", attempt=attempt, feedback=feedback)
            try:
                started = time.perf_counter()
                last_agent_result = self.agent.run(task, attempt=attempt, feedback=feedback)
                trace.append(
                    "agent_finished",
                    attempt=attempt,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    result=last_agent_result.serialize(),
                )
                last_verification = self.verifier.verify(task)
                trace.append("verification_finished", attempt=attempt, result=last_verification.serialize())
                attempt_payload = {
                    "agent": last_agent_result.serialize(),
                    "verification": last_verification.serialize(),
                }
                if last_verification.passed:
                    self.store.finish_attempt(run_id, attempt, status="completed", result=attempt_payload)
                    result = RunResult(
                        run_id=run_id,
                        task_id=task.task_id,
                        status="completed",
                        attempts=attempt,
                        resumed=resumed,
                        agent_result=last_agent_result,
                        verification=last_verification,
                    )
                    self.store.finish_run(run_id, "completed", result.serialize())
                    trace.append("run_completed", result=result.serialize())
                    return result
                last_error = last_verification.output[-4000:]
                feedback = last_error
                self.store.finish_attempt(
                    run_id,
                    attempt,
                    status="verification_failed",
                    result=attempt_payload,
                    error=last_error,
                )
            except BaseException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                feedback = last_error
                self.store.finish_attempt(run_id, attempt, status="failed", error=last_error)
                trace.append("attempt_failed", attempt=attempt, error=last_error)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise

        result = RunResult(
            run_id=run_id,
            task_id=task.task_id,
            status="failed",
            attempts=min(task.max_attempts, len(self.store.completed_attempts(run_id))),
            resumed=resumed,
            agent_result=last_agent_result,
            verification=last_verification,
            error=last_error,
        )
        self.store.finish_run(run_id, "failed", result.serialize())
        trace.append("run_failed", result=result.serialize())
        return result

    @staticmethod
    def _decode_result(payload: dict, *, resumed: bool) -> RunResult:
        agent = AgentResult(**payload["agent_result"]) if payload.get("agent_result") else None
        verification = VerificationResult(**payload["verification"]) if payload.get("verification") else None
        return RunResult(
            run_id=payload["run_id"],
            task_id=payload["task_id"],
            status=payload["status"],
            attempts=payload["attempts"],
            resumed=resumed,
            agent_result=agent,
            verification=verification,
            error=payload.get("error"),
        )
