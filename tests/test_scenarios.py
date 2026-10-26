"""End-to-end scenarios against the demo company with a real browser (scripted brain)."""
import csv
import json

import httpx
import pytest

from centralign.brains.scripted import ScriptedBrain
from centralign.demo import chaos_from_names
from centralign.models import Action, Decision
from centralign.runtime import Runtime

from conftest import human


def hr(settings, email="", q=""):
    r = httpx.get(f"{settings.acmehr_url}/api/employees", params={"email": email, "q": q}, headers={"X-API-Key": settings.acmehr_api_key})
    return r.json()["items"]


def roster(settings):
    with open(settings.files_dir / "data/team_roster.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def events(rt, run_id, type_):
    return [e["data"] for e in rt.store.events(run_id) if e["type"] == type_]


def run(rt, goal, **kw):
    state = rt.run(goal, on_wait=human(**kw))
    return state, rt.store.list_runs(1)[0]["id"]


def test_onboarding_happy_path(rt, fresh):
    state, rid = run(rt, "Onboard Priya Sharma into Engineering, starting 2026-11-02")
    assert state.status == "completed", state.summary
    [emp] = hr(fresh, "priya.sharma@acme.test")
    assert (emp["department"], emp["start_date"], emp["access_level"], emp["status"]) == ("Engineering", "2026-11-02", "standard", "active")
    assert len([r for r in roster(fresh) if r["email"] == "priya.sharma@acme.test"]) == 1
    assert len(list((fresh.files_dir / "outbox").glob("*priya*"))) == 1
    # the only human gate was the outbound email
    assert [e["tool"] for e in events(rt, rid, "approval.granted")] == ["email.send"]
    assert rt.store.verify_audit_chain(rid)[0]
    report = (rt.run_dir(rid) / "report.md").read_text(encoding="utf-8")
    assert "COMPLETED" in report and "evidence/" in report
    assert list((rt.run_dir(rid) / "evidence").glob("*.png"))


def test_secret_never_reaches_audit_log_or_report(rt, fresh):
    state, rid = run(rt, "Onboard Priya Sharma into Engineering, starting 2026-11-02")
    blob = json.dumps(rt.store.events(rid)) + (rt.run_dir(rid) / "report.md").read_text(encoding="utf-8")
    assert "ops-pass-123" not in blob and "{{secret:ACMEHR_PASSWORD}}" in blob


def test_recovers_from_500_session_expiry_and_renamed_button(rt, fresh):
    import httpx as h

    h.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("all"))
    state, rid = run(rt, "Onboard Sam Lee into Design, starting 2026-11-09")
    assert state.status == "completed", state.summary
    assert len(hr(fresh, "sam.lee@acme.test")) == 1  # exactly one record despite the failed save and retries
    notes = " ".join(events(rt, rid, "anomaly") and [a["note"] for a in events(rt, rid, "anomaly")])
    assert "HTTP 500" in notes and "session expired" in notes
    assert any("Add to directory" in json.dumps(e["data"]) for e in rt.store.events(rid))  # found the renamed button by purpose
    assert any("transient 500" in x for x in [e["lesson"] for e in events(rt, rid, "lesson.saved")])


def test_elevated_access_requires_approval_and_denial_is_respected(rt, fresh):
    state, rid = run(rt, "Onboard Dana Kim into Engineering with admin access, starting 2026-11-02", deny_tools={"browser.select"})
    # the human declined the privilege grant, so the record must NOT have admin access
    emps = hr(fresh, "dana.kim@acme.test")
    assert not emps or emps[0]["access_level"] != "admin"
    assert events(rt, rid, "approval.denied")
    # and the verifier flags that the requested outcome was not met
    assert state.status == "completed"  # the human's decision stands; the privilege check is skipped, not faked
    assert {c.check_id: c.status for c in state.checks}["c1b"] == "skipped"


def test_declined_email_skips_dependent_check_and_is_learned(rt, fresh):
    state, rid = run(rt, "Onboard Priya Sharma into Engineering, starting 2026-11-02", deny_tools={"email.send"})
    assert state.status == "completed", state.summary
    assert not list((fresh.files_dir / "outbox").glob("*priya*"))
    assert {c.check_id: c.status for c in state.checks}["c5"] == "skipped"
    assert any("not today" in f["text"] for f in rt.store.feedback())  # human feedback became a standing rule


def test_duplicate_asks_human_and_stop_creates_nothing(rt, fresh):
    before = len(hr(fresh, q="rahul"))
    state, rid = run(rt, "Onboard Rahul Verma into Sales, starting 2026-11-02", answers=["stop"])
    assert state.status == "failed" and state.replans == 0  # a human's "stop" is final: not replanned, not re-asked
    assert len(events(rt, rid, "intervention.requested")) == 1
    assert len(hr(fresh, q="rahul")) == before == 1
    q = events(rt, rid, "intervention.requested")
    assert any("already has a record" in (d.get("question") or "") for d in q)


