"""Evaluation harness: run tasks x failure conditions x repeats and report what reliability actually costs.

Success is judged twice: by the run's own verifier (did it believe it finished?) and by *ground-truth checks
written by the task author*, evaluated after the run against the real systems. A run only counts as a
success if its status is one the task allows AND every ground-truth check passes, so an agent that claims
success without doing the work scores zero.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import Settings
from .demo import chaos_from_names, prepare_demo
from .models import Check, RunState
from .runtime import Runtime


@dataclass
class TaskResult:
    task: str
    condition: str
    repeat: int
    status: str
    success: bool
    expected: list[str]
    actions: int = 0
    approvals: int = 0
    questions: int = 0
    replans: int = 0
    retries: int = 0
    anomalies: int = 0
    checks: str = "-"
    ground_truth: str = "-"
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    note: str = ""
    failed_truth: list[str] = field(default_factory=list)

    @property
    def interventions(self) -> int:
        return self.approvals + self.questions


def load_tasks(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def make_human(task: dict[str, Any]) -> Callable:
    """A scripted reviewer: approves (except tools the task says to deny), answers questions in order,
    and aborts when the operator escalates because it is stuck."""
    answers = list(task.get("answers", []))
    deny = set(task.get("deny", []))
    approve = task.get("approve", True)

    def on_wait(rt: Runtime, run_id: str, iv: dict[str, Any]) -> None:
        if iv["kind"] == "approval":
            tool = iv["payload"]["action"]["tool"]
            rt.resolve(iv["id"], approved=bool(approve and tool not in deny), note="")
        elif iv["payload"].get("purpose") == "escalation":
            rt.resolve(iv["id"], answer="abort")
        else:
            rt.resolve(iv["id"], answer=answers.pop(0) if answers else "stop")

    return on_wait


def _expected(task: dict[str, Any], brain: str) -> list[str]:
    exp = task.get("expect", ["completed"])
    if isinstance(exp, dict):
        exp = exp.get(brain, exp.get("default", ["completed"]))
    return [exp] if isinstance(exp, str) else list(exp)


def _ground_truth(rt: Runtime, task: dict[str, Any]) -> tuple[int, int, list[str]]:
    raw = json.dumps(task.get("ground_truth", [])).replace("{acmehr}", rt.s.acmehr_url)
    checks = [Check(**c) for c in json.loads(raw)]
    if not checks:
        return 0, 0, []
    results = rt.verifier.verify(checks, RunState(goal="ground truth"))
    failed = [f"{r.description}: {r.detail}" for r in results if r.status != "passed"]
    return len(results) - len(failed), len(results), failed


def run_one(rt: Runtime, task: dict[str, Any], condition: str, repeat: int, brain: str) -> TaskResult:
    expected = _expected(task, brain)
    prepare_demo(rt.s, chaos_from_names(condition), fresh=True)
    t0 = time.time()
    try:
        state = rt.run(task["goal"], on_wait=make_human(task))
        run_id = rt.store.list_runs(1)[0]["id"]
        err = ""
    except Exception as e:  # an eval must report a crash, not die with it
        return TaskResult(task["id"], condition or "none", repeat, "error", False, expected, note=f"{type(e).__name__}: {e}")
    ev = rt.store.events(run_id)
    fin = next((e["data"] for e in reversed(ev) if e["type"] == "run.finished"), {})
    usage = fin.get("usage") or {}
    ok_gt, n_gt, failed = _ground_truth(rt, task)
    passed = sum(c.status == "passed" for c in state.checks)
    return TaskResult(
        task=task["id"], condition=condition or "none", repeat=repeat, status=state.status,
        success=state.status in expected and not failed, expected=expected,
        actions=sum(e["type"] == "action.executed" for e in ev),
        approvals=sum(e["type"] == "intervention.requested" and e["data"]["kind"] == "approval" for e in ev),
        questions=sum(e["type"] == "intervention.requested" and e["data"]["kind"] == "question" for e in ev),
        replans=state.replans, retries=sum(e["data"].get("retries", 0) for e in ev if e["type"] == "action.executed"),
        anomalies=sum(e["type"] == "anomaly" for e in ev),
        checks=f"{passed}/{len(state.checks)}", ground_truth=f"{ok_gt}/{n_gt}" if n_gt else "-",
        seconds=round(time.time() - t0, 1), input_tokens=usage.get("input", 0), output_tokens=usage.get("output", 0),
        cost_usd=fin.get("cost_usd"), note=err, failed_truth=failed,
    )


def run_eval(rt: Runtime, spec: dict[str, Any], conditions: list[str] | None = None, repeats: int = 1,
             only: str | None = None, progress: Callable[[TaskResult], None] | None = None) -> list[TaskResult]:
    brain = rt.brain.name
    out: list[TaskResult] = []
    for task in spec["tasks"]:
        if only and only not in task["id"]:
            continue
        if task.get("requires") and task["requires"] != brain:
            r = TaskResult(task["id"], "-", 0, "skipped", False, [], note=f"needs the {task['requires']} brain")
            out.append(r)
            progress and progress(r)
            continue
        for cond in (task.get("conditions") or conditions or spec.get("conditions") or [""]):
            for i in range(1, repeats + 1):
                r = run_one(rt, task, cond, i, brain)
                out.append(r)
                progress and progress(r)
    return out


def summarize(results: list[TaskResult]) -> dict[str, Any]:
    ran = [r for r in results if r.status != "skipped"]
    costs = [r.cost_usd for r in ran if r.cost_usd is not None]
    return {
        "runs": len(ran), "skipped": len(results) - len(ran), "successes": sum(r.success for r in ran),
        "success_rate": round(sum(r.success for r in ran) / len(ran), 3) if ran else None,
        "avg_actions": round(statistics.mean(r.actions for r in ran), 1) if ran else None,
        "avg_interventions": round(statistics.mean(r.interventions for r in ran), 2) if ran else None,
        "avg_replans": round(statistics.mean(r.replans for r in ran), 2) if ran else None,
        "total_tokens": sum(r.input_tokens + r.output_tokens for r in ran),
        "total_cost_usd": round(sum(costs), 4) if costs else None,
        "cost_per_success_usd": round(sum(costs) / max(1, sum(r.success for r in ran)), 4) if costs else None,
    }


def render_markdown(results: list[TaskResult], summary: dict[str, Any], meta: dict[str, Any]) -> str:
    L = [f"# Eval report ({meta['brain']} brain{', ' + meta['model'] if meta.get('model') else ''})", "",
         f"Date {meta['date']} | repeats {meta['repeats']} | conditions {meta['conditions']}", "",
         "| Task | Failure condition | Result | Status | Actions | Human | Replans | Checks | Ground truth | Tokens | Cost |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.status == "skipped":
            L.append(f"| {r.task} | - | skipped | {r.note} | | | | | | | |")
            continue
        cost = f"${r.cost_usd:.3f}" if r.cost_usd is not None else "-"
        L.append(f"| {r.task} | {r.condition} | {'PASS' if r.success else 'FAIL'} | {r.status} | {r.actions} | "
                 f"{r.interventions} | {r.replans} | {r.checks} | {r.ground_truth} | {r.input_tokens + r.output_tokens} | {cost} |")
    s = summary
    L += ["", "## Summary", f"- Success rate: **{s['successes']}/{s['runs']}** ({(s['success_rate'] or 0) * 100:.0f}%), {s['skipped']} skipped",
          f"- Average per run: {s['avg_actions']} actions, {s['avg_interventions']} human interventions, {s['avg_replans']} replans",
          f"- Tokens: {s['total_tokens']}" + (f" | estimated cost ${s['total_cost_usd']} (${s['cost_per_success_usd']} per success)" if s["total_cost_usd"] is not None else " | no model cost (offline brain)")]
    fails = [r for r in results if r.failed_truth]
    if fails:
        L += ["", "## Ground-truth failures"] + [f"- {r.task} [{r.condition}]: " + "; ".join(r.failed_truth) for r in fails]
    return "\n".join(L) + "\n"


def save(rt: Runtime, results: list[TaskResult], summary: dict[str, Any], meta: dict[str, Any]) -> Path:
    d = rt.s.home / "evals"
    d.mkdir(parents=True, exist_ok=True)
    stem = d / time.strftime("%Y%m%d-%H%M%S")
    stem.with_suffix(".md").write_text(render_markdown(results, summary, meta), encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": summary, "results": [asdict(r) for r in results]}, indent=2), encoding="utf-8")
    return stem.with_suffix(".md")
