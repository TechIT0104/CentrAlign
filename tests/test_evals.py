from pathlib import Path

from centralign.brains.claude import ClaudeBrain
from centralign.evals import load_tasks, make_human, render_markdown, run_eval, summarize
from centralign.runtime import Runtime

from test_scenarios import LazyBrain

SPEC = load_tasks(Path(__file__).resolve().parent.parent / "demo_company" / "eval_tasks.yaml")


def only(task_id):
    return {**SPEC, "tasks": [t for t in SPEC["tasks"] if t["id"] == task_id]}


def test_eval_runs_a_task_and_scores_it_against_ground_truth(fresh):
    results = run_eval(Runtime(fresh), only("offboard-declined"))  # task declares its own conditions: "" and relabel
    assert [r.condition for r in results] == ["none", "relabel"]
    r = results[1]
    assert r.success and r.ground_truth == "1/1" and r.condition == "relabel" and r.approvals == 1
    s = summarize(results)
    assert s["success_rate"] == 1.0 and s["total_cost_usd"] is None  # offline brain costs nothing


def test_eval_does_not_trust_a_run_that_claims_success_without_doing_the_work(fresh):
    rt = Runtime(fresh, brain=LazyBrain(fresh.acmehr_url))
    task = {**only("onboard")["tasks"][0], "expect": ["completed", "completed_with_issues"]}
    [r] = run_eval(rt, {"tasks": [task]}, conditions=[""])
    assert r.status == "completed_with_issues"  # the status alone would have been acceptable...
    assert not r.success and r.failed_truth  # ...but the ground truth shows nothing happened


def test_tasks_needing_a_model_are_skipped_offline_not_faked(fresh):
    results = run_eval(Runtime(fresh), only("unseen-headcount-export"))
    assert results[0].status == "skipped" and summarize(results)["runs"] == 0


def test_cost_estimate_and_report_rendering():
    brain = ClaudeBrain("claude-opus-5-5", client=object())
    assert brain.cost({"input": 1_000_000, "output": 100_000}) == 6.0  # $4/M in + $20/M out * 0.1M
    assert ClaudeBrain("mystery-model", client=object()).cost({"input": 5}) is None
    md = render_markdown([], summarize([]), {"brain": "claude", "model": "m", "date": "d", "repeats": 1, "conditions": [""]})
    assert "Eval report" in md


def test_human_script_aborts_escalations_and_answers_in_order():
    calls = []

    class R:
        def resolve(self, iid, **kw):
            calls.append(kw)

    h = make_human({"answers": ["a", "b"], "deny": ["email.send"]})
    h(R(), "r", {"id": 1, "kind": "question", "payload": {"purpose": "ask_human"}})
    h(R(), "r", {"id": 2, "kind": "question", "payload": {"purpose": "escalation"}})
    h(R(), "r", {"id": 3, "kind": "approval", "payload": {"action": {"tool": "email.send"}}})
    assert calls == [{"answer": "a"}, {"answer": "abort"}, {"approved": False, "note": ""}]
