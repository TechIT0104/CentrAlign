"""Typed data model shared by the runtime, brains, tools and UI."""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- plan


class Check(BaseModel):
    """A machine-verifiable acceptance criterion. Verified independently of the executor."""

    id: str
    description: str
    type: Literal["file_exists", "file_contains", "csv_has_row", "http_json", "llm_judge"]
    params: dict[str, Any] = Field(default_factory=dict)
    # If a human/policy denies this tool, the check is skipped instead of failed.
    depends_on_tool: str | None = None


class Step(BaseModel):
    id: str
    description: str
    expected: str = ""
    kind: str = ""  # optional hint, used by the offline brain


class MissingInfo(BaseModel):
    key: str
    question: str


class Plan(BaseModel):
    understanding: str
    assumptions: list[str] = Field(default_factory=list)
    missing_info: list[MissingInfo] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    acceptance: list[Check] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # knowledge chunks the plan relied on


# ---------------------------------------------------------------- execution


class Action(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    thought: str = ""


class Observation(BaseModel):
    ok: bool
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    retryable: bool = False
    artifacts: list[str] = Field(default_factory=list)  # evidence paths, relative to run dir

    def render(self, limit: int = 3500) -> str:
        body = {"ok": self.ok, "summary": self.summary}
        if self.error:
            body["error"] = self.error
        if self.data:
            body["data"] = self.data
        text = json.dumps(body, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[: limit - 40] + "…[truncated]"


class HistoryItem(BaseModel):
    action: Action
    observation: Observation
    policy: str = "auto"
    ts: float = Field(default_factory=time.time)


class Decision(BaseModel):
    """What the brain wants to happen next within the current step."""

    kind: Literal["act", "done", "failed", "ask"]
    action: Action | None = None
    summary: str = ""  # done / failed reason
    question: str = ""  # ask
    facts: dict[str, Any] = Field(default_factory=dict)  # done: values later steps need
    skipped: bool = False  # done, but the intended effect was intentionally not performed


class StepResult(BaseModel):
    step_id: str
    status: Literal["done", "skipped", "failed"]
    summary: str
    facts: dict[str, Any] = Field(default_factory=dict)
    actions: int = 0


class CheckResult(BaseModel):
    check_id: str
    description: str
    status: Literal["passed", "failed", "skipped"]
    detail: str = ""
    evidence: str = ""


Status = Literal[
    "running", "waiting_approval", "waiting_human", "completed", "completed_with_issues", "failed", "cancelled"
]
Phase = Literal["planning", "executing", "verifying", "finished"]


class RunState(BaseModel):
    goal: str
    phase: Phase = "planning"
    status: Status = "running"
    plan: Plan | None = None
    step_idx: int = 0
    history: list[HistoryItem] = Field(default_factory=list)  # current step only
    step_results: list[StepResult] = Field(default_factory=list)
    replans: int = 0
    verify_rounds: int = 0
    guidance: list[str] = Field(default_factory=list)  # human answers/feedback applied to later reasoning
    answers: dict[str, str] = Field(default_factory=dict)  # missing_info key -> answer
    pending_intervention: int | None = None
    pending_action: Action | None = None
    denied_tools: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    recoveries: list[str] = Field(default_factory=list)  # human-readable recovery notes
    summary: str = ""
    failure: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "completed_with_issues", "failed", "cancelled")

    @property
    def waiting(self) -> bool:
        return self.status in ("waiting_approval", "waiting_human")
