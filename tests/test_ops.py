"""Operational guarantees: leases, crash recovery, scheduling and audit anchoring. No browser needed."""
import json
import threading
import time

import pytest

from centralign.runtime import Runtime, parse_every
from centralign.store import Store, _hash

UNSUPPORTED = "Reconcile the vendor invoices against the ledger"  # fails fast: no browser involved


def kinds(rt, run_id):
    return [e["type"] for e in rt.store.events(run_id)]


def test_only_the_lease_holder_can_advance_a_run(fresh):
    rt = Runtime(fresh)
    rid = rt.submit(UNSUPPORTED)
    assert rt.store.claim_lease(rid, "another-worker", 60)
    state = rt.advance(rid)  # blocked: someone else owns the run
    assert state.status == "running" and kinds(rt, rid) == ["run.created"]
    rt.store.release_lease(rid, "another-worker")
    assert rt.advance(rid).status == "failed"


def test_expired_lease_can_be_taken_over(fresh):
    rt = Runtime(fresh)
    rid = rt.submit(UNSUPPORTED)
    assert rt.store.claim_lease(rid, "crashed-worker", -1)  # already expired
    assert rt.advance(rid).status == "failed"


def test_two_workers_racing_on_one_run_act_once(fresh):
    a, b = Runtime(fresh), Runtime(fresh)  # separate in-process locks, shared database
    rid = a.submit(UNSUPPORTED)
    gate = threading.Barrier(2)

    def go(rt):
        gate.wait()
        rt.advance(rid)

    ts = [threading.Thread(target=go, args=(r,)) for r in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert kinds(a, rid).count("plan.started") == 1
    assert kinds(a, rid).count("run.finished") == 1


def test_resolve_refuses_while_another_worker_holds_the_run(fresh):
    rt = Runtime(fresh)
    rid = rt.submit("Onboard Dana Lee")
    rt.advance(rid)  # now waiting for the department answer
    [iv] = rt.store.pending_interventions(rid)
    rt.store.claim_lease(rid, "other", 60)
    with pytest.raises(ValueError, match="another worker"):
        rt.resolve(iv["id"], answer="Design")
    rt.store.release_lease(rid, "other")
    rt.resolve(iv["id"], answer="Design")
    rt.cancel(rid)


def test_worker_recovers_orphaned_runs(fresh):
    rt = Runtime(fresh)
    rid = rt.submit(UNSUPPORTED)
    rt.store._q("UPDATE runs SET updated=updated-120 WHERE id=?", (rid,))  # abandoned two minutes ago
    assert rt.recover_orphans(grace=30) == [rid]
    rt._pool.shutdown(wait=True)
    assert rt.state(rid).status == "failed" and "run.recovered" in kinds(rt, rid)
    assert rt.recover_orphans(grace=30) == []  # finished runs are not orphans


def test_scheduler_starts_due_tasks_once_per_slot(fresh):
    rt = Runtime(fresh)
    sid = rt.store.add_schedule(UNSUPPORTED, every_seconds=3600, first_run=time.time() - 5)
    first = rt.worker_tick()["scheduled"]
    assert len(first) == 1
    assert rt.worker_tick()["scheduled"] == []  # next slot is an hour away
    [sch] = rt.store.schedules()
    assert sch["id"] == sid and sch["last_run_id"] == first[0] and sch["next_run"] > time.time() + 3000
    rt._pool.shutdown(wait=True)


def test_interval_parsing():
    assert [parse_every(x) for x in ("90s", "15m", "6h", "1d", "2w")] == [90, 900, 21600, 86400, 1209600]
    for bad in ("", "0m", "5x", "soon"):
        with pytest.raises(ValueError):
            parse_every(bad)


# ---------------------------------------------------------------- audit anchors
def build(tmp_path, n=6):
    store = Store(tmp_path / "db.sqlite")
    rid = store.create_run("g")
    for i in range(n):
        store.add_event(rid, "step", {"i": i})
    store.write_anchor(rid)
    return store, rid


def disarm(store):
    store._db.execute("DROP TRIGGER events_no_update")
    store._db.execute("DROP TRIGGER events_no_delete")


def test_anchor_detects_truncation_that_the_chain_alone_cannot(tmp_path):
    store, rid = build(tmp_path)
    assert store.verify_audit_chain(rid) == (True, "audit chain intact; first 6 events match the signed anchor")
    disarm(store)
    store._db.execute("DELETE FROM events WHERE run_id=? AND seq=(SELECT max(seq) FROM events)", (rid,))
    ok, detail = store.verify_audit_chain(rid)
    assert not ok and "removed" in detail  # the remaining chain is internally consistent; only the anchor notices


def test_anchor_detects_a_rewritten_and_rehashed_history(tmp_path):
    store, rid = build(tmp_path)
    disarm(store)
    rows = store._q("SELECT * FROM events WHERE run_id=? ORDER BY seq", (rid,))
    prev = rows[1]["hash"]
    store._db.execute("UPDATE events SET data_json=? WHERE seq=?", (json.dumps({"i": 999}), rows[2]["seq"]))
    for r in rows[2:]:  # an insider recomputes every later hash so the in-database chain still verifies
        data = json.dumps({"i": 999}) if r["seq"] == rows[2]["seq"] else r["data_json"]
        h = _hash(prev, rid, r["ts"], r["type"], data)
        store._db.execute("UPDATE events SET prev_hash=?, hash=? WHERE seq=?", (prev, h, r["seq"]))
        prev = h
    ok, detail = store.verify_audit_chain(rid)
    assert not ok and "rewritten" in detail


def test_tampered_anchor_is_rejected(tmp_path):
    store, rid = build(tmp_path)
    path = store.anchor_dir / f"{rid}.json"
    a = json.loads(path.read_text())
    a["count"] = 1
    path.write_text(json.dumps(a))
    ok, detail = store.verify_audit_chain(rid)
    assert not ok and "altered" in detail


def test_events_after_the_anchor_are_still_valid(tmp_path):
    store, rid = build(tmp_path)
    store.add_event(rid, "later", {})
    assert store.verify_audit_chain(rid)[0]


def test_finished_runs_are_anchored(fresh):
    rt = Runtime(fresh)
    rid = rt.submit(UNSUPPORTED)
    rt.advance(rid)
    assert (rt.store.anchor_dir / f"{rid}.json").exists()
    assert "signed anchor" in rt.store.verify_audit_chain(rid)[1]


def test_old_databases_are_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE runs (id TEXT PRIMARY KEY, goal TEXT NOT NULL, state_json TEXT NOT NULL, "
                      "status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);")
    con.close()
    store = Store(db)
    rid = store.create_run("g")
    assert store.claim_lease(rid, "me", 10) and not store.claim_lease(rid, "you", 10)