def test_verifier_flags_mismatch_when_human_reuses_stale_record(rt, fresh):
    state, rid = run(rt, "Onboard Rahul Verma into Sales, starting 2026-11-02", answers=["reuse"])
    assert state.status == "completed_with_issues"  # the old record is inactive, so "done" would have been a lie
    failed = [c for c in state.checks if c.status == "failed"]
    assert failed and "status" in failed[0].detail


def test_missing_information_is_requested_not_guessed(rt, fresh):
    state, rid = run(rt, "Onboard Dana Lee", answers=["Design", "2026-12-01"])
    assert state.status == "completed", state.summary
    qs = [d["question"] for d in events(rt, rid, "intervention.requested") if d["kind"] == "question"]
    assert any("department" in q.lower() for q in qs) and any("start date" in q.lower() for q in qs)
    assert hr(fresh, "dana.lee@acme.test")[0]["department"] == "Design"


def test_offboarding_needs_approval_then_verifies(rt, fresh):
    state, rid = run(rt, "Offboard Sam Okafor")
    assert state.status == "completed", state.summary
    assert hr(fresh, "sam.okafor@acme.test")[0]["status"] == "inactive"
    assert [r["status"] for r in roster(fresh) if r["email"] == "sam.okafor@acme.test"] == ["inactive"]
    asked = events(rt, rid, "intervention.requested")
    assert [a["rule"] for a in asked if a["kind"] == "approval"] == ["request-deactivate"]  # exactly one approval, on the real effect


def test_offboarding_denied_leaves_account_untouched(rt, fresh):
    state, rid = run(rt, "Offboard Sam Okafor", approve=False)
    assert hr(fresh, "sam.okafor@acme.test")[0]["status"] == "active"
    assert state.status == "completed"  # nothing was supposed to change; checks skipped, not faked
    assert {c.status for c in state.checks} == {"skipped"}


def test_run_pauses_and_resumes_in_a_new_process(rt, fresh):
    run_id = rt.submit("Onboard Priya Sharma into Engineering, starting 2026-11-02")
    state = rt.advance(run_id)
    assert state.status == "waiting_approval" and state.pending_action.tool == "email.send"
    rt.browsers.close(run_id)
    # a brand-new runtime (as after a crash/restart) picks the run up from SQLite
    rt2 = Runtime(fresh)
    [pending] = rt2.store.pending_interventions(run_id)
    rt2.resolve(pending["id"], approved=True, note="")
    state = rt2.advance(run_id)
    assert state.status == "completed", state.summary
    assert len(list((fresh.files_dir / "outbox").glob("*priya*"))) == 1


def test_unsupported_request_fails_fast_with_clear_message(rt):
    state, _ = run(rt, "Reconcile the vendor invoices against the ledger")
    assert state.status == "failed" and "ANTHROPIC_API_KEY" in state.summary


class LazyBrain(ScriptedBrain):
    """A brain that claims every step is done without doing anything: the failure mode verification exists for."""

    def next_decision(self, ctx):
        return Decision(kind="done", summary="all done, trust me")


def test_verifier_catches_false_claims_of_success(fresh):
    rt = Runtime(fresh, brain=LazyBrain(fresh.acmehr_url))
    state = rt.run("Onboard Priya Sharma into Engineering, starting 2026-11-02")
    assert state.status == "completed_with_issues"
    assert not hr(fresh, "priya.sharma@acme.test")
    assert [c.status for c in state.checks if c.check_id == "c1"] == ["failed"]


class LoopingBrain(ScriptedBrain):
    def next_decision(self, ctx):
        return Decision(kind="act", action=Action(tool="fs.read", args={"path": "does/not/exist.txt"}))


def test_loop_guard_and_escalation_to_human(fresh):
    rt = Runtime(fresh, brain=LoopingBrain(fresh.acmehr_url))
    answers = []

    def on_wait(r, rid, iv):
        answers.append(iv["payload"]["question"])
        r.resolve(iv["id"], answer="abort")

    state = rt.run("Onboard Priya Sharma into Engineering, starting 2026-11-02", on_wait=on_wait)
    assert state.status == "failed" and state.replans == fresh.max_replans
    assert answers and "stuck" in answers[0]  # escalated after replans were exhausted


class WeakeningBrain(LazyBrain):
    """Does no work and, when asked to replan, 'forgets' every acceptance check to try to pass."""

    def plan(self, req):
        plan = super().plan(req)
        if req.prior_plan:
            plan.acceptance = []
        return plan


def test_replan_cannot_weaken_the_definition_of_done(fresh):
    rt = Runtime(fresh, brain=WeakeningBrain(fresh.acmehr_url))
    state = rt.run("Onboard Priya Sharma into Engineering, starting 2026-11-02")
    assert state.status == "completed_with_issues" and state.replans >= 1
    assert len(state.frozen_checks) == 6 and {c.check_id for c in state.checks} >= {"c1", "c1b", "c2", "c3", "c4", "c5"}


