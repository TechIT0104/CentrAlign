"""The Brain is the only component that reasons. Everything around it (policy, tools, verification,
state, audit) is deterministic code, so a different model, or the offline scripted stand-in,
can be swapped in without touching the safety-critical parts."""
from __future__ import annotations

import threading
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


_scope = threading.local()  # which run the current thread is working for (set by the runtime)


class Brain(ABC):
    name = "brain"

    # -- usage accounting: tokens per run, so an eval can report what each task cost --------------
    def set_scope(self, run_id: str | None) -> None:
        _scope.run_id = run_id

    def add_usage(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> None:
        book = self.__dict__.setdefault("_usage", {})
        u = book.setdefault(getattr(_scope, "run_id", None) or "-", {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        u["calls"] += 1
        u["input"] += input_tokens
        u["output"] += output_tokens
        u["cache_read"] += cache_read
        u["cache_write"] += cache_write

    def usage_for(self, run_id: str) -> dict[str, int]:
        return dict(self.__dict__.get("_usage", {}).get(run_id, {}))

    def cost(self, usage: dict[str, int]) -> float | None:
        """Estimated USD for a usage dict; None when the brain is free or the model's price is unknown."""
        return None

    @abstractmethod
    def plan(self, req: PlanRequest) -> Plan: ...

    @abstractmethod
    def next_decision(self, ctx: StepContext) -> Decision: ...

    def derive_checks(self, goal: str, memory_text: str, answers: dict[str, str]) -> list[Check]:
        """Acceptance criteria derived from the request and the company's completion criteria ONLY.

        Called separately from plan() and never shown the plan, so the party doing the work does not
        get to define what 'done' means. Brains without an independent source return []."""
        return []

    @abstractmethod
    def judge(self, check: Check, evidence: str) -> tuple[bool, str]:
        """Fuzzy acceptance check. Receives only evidence, never the executor's narrative."""

    @abstractmethod
    def reflect(self, state: RunState, trail: str) -> list[str]:
        """Distil short, reusable lessons from a finished run."""
