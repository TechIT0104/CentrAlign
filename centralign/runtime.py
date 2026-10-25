"""The operator runtime: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

State lives in SQLite after every tick, so a run can pause for a human (approval / question) and
resume later, in another thread or process. The brain proposes; this module disposes: every action
passes through the policy engine, every effect is audited, and "done" is decided by the verifier.
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .brains import Brain, BrainUnsupported, PlanRequest, StepContext, make_brain
from .config import Settings
from .events import Recorder
from .memory import Memory
from .models import Action, HistoryItem, Observation, Plan, RunState, StepResult
from .policy import Policy
from .store import Store
from .tools import BrowserManager, Registry, ToolContext, build_registry
from .verifier import Verifier


class Runtime:
    def __init__(self, settings: Settings | None = None, brain: Brain | None = None, store: Store | None = None,
                 registry: Registry | None = None):
        self.s = settings or Settings.load()
        self.s.ensure_dirs()
        self.store = store or Store(self.s.db_path)
        self.memory = Memory(self.s.knowledge_dir, self.store)
        self.policy = Policy.load(self.s.policy_path)
        self.registry = registry or build_registry()
        self.browsers = BrowserManager(self.s.headless)
        self.brain = brain or make_brain(self.s)
        self.verifier = Verifier(self.s, self.brain)
        self._locks: dict[str, threading.Lock] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="run")
        self._glock = threading.Lock()
        self.listeners: list = []  # callbacks(type, data) fed with every audit event, e.g. live CLI/UI traces

    # ------------------------------------------------------------- public API
    def submit(self, goal: str) -> str:
        run_id = self.store.create_run(goal)
        self._rec(run_id)("run.created", goal=goal, brain=self.brain.name, model=getattr(self.brain, "model", None))
        return run_id

    def submit_background(self, goal: str) -> str:
        run_id = self.submit(goal)
        self.advance_background(run_id)
        return run_id

    def advance_background(self, run_id: str) -> None:
        self._pool.submit(self.advance, run_id)

    def state(self, run_id: str) -> RunState:
        return self.store.load_run(run_id)

    def run(self, goal: str, on_wait=None) -> RunState:
        """Synchronous convenience: advance until done. `on_wait(runtime, run_id, intervention)` must resolve
        the pending intervention (CLI prompt, test double...); without it the run stops when it needs a human."""
        run_id = self.submit(goal)
        return self.drive(run_id, on_wait)

    def drive(self, run_id: str, on_wait=None) -> RunState:
        while True:
            state = self.advance(run_id)
            if state.terminal or not state.waiting or on_wait is None:
                return state
            for iv in self.store.pending_interventions(run_id):
                on_wait(self, run_id, iv)

    def cancel(self, run_id: str) -> None:
        with self._lock(run_id):
            state = self.store.load_run(run_id)
            if not state.terminal:
                state.status, state.phase = "cancelled", "finished"
                self.store.save_run(run_id, state)
                self._rec(run_id)("run.cancelled")
                self.browsers.close(run_id)

    def resolve(self, intervention_id: int, **response: Any) -> None:
        """Record a human's answer to an approval request or a question. Call advance() afterwards."""
        iv = self.store.get_intervention(intervention_id)
        run_id = iv["run_id"]
        with self._lock(run_id):
            state = self.store.load_run(run_id)
            if state.pending_intervention != intervention_id or iv["status"] != "pending":
                raise ValueError(f"intervention {intervention_id} is not awaiting a response")
            rec = self._rec(run_id)
            payload = iv["payload"]
            if iv["kind"] == "approval":
                self._resolve_approval(run_id, state, rec, payload, response)
            elif self._resolve_question(state, payload, str(response.get("answer", "")).strip()):
                self.store.resolve_intervention(intervention_id, response)
                rec("intervention.resolved", id=intervention_id, kind=iv["kind"], response=response)
                state.pending_intervention = None
                return self._finish(run_id, state, rec, "failed", "The task was aborted at a human's request.")
            self.store.resolve_intervention(intervention_id, response)
            rec("intervention.resolved", id=intervention_id, kind=iv["kind"], response=response)
            state.pending_intervention, state.pending_action = None, None
            if not state.terminal:
                state.status = "running"
            self.store.save_run(run_id, state)

    def advance(self, run_id: str) -> RunState:
        """Run ticks until the run finishes or needs a human. Safe to call from any thread."""
        with self._lock(run_id):
            state = self.store.load_run(run_id)
            rec = self._rec(run_id)
            try:
                while not state.terminal and not state.waiting:
                    self._tick(run_id, state, rec)
                    self.store.save_run(run_id, state)
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                rec("run.error", error=detail, trace=traceback.format_exc()[-1500:])
                self._finish(run_id, state, rec, "failed", f"The operator stopped because of an internal error: {detail}")
                self.store.save_run(run_id, state)
            if state.terminal:
                self.browsers.close(run_id)
            return state

    # --------------------------------------------------------------- plumbing
    def _lock(self, run_id: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(run_id, threading.Lock())

    def _rec(self, run_id: str) -> Recorder:
        r = Recorder(self.store, run_id)
        r.listeners = self.listeners
        return r

    def _tctx(self, run_id: str, rec: Recorder) -> ToolContext:
        run_dir = self.s.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return ToolContext(self.s, run_id, run_dir, self.memory, self.browsers, rec)

    def run_dir(self, run_id: str):
        return self.s.runs_dir / run_id

    # ------------------------------------------------------------------- tick
    def _tick(self, run_id: str, state: RunState, rec: Recorder) -> None:
        if state.phase == "planning":
            self._plan(run_id, state, rec)
        elif state.phase == "executing":
            self._execute(run_id, state, rec)
        elif state.phase == "verifying":
            self._verify(run_id, state, rec)

    # --------------------------------------------------------------- planning
    def _plan_request(self, state: RunState, **kw) -> PlanRequest:
        ctx = self.memory.recall(state.goal + " " + " ".join(state.answers.values()))
        return PlanRequest(goal=state.goal, memory_text=Memory.render(ctx), memory=ctx, tool_names=self.registry.names(),
                           answers=dict(state.answers), guidance=list(state.guidance), step_results=list(state.step_results),
                           search=lambda q: self.memory.knowledge.search(q, k=4), **kw)

    def _plan(self, run_id: str, state: RunState, rec: Recorder) -> None:
        rec("plan.started")
        req = self._plan_request(state)
        rec("memory.recalled", docs=[c.ref for c in req.memory["docs"]], rules=req.memory["rules"],
            episodes=[e["goal"] for e in req.memory["episodes"]])
        try:
            plan = self.brain.plan(req)
        except BrainUnsupported as e:
            return self._finish(run_id, state, rec, "failed", str(e))
        unanswered = [m for m in plan.missing_info if m.key not in state.answers]
        if unanswered:
            m = unanswered[0]
            iid = self.store.create_intervention(run_id, "question", {"purpose": "missing_info", "key": m.key, "question": m.question})
            state.pending_intervention, state.status = iid, "waiting_human"
            state.plan = plan
            rec("intervention.requested", id=iid, kind="question", question=m.question, why="required information is missing from the request")
            return
        if not plan.steps:
            return self._finish(run_id, state, rec, "failed", "The planner could not produce any steps for this request.")
        state.plan, state.phase, state.step_idx, state.history = plan, "executing", 0, []
        rec("plan.created", plan=plan.model_dump())

    def _replan(self, run_id: str, state: RunState, rec: Recorder, reason: str) -> None:
        assert state.plan
        state.replans += 1
        rec("plan.replanning", reason=reason, attempt=state.replans)
        req = self._plan_request(state, prior_plan=state.plan, failure=reason)
        try:
            new = self.brain.plan(req)
        except BrainUnsupported as e:
            return self._finish(run_id, state, rec, "failed", str(e))
        done_kinds = {r.kind for r in state.step_results if r.kind and r.status != "failed"}
        new.steps = [s.model_copy(update={"id": f"r{state.replans}-{s.id}"}) for s in new.steps if not (s.kind and s.kind in done_kinds)]
        state.plan, state.step_idx, state.history = new, 0, []
        state.phase = "executing" if new.steps else "verifying"
        rec("plan.revised", plan=new.model_dump())

    # -------------------------------------------------------------- execution
    def _execute(self, run_id: str, state: RunState, rec: Recorder) -> None:
        plan = state.plan
        assert plan
        if state.step_idx >= len(plan.steps):
            state.phase = "verifying"
            return
        step = plan.steps[state.step_idx]
        if state.started_step != step.id:
            state.started_step = step.id
            rec("step.started", step=step.id, description=step.description, index=state.step_idx + 1, of=len(plan.steps))
        if len(state.history) >= self.s.max_actions_per_step:
            return self._step_failed(run_id, state, rec, f"action budget of {self.s.max_actions_per_step} exhausted without completing the step")
        if self._looping(state.history):
            return self._step_failed(run_id, state, rec, "the same action failed repeatedly; stopping to avoid a loop")
        mem = self.memory.recall(f"{step.description} {state.goal}", k_docs=3, k_episodes=1)
        decision = self.brain.next_decision(StepContext(
            goal=state.goal, plan=plan, step=step, step_idx=state.step_idx, history=list(state.history),
            step_results=list(state.step_results), guidance=list(state.guidance), memory_text=Memory.render(mem),
            tool_specs=self.registry.specs()))
        if decision.kind == "done":
            state.step_results.append(StepResult(step_id=step.id, kind=step.kind, status="skipped" if decision.skipped else "done",
                                                 summary=decision.summary, facts=decision.facts, actions=len(state.history)))
            rec("step.completed", step=step.id, summary=decision.summary, skipped=decision.skipped, actions=len(state.history))
            state.step_idx, state.history = state.step_idx + 1, []
            if state.step_idx >= len(plan.steps):
                state.phase = "verifying"
        elif decision.kind == "failed":
            self._step_failed(run_id, state, rec, decision.summary or "step failed")
        elif decision.kind == "ask":
            iid = self.store.create_intervention(run_id, "question", {"purpose": "ask_human", "question": decision.question, "step": step.id})
            state.pending_intervention, state.status = iid, "waiting_human"
            rec("intervention.requested", id=iid, kind="question", question=decision.question, why="the operator needs a human decision")
        else:
            assert decision.action
            self._act(run_id, state, rec, decision.action)

    @staticmethod
    def _looping(history: list[HistoryItem]) -> bool:
        last = history[-3:]
        return len(last) == 3 and not any(h.observation.ok for h in last) and \
            len({(h.action.tool, str(sorted(h.action.args.items()))) for h in last}) == 1

    def _step_failed(self, run_id: str, state: RunState, rec: Recorder, reason: str) -> None:
        step = state.plan.steps[state.step_idx]  # type: ignore[union-attr]
        rec("step.failed", step=step.id, reason=reason)
        if state.replans < self.s.max_replans:
            state.recoveries.append(f"Step '{step.description}' failed ({reason}); replanned the remaining work.")
            return self._replan(run_id, state, rec, f"step '{step.description}' failed: {reason}")
        if state.escalations < 2:
            state.escalations += 1
            q = (f"I'm stuck on '{step.description}': {reason}. I've already replanned {state.replans} time(s). "
                 "Tell me what to try, or reply 'abort' to stop the task.")
            iid = self.store.create_intervention(run_id, "question", {"purpose": "escalation", "question": q, "step": step.id})
            state.pending_intervention, state.status = iid, "waiting_human"
            rec("intervention.requested", id=iid, kind="question", question=q, why="automatic recovery exhausted; escalating")
            return
        state.step_results.append(StepResult(step_id=step.id, kind=step.kind, status="failed", summary=reason, actions=len(state.history)))
        self._finish(run_id, state, rec, "failed", f"Could not complete step '{step.description}': {reason}")

    # --------------------------------------------------------------- actions
    def _act(self, run_id: str, state: RunState, rec: Recorder, action: Action) -> None:
        tctx = self._tctx(run_id, rec)
        tool = self.registry.get(action.tool)
        rec("action.proposed", tool=action.tool, args=action.args, thought=action.thought)
        if tool is None:
            obs = Observation(ok=False, error=f"unknown tool {action.tool!r}; use one of {self.registry.names()}",
                              summary=f"unknown tool {action.tool}")
            return self._record(run_id, state, rec, action, obs, "error")
        try:
            preview = tool.preview(action.args, tctx)
        except Exception as e:  # preview must never block the run, but a failure means we cannot vet it
            preview = {"text": f"(preview unavailable: {e})"}
        verdict = self.policy.decide(action, tool.risk_for(action.args), preview)
        rec("policy.decision", tool=action.tool, verdict=verdict.verdict, rule=verdict.rule, reason=verdict.reason, preview=preview.get("text"))
        if verdict.verdict == "deny":
            state.denied_tools.append(action.tool)
            obs = Observation(ok=False, error=f"denied by policy ({verdict.rule}): {verdict.reason}", summary="denied by policy", data={"denied": True})
            return self._record(run_id, state, rec, action, obs, "deny")
        if verdict.verdict == "approve":
            step = state.plan.steps[state.step_idx]  # type: ignore[union-attr]
            iid = self.store.create_intervention(run_id, "approval", {
                "action": action.model_dump(), "preview": preview.get("text"), "rule": verdict.rule,
                "reason": verdict.reason, "step": step.description})
            state.pending_intervention, state.pending_action, state.status = iid, action, "waiting_approval"
            rec("intervention.requested", id=iid, kind="approval", tool=action.tool, preview=preview.get("text"),
                reason=verdict.reason, rule=verdict.rule)
            return
        self._execute_action(run_id, state, rec, action, "auto")

    def _execute_action(self, run_id: str, state: RunState, rec: Recorder, action: Action, policy: str) -> None:
        tctx = self._tctx(run_id, rec)
        obs, retries = self.registry.execute(action, tctx, retries=self.s.max_tool_retries)
        if retries and obs.ok:
            state.recoveries.append(f"{action.tool}: transient failure, recovered automatically after {retries} retry(ies).")
        self._record(run_id, state, rec, action, obs, policy, retries)

    def _record(self, run_id: str, state: RunState, rec: Recorder, action: Action, obs: Observation, policy: str, retries: int = 0) -> None:
        rec("action.executed", tool=action.tool, args=action.args, ok=obs.ok, summary=obs.summary, error=obs.error,
            artifacts=obs.artifacts, retries=retries, policy=policy, data={k: v for k, v in obs.data.items() if k in ("url", "http_status")})
        self._note_anomalies(state, rec, action, obs)
        state.history.append(HistoryItem(action=action, observation=obs, policy=policy))

    def _note_anomalies(self, state: RunState, rec: Recorder, action: Action, obs: Observation) -> None:
        """Surface environment surprises so they show up in the evidence report as handled/unhandled."""
        step = state.plan.steps[state.step_idx] if state.plan and state.step_idx < len(state.plan.steps) else None
        notes = []
        status = obs.data.get("http_status")
        if isinstance(status, int) and status >= 500:
            notes.append(f"server error HTTP {status} at {obs.data.get('url')}")
        url = str(obs.data.get("url", ""))
        if "/login" in url and step and not any(w in step.description.lower() for w in ("sign in", "login", "log in", "authenticat")):
            notes.append("redirected to the sign-in page mid-task (session expired)")
        if not obs.ok and not obs.data.get("denied") and obs.error:
            notes.append(f"{action.tool} failed: {obs.error[:140]}")
        for n in notes:
            rec("anomaly", note=n, tool=action.tool)
            state.recoveries.append(f"Observed: {n}; adapted and continued.")

    def _resolve_approval(self, run_id: str, state: RunState, rec: Recorder, payload: dict, response: dict) -> None:
        action = Action.model_validate(payload["action"])
        if response.get("approved"):
            rec("approval.granted", tool=action.tool, preview=payload["preview"], note=response.get("note", ""))
            self._execute_action(run_id, state, rec, action, "approved")
            return
        note = str(response.get("note", "")).strip()
        rec("approval.denied", tool=action.tool, preview=payload["preview"], note=note)
        state.denied_tools.append(action.tool)
        if note:
            self.store.add_feedback(f"A human declined this action ({payload['preview']}): {note}", run_id)
            state.guidance.append(f"A human declined '{payload['preview']}': {note}")
        obs = Observation(ok=False, error=f"denied by human reviewer{': ' + note if note else ''}", summary="denied by human", data={"denied": True})
        self._record(run_id, state, rec, action, obs, "denied")

    def _resolve_question(self, state: RunState, payload: dict, answer: str) -> bool:
        """Apply a human's answer. Returns True when the human asked to abort the run."""
        purpose = payload["purpose"]
        if purpose == "missing_info":
            state.answers[payload["key"]] = answer
        elif purpose == "ask_human":
            action = Action(tool="ask_human", args={"question": payload["question"]})
            obs = Observation(ok=True, summary="human replied", data={"answer": answer})
            state.history.append(HistoryItem(action=action, observation=obs, policy="human"))
            state.guidance.append(f"Q: {payload['question']} A: {answer}")
        else:  # escalation
            state.guidance.append(f"After a failure ({payload['question'][:120]}...), a human said: {answer}")
            if "abort" in answer.lower():
                return True
            state.history = []  # fresh action budget for the step, with the human's guidance in context
        return False

    # ----------------------------------------------------------- verification
    def _verify(self, run_id: str, state: RunState, rec: Recorder) -> None:
        assert state.plan
        state.verify_rounds += 1
        rec("verify.started", round=state.verify_rounds, checks=len(state.plan.acceptance))
        results = self.verifier.verify(state.plan.acceptance, state)
        state.checks = results
        for r in results:
            rec("check.result", check=r.check_id, description=r.description, status=r.status, detail=r.detail, evidence=r.evidence)
        failed = [r for r in results if r.status == "failed"]
        if not state.plan.acceptance:
            return self._finish(run_id, state, rec, "completed_with_issues", "Steps finished but the plan defined no acceptance checks, so nothing was independently verified.")
        if not failed:
            skipped = [r for r in results if r.status == "skipped"]
            note = f" ({len(skipped)} check(s) skipped because a human declined the related action)" if skipped else ""
            return self._finish(run_id, state, rec, "completed", f"All {len(results) - len(skipped)} acceptance checks passed{note}.")
        if state.verify_rounds <= self.s.max_verify_rounds and state.replans < self.s.max_replans + self.s.max_verify_rounds:
            reason = "acceptance checks failed: " + "; ".join(f"{r.description} -> {r.detail}" for r in failed)
            state.recoveries.append(f"Verification found problems ({len(failed)} failed check(s)); planned remediation.")
            return self._replan(run_id, state, rec, reason)
        self._finish(run_id, state, rec, "completed_with_issues",
                     "Work was performed but these acceptance checks still fail: " + "; ".join(r.description for r in failed))

    # ----------------------------------------------------------------- finish
    def _finish(self, run_id: str, state: RunState, rec: Recorder, status: str, summary: str) -> None:
        state.status, state.phase, state.summary = status, "finished", summary  # type: ignore[assignment]
        if status == "failed":
            state.failure = summary
        lessons: list[str] = []
        try:
            lessons = self.brain.reflect(state, self._trail(run_id))
        except Exception as e:
            rec("reflect.error", error=str(e))
        self.store.add_episode(run_id, state.goal, status, lessons)
        for lesson in lessons:
            rec("lesson.saved", lesson=lesson)
        rec("run.finished", status=status, summary=summary)
        from .report import write_report

        self.store.save_run(run_id, state)
        write_report(self, run_id)

    def _trail(self, run_id: str) -> str:
        lines = []
        for e in self.store.events(run_id):
            d = e["data"]
            if e["type"] == "action.executed":
                lines.append(f"{d['tool']} -> {'ok' if d['ok'] else 'FAILED'} {d.get('summary') or ''} {d.get('error') or ''} {d.get('data') or ''}")
            elif e["type"] in ("anomaly", "step.failed", "approval.denied", "approval.granted", "intervention.requested", "intervention.resolved"):
                lines.append(f"{e['type']}: {d}")
        return "\n".join(lines)
