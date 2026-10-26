"""Claude-backed brain: planning, step-level tool selection, fuzzy judging and reflection.

The brain is stateless: the conversation for a step is rebuilt from the recorded history on every
call. That makes runs resumable (after approvals, restarts) and keeps the audit log the single
source of truth. Reasoning only ever happens here; every effect goes through the runtime.

API notes (current models): no forced tool_choice (400), no sampling params, thinking left to the
model. We use tool_choice=auto, one tool per turn, and re-prompt if the model answers in prose.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from ..models import Action, Check, Decision, MissingInfo, Plan, RunState, Step
from .base import Brain, PlanRequest, StepContext


class BrainError(Exception):
    pass


CHECK_DOC = """Acceptance check types (verified by independent code AFTER execution, never by you):
- file_exists    {path | glob}
- file_contains  {path | glob, text}                       (glob: first matching file must contain the text)
- csv_has_row    {path, match: {column: value, ...}, count?: int}   (count = exact number of matching rows)
- http_json      {url, expect: {"dotted.path.0.field": value}}      (GET against an internal system of record;
                  read-only credentials are attached automatically for internal hosts)
- llm_judge      {question, path | glob}                   (only for qualities code cannot test, e.g. tone)
Write checks that read the real end state from the system of record, not from artefacts you produced.
Set depends_on_tool (e.g. "email.send") when a check only makes sense if a human-approved action ran."""

SYSTEM = """You are the reasoning core of CentrAlign, an autonomous AI operator that completes company tasks end to end.
You work through tools: a browser, the company drive (files/CSV), email, HTTP APIs and a knowledge base of the company's SOPs.
Principles:
- Company context (SOPs, policies, learned rules) is authoritative. Follow its procedure even when the request omits the steps.
- Never invent facts, identities or values. If a required detail is missing or ambiguous, ask a human (missing_info / ask_human).
- Prefer the smallest action that makes progress; after any action that changes state, observe the result before moving on.
- Read pages before acting on them and choose controls by purpose, not remembered labels; they change between releases.
- Credentials are secrets: type them as {{secret:NAME}}; you will never see the value.
- Some actions need human approval or are forbidden by policy. If an observation says an action was denied, do not retry it:
  adapt, or finish the step with skipped=true if the human declined and nothing else is required, or fail the step with a reason.
