"""The Brain is the only component that reasons. Everything around it (policy, tools, verification,
state, audit) is deterministic code, so a different model, or the offline scripted stand-in,
can be swapped in without touching the safety-critical parts."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import Check, Decision, HistoryItem, Plan, RunState, Step, StepResult


class BrainUnsupported(Exception):
    """Raised by the offline brain for requests it has no procedure for."""


@dataclass
class PlanRequest:
    goal: str
    memory_text: str  # rendered company context
    memory: dict[str, Any]  # raw: docs/episodes/rules
    tool_names: list[str]
    answers: dict[str, str] = field(default_factory=dict)
    guidance: list[str] = field(default_factory=list)
    prior_plan: Plan | None = None  # set when replanning
    step_results: list[StepResult] = field(default_factory=list)
    failure: str = ""  # why we are replanning (failed step or failed checks)
    search: Any = None  # callable(query) -> list[Chunk]: on-demand knowledge lookup


@dataclass
class StepContext:
    goal: str
    plan: Plan
    step: Step
    step_idx: int
    history: list[HistoryItem]
    step_results: list[StepResult]
    guidance: list[str]
    memory_text: str
    tool_specs: list[dict[str, Any]]

    @property
    def facts(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.step_results:
            out.update(r.facts)
        return out


class Brain(ABC):
    name = "brain"

    @abstractmethod
    def plan(self, req: PlanRequest) -> Plan: ...

    @abstractmethod
    def next_decision(self, ctx: StepContext) -> Decision: ...

    @abstractmethod
    def judge(self, check: Check, evidence: str) -> tuple[bool, str]:
        """Fuzzy acceptance check. Receives only evidence, never the executor's narrative."""

    @abstractmethod
    def reflect(self, state: RunState, trail: str) -> list[str]:
        """Distil short, reusable lessons from a finished run."""
