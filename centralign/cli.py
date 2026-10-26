"""Command line interface: `python -m centralign ...`"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from .config import Settings
from .demo import chaos_from_names, prepare_demo, reset_acmehr, reset_files
import time

from .runtime import Runtime, parse_every

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "c": "\033[36m", "0": "\033[0m"}


def paint(color: str, text: str) -> str:
    return f"{C[color]}{text}{C['0']}" if sys.stdout.isatty() else text


def format_event(t: str, d: dict[str, Any]) -> str | None:
    """One human-readable line per interesting audit event (the live trace)."""
    if t == "run.created":
        return paint("b", f"> Goal: {d['goal']}") + paint("dim", f"   [brain: {d['brain']}]")
    if t == "memory.recalled":
        return paint("dim", f"  recalled company knowledge: {', '.join(d['docs']) or 'none'}" + (f"; {len(d['rules'])} learned rule(s)" if d["rules"] else ""))
    if t == "plan.created" or t == "plan.revised":
        p = d["plan"]
        lines = [paint("c", ("  PLAN" if t == "plan.created" else "  REVISED PLAN") + f": {p['understanding']}")]
        lines += [f"    {i}. {s['description']}" for i, s in enumerate(p["steps"], 1)]
        lines.append(paint("dim", f"    acceptance: {len(p['acceptance'])} independent check(s)"))
        return "\n".join(lines)
    if t == "plan.replanning":
        return paint("y", f"  ! replanning ({d['reason'][:140]})")
    if t == "step.started":
        return paint("b", f"\n  [{d['index']}/{d['of']}] {d['description']}")
    if t == "action.proposed":
        return None
    if t == "policy.decision":
        if d["verdict"] == "auto":
            return None
        color = "y" if d["verdict"] == "approve" else "r"
        return paint(color, f"      policy: {d['verdict'].upper()}  ({d['rule']}) {d['preview'] or ''}")
    if t == "action.executed":
        mark = paint("g", "ok") if d["ok"] else paint("r", "FAIL")
        retry = f" after {d['retries']} retr{'y' if d['retries'] == 1 else 'ies'}" if d.get("retries") else ""
        args = {k: v for k, v in d["args"].items() if k != "content" and k != "body"}
        txt = str(args)
        return f"      {d['tool']:<18} {txt[:70]:<70} {mark}{retry} {paint('dim', (d.get('error') or d.get('summary') or '')[:60])}"
    if t == "anomaly":
        return paint("y", f"      ! {d['note']}")
    if t == "step.completed":
        return paint("g", f"      done: {d['summary']}") + (paint("y", " (skipped)") if d.get("skipped") else "")
    if t == "step.failed":
        return paint("r", f"      step failed: {d['reason']}")
    if t == "intervention.requested":
        return paint("y", f"  ? waiting for a human: {d.get('question') or d.get('preview')}")
    if t == "approval.granted":
        return paint("g", f"      approved by human: {d['preview']}")
    if t == "approval.denied":
        return paint("r", f"      DENIED by human: {d['preview']} {d.get('note', '')}")
    if t == "verify.started":
        return paint("b", f"\n  VERIFY (round {d['round']}): {d['checks']} acceptance check(s), independent of the agent")
    if t == "check.result":
        col = {"passed": "g", "failed": "r", "skipped": "y"}[d["status"]]
        return f"    {paint(col, d['status'].upper().ljust(8))} {d['description']}  {paint('dim', d['detail'][:90])}"
    if t == "lesson.saved":
        return paint("dim", f"  memory: learned -> {d['lesson']}")
    if t == "run.error":
        return paint("r", f"  internal error: {d['error']}")
    return None


def prompt_human(rt: Runtime, run_id: str, iv: dict[str, Any], auto_yes: bool = False) -> None:
    p = iv["payload"]
    if iv["kind"] == "approval":
        print(paint("y", f"\n  APPROVAL NEEDED ({p['rule']}): {p['preview']}\n  Why: {p['reason']}\n  Step: {p['step']}"))
        if auto_yes:
            print("  --yes given: approving")
            rt.resolve(iv["id"], approved=True, note="auto-approved by --yes")
            return
        ans = input("  Approve? [y/N, or 'n <reason>' to decline with feedback]: ").strip()
        if ans.lower() in ("y", "yes"):
            rt.resolve(iv["id"], approved=True, note="")
        else:
            rt.resolve(iv["id"], approved=False, note=ans[1:].strip() if ans.lower().startswith("n ") else "")
    else:
        print(paint("y", f"\n  QUESTION: {p['question']}"))
        rt.resolve(iv["id"], answer=input("  Your answer: ").strip())


def build_runtime(args: argparse.Namespace) -> Runtime:
    over: dict[str, Any] = {}
    if getattr(args, "brain", None):
        over["brain"] = args.brain
    if getattr(args, "headed", False):
        over["headless"] = False
    return Runtime(Settings.load(**over))


def cmd_run(args: argparse.Namespace) -> int:
    rt = build_runtime(args)
    proc = prepare_demo(rt.s, chaos_from_names(args.chaos or ""), fresh=not args.keep)
    rt.listeners.append(lambda t, d: (line := format_event(t, d)) and print(line, flush=True))
    try:
        state = rt.run(args.goal, on_wait=lambda r, rid, iv: prompt_human(r, rid, iv, args.yes))
    finally:
        if proc:
            proc.terminate()
    run_id = rt.store.list_runs(1)[0]["id"]
    print(paint("b", f"\nOUTCOME: {state.status.upper()}") + f" - {state.summary}")
    print(f"Report:   {rt.run_dir(run_id) / 'report.md'}\nRun id:   {run_id}")
    return 0 if state.status == "completed" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.server import create_app

    rt = build_runtime(args)
    proc = prepare_demo(rt.s, chaos_from_names(args.chaos or ""), fresh=True)
    try:
        uvicorn.run(create_app(rt), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if proc:
            proc.terminate()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    s = Settings.load()
    prepare_demo(s, chaos_from_names(args.chaos or ""), fresh=True)
    print("demo data reset")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(brain="scripted"))
    for r in rt.store.list_runs(20):
        print(f"{r['id']}  {r['status']:<22} {r['goal'][:80]}")
    pend = rt.store.pending_interventions()
    for iv in pend:
        print(paint("y", f"pending #{iv['id']} (run {iv['run_id']}): {iv['payload'].get('preview') or iv['payload'].get('question')}"))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(**({"brain": args.brain} if args.brain else {})))
    iv = rt.store.get_intervention(args.id)
    if args.action in ("approve", "deny"):
        rt.resolve(args.id, approved=args.action == "approve", note=args.text or "")
    else:
        rt.resolve(args.id, answer=args.text or "")
    state = rt.advance(iv["run_id"])
    print(f"run {iv['run_id']}: {state.status} {state.summary}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run tasks x failure conditions x repeats and report success rate, effort, human load and cost."""
    from datetime import date
    from pathlib import Path

    from .evals import load_tasks, render_markdown, run_eval, save, summarize

    rt = build_runtime(args)
    proc = prepare_demo(rt.s, fresh=True)
    spec = load_tasks(Path(args.tasks))
    conditions = [c.strip() for c in args.conditions.split(",")] if args.conditions is not None else None

    def progress(r) -> None:
        tag = paint("g", "PASS") if r.success else paint("y", "skip") if r.status == "skipped" else paint("r", "FAIL")
        print(f"  {tag}  {r.task:<26} {r.condition:<8} -> {r.status:<22} actions={r.actions} human={r.interventions} replans={r.replans}"
              + (f" truth={r.ground_truth}" if r.ground_truth != "-" else "") + (f"  {r.note}" if r.note else ""), flush=True)

    try:
        results = run_eval(rt, spec, conditions, args.repeat, args.only, progress)
    finally:
        if proc:
            proc.terminate()
    meta = {"brain": rt.brain.name, "model": getattr(rt.brain, "model", None), "date": str(date.today()), "repeats": args.repeat,
            "conditions": conditions if conditions is not None else spec.get("conditions")}
    summary = summarize(results)
    path = save(rt, results, summary, meta)
    print("\n" + render_markdown(results, summary, meta))
    print(f"saved {path}")
    return 0 if summary["successes"] == summary["runs"] else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(brain="scripted"))
    if args.sub == "add":
        sid = rt.store.add_schedule(args.goal, parse_every(args.every))
        print(f"schedule #{sid}: '{args.goal}' every {args.every} (first run in {args.every})")
    elif args.sub == "rm":
        rt.store.remove_schedule(args.id)
        print(f"removed schedule #{args.id}")
    else:
        for s in rt.store.schedules():
            due = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["next_run"]))
            print(f"#{s['id']}  every {s['every_seconds']}s  next {due}  last run {s['last_run_id'] or '-'}  {s['goal'][:70]}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Background worker: starts scheduled runs and resumes runs whose worker died."""
    rt = build_runtime(args)
    proc = prepare_demo(rt.s, fresh=False)
    rt.listeners.append(lambda t, d: (line := format_event(t, d)) and print(line, flush=True))
    print(f"worker {rt.owner} started (poll {args.poll}s). Ctrl-C to stop.")
    try:
        while True:
            res = rt.worker_tick(grace=args.grace)
            if res["scheduled"] or res["recovered"]:
                print(paint("c", f"scheduled {res['scheduled']}  recovered {res['recovered']}"))
            if args.once:
                rt._pool.shutdown(wait=True)
                return 0
            time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0
    finally:
        if proc:
            proc.terminate()


def cmd_feedback(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(brain="scripted"))
    rt.store.add_feedback(args.text)
    print("saved. It will be included in the context of future runs.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(brain="scripted"))
    from .report import build_report, render_markdown

    print(render_markdown(build_report(rt, args.run_id)))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    rt = Runtime(Settings.load(brain="scripted"))
    ok, detail = rt.store.verify_audit_chain(args.run_id)
    print(detail)
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="centralign", description="Autonomous company operator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a task against the demo company")
    p.add_argument("goal")
    p.add_argument("--chaos", help="inject failures: flaky,rename,expiry,slow or all")
    p.add_argument("--yes", action="store_true", help="auto-approve approval requests (demo only)")
    p.add_argument("--keep", action="store_true", help="do not reset demo data first")
    p.add_argument("--brain", choices=["claude", "scripted"])
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("serve", help="start the web UI")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--chaos")
    p.add_argument("--brain", choices=["claude", "scripted"])
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("reset", help="reset demo data")
    p.add_argument("--chaos")
    p.set_defaults(fn=cmd_reset)

    sub.add_parser("runs", help="list runs and pending approvals").set_defaults(fn=cmd_runs)

    for name in ("approve", "deny", "answer"):
        p = sub.add_parser(name, help=f"{name} a pending intervention and resume its run")
        p.add_argument("id", type=int)
        p.add_argument("text", nargs="?", default="")
        p.add_argument("--brain", choices=["claude", "scripted"])
        p.set_defaults(fn=cmd_resolve, action=name)

    p = sub.add_parser("eval", help="measure reliability: tasks x failure conditions x repeats")
    p.add_argument("--tasks", default="demo_company/eval_tasks.yaml")
    p.add_argument("--conditions", help="override failure conditions, comma separated; '' = none (e.g. ',flaky,all')")
    p.add_argument("--repeat", type=int, default=1, help="runs per task/condition (models are non-deterministic)")
    p.add_argument("--only", help="run tasks whose id contains this text")
    p.add_argument("--brain", choices=["claude", "scripted"])
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("schedule", help="manage recurring tasks")
    ss = p.add_subparsers(dest="sub", required=True)
    q = ss.add_parser("add", help="run GOAL every INTERVAL")
    q.add_argument("goal")
    q.add_argument("--every", required=True, help="e.g. 30m, 6h, 1d, 1w")
    ss.add_parser("list")
    q = ss.add_parser("rm")
    q.add_argument("id", type=int)
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("worker", help="background worker: scheduled tasks + crash recovery")
    p.add_argument("--once", action="store_true", help="do one pass and exit")
    p.add_argument("--poll", type=float, default=10)
    p.add_argument("--grace", type=float, default=30, help="seconds a running run may be idle before it counts as orphaned")
    p.add_argument("--brain", choices=["claude", "scripted"])
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("feedback", help="teach the operator a standing rule")
    p.add_argument("text")
    p.set_defaults(fn=cmd_feedback)

    p = sub.add_parser("report", help="print a run's evidence report")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("audit", help="verify the tamper-evident audit chain of a run")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_audit)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