- If something fails, diagnose from the observation, then retry differently. Don't repeat an identical failing action.
- Respond ONLY by calling exactly one tool. Never answer in prose."""


def _api_name(tool: str) -> str:
    return tool.replace(".", "__")


def _tool_name(api: str) -> str:
    return api.replace("__", ".")


PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit your understanding of the request, any missing information, an ordered plan and acceptance checks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "understanding": {"type": "string", "description": "The outcome the requester actually wants, in one or two sentences."},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "missing_info": {
                "type": "array",
                "description": "Facts you cannot infer or find in company knowledge and that the work depends on. Empty if none.",
                "items": {"type": "object", "properties": {"key": {"type": "string"}, "question": {"type": "string"}}, "required": ["key", "question"]},
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "description": {"type": "string"}, "expected": {"type": "string"}},
                    "required": ["id", "description", "expected"],
                },
            },
            "acceptance": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "type": {"type": "string", "enum": ["file_exists", "file_contains", "csv_has_row", "http_json", "llm_judge"]},
                        "params": {"type": "object"},
                        "depends_on_tool": {"type": "string"},
                    },
                    "required": ["id", "description", "type", "params"],
                },
            },
        },
        "required": ["understanding", "steps", "acceptance"],
    },
}

CONTROL_TOOLS = [
    {
        "name": "step_done",
        "description": "Declare the CURRENT step complete, only after observing evidence that its expected result holds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was done and the evidence seen."},
                "facts": {"type": "object", "description": "Values later steps need (ids, emails, paths)."},
                "skipped": {"type": "boolean", "description": "True if the intended effect was intentionally not performed."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "step_failed",
        "description": "Declare the current step cannot be completed after trying reasonable alternatives.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "ask_human",
        "description": "Ask a human for information or a decision you cannot determine yourself.",
        "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
]


class ClaudeBrain(Brain):
    name = "claude"

    def __init__(self, model: str, client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    # ------------------------------------------------------------------ plumbing
    def _call(self, messages: list[dict], tools: list[dict], effort: str, max_tokens: int, system: str = SYSTEM) -> tuple[Any, str]:
        """One model turn that must end in a tool call. Returns (tool_use_block, preamble_text)."""
        msgs = list(messages)
        for _ in range(3):
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=msgs,
                tools=tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config={"effort": effort},
            )
            if resp.stop_reason == "refusal":
                raise BrainError(f"model declined the request ({getattr(resp.stop_details, 'category', None)})")
            text = "".join(b.text for b in resp.content if b.type == "text")
            tool = next((b for b in resp.content if b.type == "tool_use"), None)
            if tool is not None:
                return tool, text.strip()
            msgs += [
                {"role": "assistant", "content": text or "(no output)"},
                {"role": "user", "content": "Respond by calling exactly one of the provided tools."},
            ]
        raise BrainError("model did not call a tool after 3 attempts")

    @staticmethod
    def _plan_text(plan: Plan, idx: int, results) -> str:
        done = {r.step_id: r for r in results}
        lines = []
        for i, s in enumerate(plan.steps):
            mark = "DONE" if s.id in done and done[s.id].status != "failed" else ("<- CURRENT" if i == idx else "todo")
            extra = f" -> {done[s.id].summary}" if s.id in done else ""
            lines.append(f"{i + 1}. [{mark}] {s.description} (expect: {s.expected}){extra}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ planning
    def plan(self, req: PlanRequest) -> Plan:
        parts = [f"# Request\n{req.goal}", f"# Company context\n{req.memory_text}",
                 f"# Tools available\n{', '.join(req.tool_names)}", CHECK_DOC]
        if req.answers:
            parts.append("# Answers already provided by humans\n" + "\n".join(f"- {k}: {v}" for k, v in req.answers.items()))
        if req.guidance:
            parts.append("# Human guidance and feedback for this run\n" + "\n".join(f"- {g}" for g in req.guidance))
        if req.prior_plan:
            done = "\n".join(f"- {r.step_id}: {r.status}: {r.summary}" for r in req.step_results) or "(none)"
            parts.append(f"# This is a REPLAN\nPrior plan understanding: {req.prior_plan.understanding}\n"
                         f"Work already completed (do not redo it unless it is wrong):\n{done}\nProblem to fix: {req.failure}\n"
                         "Produce a revised plan covering only the remaining work, plus acceptance checks for the whole outcome.")
        parts.append("Call submit_plan. If something essential is unknown and cannot be derived from the company context, "
                     "list it in missing_info and leave steps empty.")
        tool, _ = self._call([{"role": "user", "content": "\n\n".join(parts)}], [PLAN_TOOL], "high", 8000)
        if tool.name != "submit_plan":
            raise BrainError(f"expected submit_plan, got {tool.name}")
        d = tool.input
        return Plan(
            understanding=d.get("understanding", ""),
            assumptions=d.get("assumptions", []),
            missing_info=[MissingInfo(**m) for m in d.get("missing_info", [])],
            steps=[Step(**{k: s[k] for k in ("id", "description", "expected") if k in s}) for s in d.get("steps", [])],
            acceptance=[Check(**c) for c in d.get("acceptance", [])],
            sources=[c.ref for c in req.memory["docs"]],
        )

    def derive_checks(self, goal: str, memory_text: str, answers: dict[str, str]) -> list[Check]:
        tool_def = {
            "name": "submit_checks",
            "description": "Submit acceptance checks that must hold for the request to count as completed.",
            "input_schema": {"type": "object", "properties": {"acceptance": PLAN_TOOL["input_schema"]["properties"]["acceptance"]},
                             "required": ["acceptance"]},
        }
        prompt = (f"# Request
{goal}

# Company context
{memory_text}

"
                  + ("# Answers from humans
" + "
".join(f"- {k}: {v}" for k, v in answers.items()) + "

" if answers else "")
                  + CHECK_DOC + "

You are the QA owner. You have NOT seen any plan and must not assume how the work will be done. "
                  "From the request and the company's own completion criteria (SOPs), define the checks that prove the "
                  "outcome in the real systems of record. Call submit_checks.")
        tool, _ = self._call([{"role": "user", "content": prompt}], [tool_def], "high", 4000,
                             system="You define independent acceptance criteria for work done by someone else. Respond only by calling the tool.")
        return [Check(**c) for c in tool.input.get("acceptance", [])]

    # ----------------------------------------------------------------- execution
    def next_decision(self, ctx: StepContext) -> Decision:
        results = "\n".join(f"- {r.step_id} ({r.status}): {r.summary}" for r in ctx.step_results) or "(none yet)"
        facts = json.dumps(ctx.facts, default=str) if ctx.facts else "{}"
        intro = (
            f"# Overall request\n{ctx.goal}\n\n# Plan\n{self._plan_text(ctx.plan, ctx.step_idx, ctx.step_results)}\n\n"
            f"# Completed so far\n{results}\nFacts recorded: {facts}\n\n"
            f"# Company context relevant to this step\n{ctx.memory_text}\n\n"
            + ("# Human guidance\n" + "\n".join(f"- {g}" for g in ctx.guidance) + "\n\n" if ctx.guidance else "")
            + f"# Your current step\n{ctx.step.description}\nExpected result: {ctx.step.expected}\n\n"
            "Work ONLY on this step. Choose the next action, or call step_done / step_failed / ask_human."
        )
        messages: list[dict] = [{"role": "user", "content": intro}]
        for i, h in enumerate(ctx.history):
            block = {"type": "tool_use", "id": f"toolu_{i:03d}", "name": _api_name(h.action.tool), "input": h.action.args}
            content: list[dict] = ([{"type": "text", "text": h.action.thought}] if h.action.thought else []) + [block]
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": block["id"], "content": h.observation.render(),
                 "is_error": not h.observation.ok}]})
        tools = [{**s, "name": _api_name(s["name"])} for s in ctx.tool_specs] + CONTROL_TOOLS
        tool, thought = self._call(messages, tools, "medium", 4096)
        name, args = _tool_name(tool.name), dict(tool.input)
        if name == "step_done":
            return Decision(kind="done", summary=args.get("summary", ""), facts=args.get("facts") or {}, skipped=bool(args.get("skipped")))
        if name == "step_failed":
            return Decision(kind="failed", summary=args.get("reason", ""))
        if name == "ask_human":
            return Decision(kind="ask", question=args.get("question", ""))
        return Decision(kind="act", action=Action(tool=name, args=args, thought=thought))

    # ------------------------------------------------------ verification & learning
    def judge(self, check: Check, evidence: str) -> tuple[bool, str]:
        tool_def = {
            "name": "verdict",
            "description": "Give your verdict on the acceptance criterion based only on the evidence.",
            "input_schema": {"type": "object", "properties": {"passed": {"type": "boolean"}, "reasoning": {"type": "string"}},
                             "required": ["passed", "reasoning"]},
        }
        prompt = (f"Acceptance criterion: {check.description}\nQuestion: {check.params.get('question', check.description)}\n\n"
                  f"Evidence:\n---\n{evidence[:12000]}\n---\nBe strict: pass only if the evidence clearly satisfies the criterion. Call verdict.")
        tool, _ = self._call([{"role": "user", "content": prompt}], [tool_def], "medium", 1024,
                             system="You are an impartial QA reviewer. Judge only from the evidence given. Respond only by calling the tool.")
        return bool(tool.input.get("passed")), str(tool.input.get("reasoning", ""))

    def reflect(self, state: RunState, trail: str) -> list[str]:
        tool_def = {
            "name": "save_lessons",
            "description": "Save 0-4 short, reusable operational lessons for future runs of similar tasks.",
            "input_schema": {"type": "object", "properties": {"lessons": {"type": "array", "items": {"type": "string"}}}, "required": ["lessons"]},
        }
        prompt = (f"Request: {state.goal}\nOutcome: {state.status}\nTrail of what happened:\n{trail[:9000]}\n\n"
                  "What would a future operator doing a similar task want to know? Only include things learned from this run "
                  "(quirks of systems, failures and what fixed them, human preferences). Skip generic advice. Call save_lessons.")
        tool, _ = self._call([{"role": "user", "content": prompt}], [tool_def], "low", 1024)
        return [str(x) for x in tool.input.get("lessons", [])][:4]
