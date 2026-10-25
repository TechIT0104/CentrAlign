import time

from fastapi.testclient import TestClient

from centralign.runtime import Runtime
from centralign.web.server import create_app


def wait_for(client, run_id, pred, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/runs/{run_id}").json()
        if pred(s):
            return s
        time.sleep(0.4)
    raise AssertionError(f"timed out; last state: {s['status']} {s['summary']}")


def test_web_flow_approve_via_api(fresh):
    client = TestClient(create_app(Runtime(fresh)))
    assert "CentrAlign" in client.get("/").text
    assert client.get("/api/info").json()["brain"] == "scripted"
    rid = client.post("/api/runs", json={"goal": "Onboard Priya Sharma into Engineering, starting 2026-11-02"}).json()["run_id"]
    s = wait_for(client, rid, lambda s: s["status"] == "waiting_approval")
    [p] = s["pending"]
    assert p["kind"] == "approval" and "email" in p["payload"]["preview"]
    assert client.post(f"/api/interventions/{p['id']}", json={"note": ""}).status_code == 400  # decision required
    assert client.post(f"/api/interventions/{p['id']}", json={"approved": True}).status_code == 200
    s = wait_for(client, rid, lambda s: s["status"] in ("completed", "failed", "completed_with_issues"))
    assert s["status"] == "completed" and all(c["status"] == "passed" for c in s["checks"])
    assert client.post(f"/api/interventions/{p['id']}", json={"approved": True}).status_code == 409  # already resolved
    events = client.get(f"/api/runs/{rid}/events").json()
    assert events[0]["type"] == "run.created" and events[-1]["type"] == "run.finished"
    shot = next(a for e in events if e["type"] == "action.executed" for a in e["data"]["artifacts"])
    assert client.get(f"/api/runs/{rid}/evidence/{shot}").headers["content-type"] == "image/png"
    assert client.get(f"/api/runs/{rid}/evidence/../../centralign.db").status_code == 404
    assert client.get(f"/api/runs/{rid}/report").json()["audit"]["intact"]


def test_web_question_flow_and_feedback(fresh):
    client = TestClient(create_app(Runtime(fresh)))
    rid = client.post("/api/runs", json={"goal": "Onboard Dana Lee"}).json()["run_id"]
    s = wait_for(client, rid, lambda s: s["status"] == "waiting_human")
    assert "department" in s["pending"][0]["payload"]["question"].lower()
    client.post(f"/api/interventions/{s['pending'][0]['id']}", json={"answer": "Design"})
    s = wait_for(client, rid, lambda s: s["status"] == "waiting_human" and "start date" in s["pending"][0]["payload"]["question"].lower())
    client.post(f"/api/interventions/{s['pending'][0]['id']}", json={"answer": "2026-12-01"})
    wait_for(client, rid, lambda s: s["status"] == "waiting_approval")
    client.post("/api/feedback", json={"text": "Always cc the manager"})
    assert "Always cc the manager" in client.get("/api/info").json()["feedback"]
    client.post(f"/api/runs/{rid}/cancel")
    assert client.get(f"/api/runs/{rid}").json()["status"] == "cancelled"
