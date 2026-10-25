from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from agent_harness.runtime import AgentAdapter, AgentResult, TaskSpec

FaultStage = Literal["before_agent", "after_agent"]
FaultKind = Literal["raise", "delay"]


@dataclass(frozen=True)
class Fault:
    task_id: str
    attempt: int
    stage: FaultStage
    kind: FaultKind
    value: str | float


class FaultPlan:
    def __init__(self, faults: list[Fault]):
        self._faults = {(fault.task_id, fault.attempt, fault.stage): fault for fault in faults}

    def trigger(self, task_id: str, attempt: int, stage: FaultStage) -> None:
        fault = self._faults.get((task_id, attempt, stage))
        if fault is None:
            return
        if fault.kind == "raise":
            raise RuntimeError(str(fault.value))
        if fault.kind == "delay":
            time.sleep(float(fault.value))


class FaultInjectingAgent:
    """Deterministic wrapper used to exercise recovery without changing the underlying agent."""

    def __init__(self, delegate: AgentAdapter, plan: FaultPlan):
        self.delegate = delegate
        self.plan = plan

    def run(self, task: TaskSpec, *, attempt: int, feedback: str | None) -> AgentResult:
        self.plan.trigger(task.task_id, attempt, "before_agent")
        result = self.delegate.run(task, attempt=attempt, feedback=feedback)
        self.plan.trigger(task.task_id, attempt, "after_agent")
        return result
