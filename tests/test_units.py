"""Fast tests for the deterministic building blocks (no browser)."""
import sqlite3

import pytest

from centralign.config import Settings
from centralign.memory import Knowledge, Memory
from centralign.models import Action, Check, RunState
from centralign.policy import Policy
from centralign.store import Store
from centralign.tools.base import safe_path
from centralign.tools.files import CsvUpsert, FsWrite
from centralign.tools import ToolContext, build_registry
from centralign.verifier import Verifier


@pytest.fixture
def policy():
    return Policy.load(Settings.load().policy_path)


def test_policy_email_needs_approval(policy):
    v = policy.decide(Action(tool="email.send", args={"to": "a@b.c"}), "external")
    assert v.verdict == "approve" and v.rule == "outbound-email"


def test_policy_elevated_access_only_for_access_fields(policy):
    dept = policy.decide(Action(tool="browser.select", args={"value": "Finance"}), "write", {"text": "select | Department | department <- Finance"})
    acc = policy.decide(Action(tool="browser.select", args={"value": "admin"}), "write", {"text": "select | Access level | access_level <- admin"})
    assert dept.verdict == "auto" and acc.verdict == "approve"


def test_policy_blocks_external_hosts_and_unknown_risk_fails_safe(policy):
    assert policy.decide(Action(tool="browser.goto", args={"url": "https://evil.example/x"}), "write").verdict == "deny"
    assert policy.decide(Action(tool="browser.goto", args={"url": "http://127.0.0.1:8765/"}), "write").verdict == "auto"
    assert policy.decide(Action(tool="x.y"), "mystery").verdict == "approve"
    assert policy.decide(Action(tool="http.request", args={"method": "POST", "url": "http://127.0.0.1/x"}), "write").verdict == "approve"
    assert policy.decide(Action(tool="http.request", args={"method": "GET", "url": "http://127.0.0.1/x"}), "read").verdict == "auto"


def test_policy_rejects_bad_rule():
    with pytest.raises(ValueError):
        Policy({"rules": [{"id": "x", "action": "maybe"}]})


def test_memory_retrieves_relevant_sop():
    k = Knowledge(Settings.load().knowledge_dir)
    top = k.search("how do I onboard a new hire and what email format", k=2)
    assert top and top[0].source == "sop_onboarding.md"
    assert k.search("deactivate employee revoke access", k=1)[0].source == "sop_offboarding.md"
    assert k.search("zzzz qqqq", k=3) == []


