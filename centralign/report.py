"""Evidence report: what was asked, what was done, what was verified, and what a human decided."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import Runtime

LABEL = {
    "completed": "COMPLETED (all acceptance checks verified)",
    "completed_with_issues": "COMPLETED WITH ISSUES (outcome not fully verified)",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}


def build_report(rt: "Runtime", run_id: str) -> dict[str, Any]:
    state = rt.store.load_run(run_id)
    events = rt.store.events(run_id)
    ok, audit = rt.store.verify_audit_chain(run_id)
    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    humans: list[dict[str, Any]] = []
    lessons, anomalies, memory, usage, cost = [], [], {}, {}, None
    started = events[0]["ts"] if events else time.time()
    ended = events[-1]["ts"] if events else started
    for e in events:
        t, d = e["type"], e["data"]
        if t == "step.started":
            current = {"id": d["step"], "description": d["description"], "actions": [], "result": None}
            steps.append(current)
        elif t == "action.executed" and current is not None:
            current["actions"].append({
                "tool": d["tool"], "args": d["args"], "ok": d["ok"], "summary": d.get("summary"),
                "error": d.get("error"), "policy": d.get("policy"), "retries": d.get("retries", 0), "artifacts": d.get("artifacts", [])})
        elif t == "step.completed" and current is not None:
            current["result"] = {"status": "skipped" if d.get("skipped") else "done", "summary": d["summary"]}
        elif t == "step.failed" and current is not None:
            current["result"] = {"status": "failed", "summary": d["reason"]}
        elif t in ("approval.granted", "approval.denied"):
            humans.append({"type": "approval", "decision": "approved" if t.endswith("granted") else "denied",
                           "what": d.get("preview"), "note": d.get("note", "")})
        elif t == "intervention.requested" and d["kind"] == "question":
            humans.append({"type": "question", "question": d["question"], "why": d.get("why", "")})
        elif t == "intervention.resolved" and d["kind"] == "question":
            humans[-1]["answer"] = d["response"].get("answer", "") if humans else ""
        elif t == "run.finished":
            usage, cost = d.get("usage") or {}, d.get("cost_usd")
        elif t == "lesson.saved":
            lessons.append(d["lesson"])
        elif t == "anomaly":
            anomalies.append(d["note"])
        elif t == "memory.recalled" and not memory:
            memory = d
    plan = state.plan.model_dump() if state.plan else None
    return {
        "run_id": run_id, "goal": state.goal, "status": state.status, "outcome": LABEL.get(state.status, state.status),
        "summary": state.summary, "duration_s": round(ended - started, 1), "plan": plan, "steps": steps,
        "checks": [c.model_dump() for c in state.checks], "human_interactions": humans, "recoveries": state.recoveries,
        "anomalies": anomalies, "lessons": lessons, "memory_used": memory, "audit": {"intact": ok, "detail": audit, "events": len(events)},
        "replans": state.replans, "usage": usage, "cost_usd": cost,
    }


def _args(a: dict) -> str:
    s = json.dumps(a, ensure_ascii=False)
    return s if len(s) < 110 else s[:107] + "..."


def render_markdown(r: dict[str, Any]) -> str:
    L = [f"# Operator report: {r['run_id']}", "", f"**Request:** {r['goal']}", "", f"**Outcome:** {r['outcome']}", "",
         f"{r['summary']}", "", f"_Duration {r['duration_s']}s | replans {r['replans']} | audit log: {r['audit']['detail']} ({r['audit']['events']} events)"
         + (f" | model tokens {r['usage']['input'] + r['usage']['output']} (~${r['cost_usd']})" if r.get("usage") and r["usage"].get("input") is not None else "") + "_", ""]
    if r["plan"]:
        L += ["## Understanding", r["plan"]["understanding"], ""]
        if r["plan"]["assumptions"]:
            L += ["Assumptions:"] + [f"- {a}" for a in r["plan"]["assumptions"]] + [""]
        if r["plan"]["sources"]:
            L += ["Company knowledge used: " + ", ".join(r["plan"]["sources"]), ""]
    L += ["## Acceptance checks (verified independently of the agent)", "", "| Check | Result | Detail |", "|---|---|---|"]
    for c in r["checks"]:
        L.append(f"| {c['description']} | {c['status'].upper()} | {c['detail'].replace('|', '/')[:140]} |")
    L.append("")
    L += ["## What the operator did"]
    for i, s in enumerate(r["steps"], 1):
        res = s["result"] or {"status": "incomplete", "summary": ""}
        L += ["", f"### {i}. {s['description']}  [{res['status']}]", res["summary"], ""]
        for a in s["actions"]:
            flag = "ok" if a["ok"] else "FAILED"
            extra = f" (retried {a['retries']}x)" if a["retries"] else ""
            L.append(f"- `{a['tool']}` {_args(a['args'])} -> {flag}{extra}, policy: {a['policy']}. {a['summary'] or a['error'] or ''}")
            for art in a["artifacts"]:
                L.append(f"  - evidence: ![{art}]({art})")
    if r["human_interactions"]:
        L += ["", "## Human involvement"]
        for h in r["human_interactions"]:
            if h["type"] == "approval":
                L.append(f"- Approval {h['decision'].upper()}: {h['what']}" + (f" (note: {h['note']})" if h["note"] else ""))
            else:
                L.append(f"- Question: {h['question']} -> answer: {h.get('answer', '(pending)')}")
    if r["recoveries"]:
        L += ["", "## Problems encountered and how they were handled"] + [f"- {x}" for x in dict.fromkeys(r["recoveries"])]
    if r["lessons"]:
        L += ["", "## Lessons saved to company memory"] + [f"- {x}" for x in r["lessons"]]
    return "\n".join(L) + "\n"


def write_report(rt: "Runtime", run_id: str) -> dict[str, Any]:
    r = build_report(rt, run_id)
    d = rt.run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(render_markdown(r), encoding="utf-8")
    (d / "report.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    return r