def test_independent_checks_are_merged_and_deduplicated(fresh):
    from centralign.models import Check, merge_checks

    a = Check(id="c1", description="x", type="file_exists", params={"path": "a"})
    dup = Check(id="zz", description="same thing reworded", type="file_exists", params={"path": "a"})
    clash = Check(id="c1", description="other", type="file_exists", params={"path": "b"})
    out = merge_checks([a], [dup, clash], prefix="r1")
    assert [c.id for c in out] == ["c1", "r1-c1"] and out[1].params == {"path": "b"}


def test_approval_follows_the_effect_not_the_button_label(rt, fresh):
    """Relabel 'Deactivate employee' to 'Disable account': the click-level rule no longer matches,
    but the request the click sends still requires approval, and declining leaves the account intact."""
    httpx.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("relabel"))
    state, rid = run(rt, "Offboard Sam Okafor", approve=False)
    reqs = events(rt, rid, "intervention.requested")
    assert any(r.get("rule") == "request-deactivate" for r in reqs), reqs
    assert len([r for r in reqs if r["kind"] == "approval"]) == 1  # one prompt, however the button is labelled
    assert events(rt, rid, "action.blocked")[0]["request"].endswith("/deactivate")
    assert hr(fresh, "sam.okafor@acme.test")[0]["status"] == "active"


def test_approved_request_is_released_exactly_once(rt, fresh):
    httpx.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("relabel"))
    state, rid = run(rt, "Offboard Sam Okafor")
    assert state.status == "completed", state.summary
    assert hr(fresh, "sam.okafor@acme.test")[0]["status"] == "inactive"
    granted = [e for e in events(rt, rid, "approval.granted")]
    assert len(granted) == 1 and "sends POST" in granted[0]["preview"]


def test_unseen_failures_stop_safely_instead_of_faking_success(rt, fresh):
    """The offline brain has no recovery for a newly required form field. It must not claim success."""
    httpx.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("newfield"))
    answers = []

    def on_wait(r, rid, iv):
        answers.append(iv["payload"]["purpose"])
        r.resolve(iv["id"], answer="abort")

    state = rt.run("Onboard Priya Sharma into Engineering, starting 2026-11-02", on_wait=on_wait)
    assert state.status == "failed" and answers == ["escalation"]
    assert not hr(fresh, "priya.sharma@acme.test")


def test_form_outage_is_recovered_by_replanning(rt, fresh):
    httpx.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("outage"))
    state, rid = run(rt, "Onboard Priya Sharma into Engineering, starting 2026-11-02")
    assert state.status == "completed" and state.replans >= 1
    assert len(hr(fresh, "priya.sharma@acme.test")) == 1


def test_offboarding_survives_session_expiry_after_the_save_without_double_acting(rt, fresh):
    """The session dies on the redirect AFTER the deactivation went through. The operator must check the
    record instead of assuming failure, and must not ask for (or perform) a second deactivation."""
    httpx.post(f"{fresh.acmehr_url}/__admin/chaos", json=chaos_from_names("expiry"))
    state, rid = run(rt, "Offboard Sam Okafor")
    assert state.status == "completed", state.summary
    assert hr(fresh, "sam.okafor@acme.test")[0]["status"] == "inactive"
    assert len(events(rt, rid, "approval.granted")) == 1 and state.replans == 0


def test_first_name_only_asks_for_the_full_name_and_then_uses_the_answer(rt, fresh):
    state, rid = run(rt, "Onboard Priya into Engineering, starting 2026-11-02", answers=["Priya Sharma"])
    assert state.status == "completed", state.summary
    qs = [d["question"] for d in events(rt, rid, "intervention.requested") if d["kind"] == "question"]
    assert qs == ["What is Priya's last name?"]
    assert hr(fresh, "priya.sharma@acme.test")[0]["department"] == "Engineering"


def test_an_unusable_name_answer_gets_one_clearer_follow_up_then_a_clear_failure(rt, fresh):
    state, rid = run(rt, "Onboard Priya into Engineering, starting 2026-11-02", answers=["", "Priya Sharma"])
    assert state.status == "completed"
    qs = [d["question"] for d in events(rt, rid, "intervention.requested") if d["kind"] == "question"]
    assert len(qs) == 2 and "first and last name" in qs[1]
    state, rid = run(rt, "Onboard Priya into Engineering, starting 2026-11-02", answers=["Priya", "Priya"])
    assert state.status == "failed" and "could not use the answer" in state.summary
    assert len(hr(fresh, q="priya")) == 1  # only the first run's Priya Sharma; the failed one created nothing


def test_a_lone_surname_is_combined_with_the_first_name_from_the_request(rt, fresh):
    state, rid = run(rt, "Onboard Priya into Engineering, starting 2026-11-02", answers=["kk"])
    assert state.status == "completed", state.summary
    assert hr(fresh, "priya.kk@acme.test")[0]["full_name"] == "Priya Kk"