def test_feedback_and_episodes_flow_into_context(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_feedback("Never email before the manager approves")
    store.add_episode("r1", "Onboard Sam Lee into Design", "completed", ["AcmeHR sessions expire"])
    ctx = Memory(Settings.load().knowledge_dir, store).recall("Onboard Ann Bee into Design")
    assert ctx["rules"] == ["Never email before the manager approves"]
    assert ctx["episodes"] and "sessions expire" in Memory.render(ctx)


def test_audit_log_is_append_only_and_tamper_evident(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rid = store.create_run("goal")
    for i in range(3):
        store.add_event(rid, "x", {"i": i})
    assert store.verify_audit_chain(rid)[0]
    with pytest.raises(sqlite3.DatabaseError):
        store._db.execute("UPDATE events SET data_json='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        store._db.execute("DELETE FROM events WHERE seq=1")
    # an attacker who drops the trigger still breaks the hash chain
    store._db.execute("DROP TRIGGER events_no_update")
    store._db.execute("UPDATE events SET data_json='{\"i\": 99}' WHERE seq=2")
    ok, detail = store.verify_audit_chain(rid)
    assert not ok and "#2" in detail


def test_sandbox_blocks_path_escape(tmp_path):
    with pytest.raises(PermissionError):
        safe_path(tmp_path, "../secret.txt")
    s = Settings.load(home=tmp_path)
    ctx = ToolContext(s, "r", tmp_path / "run")
    obs, _ = build_registry().execute(Action(tool="fs.write", args={"path": "../../evil.txt", "content": "x"}), ctx)
    assert not obs.ok and "sandbox" in obs.error


def test_csv_upsert_updates_in_place_and_rejects_unknown_columns(fresh):
    ctx = ToolContext(fresh, "r", fresh.runs_dir / "r")
    reg = build_registry()
    row = {"name": "A B", "email": "a.b@acme.test", "department": "Sales", "start_date": "2026-01-01", "status": "active"}
    assert reg.execute(Action(tool="csv.upsert_row", args={"path": "data/team_roster.csv", "key": "email", "row": row}), ctx)[0].ok
    assert reg.execute(Action(tool="csv.upsert_row", args={"path": "data/team_roster.csv", "key": "email", "row": {**row, "status": "inactive"}}), ctx)[0].ok
    rows = reg.execute(Action(tool="csv.read", args={"path": "data/team_roster.csv", "where": {"email": "a.b@acme.test"}}), ctx)[0].data["rows"]
    assert len(rows) == 1 and rows[0]["status"] == "inactive"
    bad = reg.execute(Action(tool="csv.upsert_row", args={"path": "data/team_roster.csv", "key": "email", "row": {"email": "x@y", "salary": "1"}}), ctx)[0]
    assert not bad.ok


def test_secrets_are_injected_late_and_redacted(fresh, monkeypatch):
    from centralign.tools.base import Registry, Tool, redact, resolve_secrets
    from centralign.models import Observation

    monkeypatch.setenv("SECRET_DEMO_TOKEN", "hunter2-token")
    assert resolve_secrets({"v": "{{secret:DEMO_TOKEN}}"}) == {"v": "hunter2-token"}

    class Echo(Tool):
        name, risk = "echo", "read"

        def run(self, args, ctx):
            return Observation(ok=True, summary=f"got {args['v']}")

    reg = Registry()
    reg.register(Echo())
    obs, _ = reg.execute(Action(tool="echo", args={"v": "{{secret:DEMO_TOKEN}}"}), ToolContext(fresh, "r", fresh.runs_dir / "r"))
    assert "hunter2-token" not in obs.summary and "[REDACTED]" in obs.summary
    assert "hunter2-token" not in redact("x hunter2-token y")


def test_transient_failures_are_retried(fresh):
    from centralign.tools.base import Registry, Tool
    from centralign.models import Observation

    calls = []

    class Flaky(Tool):
        name, risk = "flaky", "read"

        def run(self, args, ctx):
            calls.append(1)
            return Observation(ok=len(calls) >= 3, error="timeout", retryable=True, summary="x")

    reg = Registry()
    reg.register(Flaky())
    obs, retries = reg.execute(Action(tool="flaky"), ToolContext(fresh, "r", fresh.runs_dir / "r"), retries=2)
    assert obs.ok and retries == 2


def test_verifier_check_types(fresh):
    class NoBrain:
        def judge(self, c, e):
            return True, "ok"

    v = Verifier(fresh, NoBrain())
    state = RunState(goal="g")
    checks = [
        Check(id="a", description="roster has Anita", type="csv_has_row", params={"path": "data/team_roster.csv", "match": {"email": "anita.rao@acme.test"}, "count": 1}),
        Check(id="b", description="missing file", type="file_exists", params={"path": "outputs/nope.md"}),
        Check(id="c", description="escape attempt", type="file_contains", params={"path": "../../x", "text": "y"}),
        Check(id="d", description="external host refused", type="http_json", params={"url": "https://example.com/x", "expect": {}}),
        Check(id="e", description="skipped when denied", type="file_exists", params={"path": "x"}, depends_on_tool="email.send"),
    ]
    state.denied_tools.append("email.send")
    res = {r.check_id: r.status for r in v.verify(checks, state)}
    assert res == {"a": "passed", "b": "failed", "c": "failed", "d": "failed", "e": "skipped"}


def test_request_policy_is_effect_based(policy):
    deact = policy.decide_request("POST", "http://127.0.0.1:8765/employees/12/deactivate")
    assert deact.verdict == "approve" and deact.rule == "request-deactivate"
    assert policy.decide_request("POST", "http://127.0.0.1:8765/employees").verdict == "auto"
    assert policy.decide_request("POST", "https://evil.example/collect").verdict == "deny"
    assert policy.decide_request("DELETE", "http://localhost/admin/users/3").verdict == "approve"
