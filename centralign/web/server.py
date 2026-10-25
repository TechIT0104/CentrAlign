"""Web UI backend: submit tasks, watch the live trace, approve/decline actions, answer questions."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from ..demo import chaos_from_names, reset_acmehr, reset_files
from ..report import build_report
from ..runtime import Runtime

STATIC = Path(__file__).parent


class NewRun(BaseModel):
    goal: str


class Resolution(BaseModel):
    approved: bool | None = None
    note: str = ""
    answer: str = ""


class Feedback(BaseModel):
    text: str


class Demo(BaseModel):
    chaos: str = ""


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="CentrAlign operator")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/info")
    def info():
        return {"brain": rt.brain.name, "model": getattr(rt.brain, "model", None), "tools": rt.registry.names(),
                "feedback": [f["text"] for f in rt.store.feedback()]}

    @app.get("/api/runs")
    def runs():
        return rt.store.list_runs(30)

    @app.post("/api/runs")
    def new_run(body: NewRun):
        if not body.goal.strip():
            raise HTTPException(400, "empty goal")
        return {"run_id": rt.submit_background(body.goal.strip())}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        try:
            state = rt.state(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run")
        return {
            "id": run_id, "goal": state.goal, "status": state.status, "phase": state.phase, "summary": state.summary,
            "plan": state.plan.model_dump() if state.plan else None, "step_idx": state.step_idx,
            "checks": [c.model_dump() for c in state.checks], "pending": rt.store.pending_interventions(run_id),
        }

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0):
        return rt.store.events(run_id, after)

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str):
        return build_report(rt, run_id)

    @app.get("/api/runs/{run_id}/evidence/{path:path}")
    def evidence(run_id: str, path: str):
        base = (rt.run_dir(run_id)).resolve()
        f = (base / path).resolve()
        if base not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        rt.cancel(run_id)
        return {"ok": True}

    @app.post("/api/interventions/{iid}")
    def resolve(iid: int, body: Resolution):
        try:
            iv = rt.store.get_intervention(iid)
            if iv["kind"] == "approval":
                if body.approved is None:
                    raise HTTPException(400, "approved is required")
                rt.resolve(iid, approved=body.approved, note=body.note)
            else:
                if not body.answer.strip():
                    raise HTTPException(400, "answer is required")
                rt.resolve(iid, answer=body.answer.strip())
        except KeyError:
            raise HTTPException(404, "unknown intervention")
        except ValueError as e:
            raise HTTPException(409, str(e))
        rt.advance_background(iv["run_id"])
        return {"ok": True}

    @app.post("/api/feedback")
    def feedback(body: Feedback):
        rt.store.add_feedback(body.text.strip())
        return {"ok": True}

    @app.post("/api/demo/reset")
    def demo_reset(body: Demo):
        reset_files(rt.s)
        reset_acmehr(rt.s, chaos_from_names(body.chaos))
        return {"ok": True}

    return app
