"""SQLite persistence: run state, tamper-evident audit log, interventions, episodic memory, feedback."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import RunState

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, state_json TEXT NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL NOT NULL,
  type TEXT NOT NULL, data_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_run ON events(run_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  response_json TEXT, created REAL NOT NULL, resolved REAL
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, goal TEXT NOT NULL, outcome TEXT NOT NULL,
  lessons_json TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, text TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
  source TEXT NOT NULL DEFAULT 'human', created REAL NOT NULL
);
"""

CREATE_SCHEDULES = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT NOT NULL, every_seconds INTEGER NOT NULL,
  next_run REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_run_id TEXT, created REAL NOT NULL
);
"""

GENESIS = "0" * 64


def _hash(prev: str, run_id: str, ts: float, type_: str, data_json: str) -> str:
    return hashlib.sha256(f"{prev}|{run_id}|{ts!r}|{type_}|{data_json}".encode()).hexdigest()


class Store:
    def __init__(self, path: Path, anchor_dir: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.anchor_dir = anchor_dir or path.parent / "anchors"
        self._key_path = path.parent / "audit.key"
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.executescript(CREATE_SCHEDULES)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(runs)")}
            for col, typ in (("lease_owner", "TEXT"), ("lease_until", "REAL")):  # migrate older databases
                if col not in cols:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ runs
    def create_run(self, goal: str) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        state = RunState(goal=goal)
        now = time.time()
        self._q(
            "INSERT INTO runs(id, goal, state_json, status, created, updated) VALUES (?,?,?,?,?,?)",
            (run_id, goal, state.model_dump_json(), state.status, now, now),
        )
        return run_id

    def load_run(self, run_id: str) -> RunState:
        rows = self._q("SELECT state_json FROM runs WHERE id=?", (run_id,))
        if not rows:
            raise KeyError(f"unknown run {run_id}")
        return RunState.model_validate_json(rows[0]["state_json"])

    def save_run(self, run_id: str, state: RunState) -> None:
        self._q(
            "UPDATE runs SET state_json=?, status=?, updated=? WHERE id=?",
            (state.model_dump_json(), state.status, time.time(), run_id),
        )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q("SELECT id, goal, status, created, updated FROM runs ORDER BY created DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- leases
    def claim_lease(self, run_id: str, owner: str, ttl: float) -> bool:
        """Atomically become the only worker allowed to advance a run (or renew our own lease)."""
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "UPDATE runs SET lease_owner=?, lease_until=? WHERE id=? "
                "AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until<? OR lease_owner=?)",
                (owner, now + ttl, run_id, now, owner),
            )
            return cur.rowcount == 1

    def release_lease(self, run_id: str, owner: str) -> None:
        self._q("UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE id=? AND lease_owner=?", (run_id, owner))

    def orphaned_runs(self, grace: float = 30.0) -> list[str]:
        """Runs that claim to be running but whose worker is gone (lease expired, or never taken)."""
        now = time.time()
        rows = self._q(
            "SELECT id FROM runs WHERE status='running' AND (lease_until IS NULL OR lease_until<?) AND updated<?",
            (now, now - grace),
        )
        return [r["id"] for r in rows]

    # -------------------------------------------------------- schedules
    def add_schedule(self, goal: str, every_seconds: int, first_run: float | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO schedules(goal, every_seconds, next_run, created) VALUES (?,?,?,?)",
                (goal, every_seconds, first_run if first_run is not None else now + every_seconds, now),
            )
            return int(cur.lastrowid)

    def schedules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM schedules ORDER BY id")]

    def due_schedules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (time.time(),))]

    def mark_scheduled(self, schedule_id: int, run_id: str, next_run: float) -> None:
        self._q("UPDATE schedules SET last_run_id=?, next_run=? WHERE id=?", (run_id, next_run, schedule_id))

    def remove_schedule(self, schedule_id: int) -> None:
        self._q("DELETE FROM schedules WHERE id=?", (schedule_id,))

    # ------------------------------------------------------- audit log
    def add_event(self, run_id: str, type_: str, data: dict[str, Any]) -> int:
        with self._lock:
            prev = self._q("SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,))
            prev_hash = prev[0]["hash"] if prev else GENESIS
            ts = time.time()
            data_json = json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
            h = _hash(prev_hash, run_id, ts, type_, data_json)
            cur = self._db.execute(
                "INSERT INTO events(run_id, ts, type, data_json, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (run_id, ts, type_, data_json, prev_hash, h),
            )
            return int(cur.lastrowid)

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after))
        return [
            {"seq": r["seq"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data_json"]), "hash": r["hash"]}
            for r in rows
        ]

    def verify_audit_chain(self, run_id: str) -> tuple[bool, str]:
        """Recompute the hash chain; any edited or removed row breaks it."""
        prev = GENESIS
        for r in self._q("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)):
            if r["prev_hash"] != prev or _hash(prev, run_id, r["ts"], r["type"], r["data_json"]) != r["hash"]:
                return False, f"chain broken at event #{r['seq']}"
            prev = r["hash"]
        return self._verify_anchor(run_id)

    def _verify_anchor(self, run_id: str) -> tuple[bool, str]:
        path = self.anchor_dir / f"{run_id}.json"
        if not path.exists():
            return True, "audit chain intact (no signed anchor written yet)"
        a = json.loads(path.read_text())
        if not hmac.compare_digest(a["mac"], self._anchor_mac(run_id, a["count"], a["last_hash"])):
            return False, "signed anchor was altered or signed with a different key"
        rows = self._q("SELECT hash FROM events WHERE run_id=? ORDER BY seq", (run_id,))
        if len(rows) < a["count"]:
            return False, f"events were removed: {a['count']} were anchored, {len(rows)} remain"
        if rows[a["count"] - 1]["hash"] != a["last_hash"]:
            return False, "the history was rewritten after it was anchored"
        return True, f"audit chain intact; first {a['count']} events match the signed anchor"

    # --------------------------------------------------- audit anchors
    def _audit_key(self) -> bytes:
        """HMAC key for anchors. Prefer CENTRALIGN_AUDIT_KEY from a secret store; else a key file beside the DB."""
        if env := os.getenv("CENTRALIGN_AUDIT_KEY"):
            return env.encode()
        if not self._key_path.exists():
            self._key_path.write_text(secrets.token_hex(32))
            try:
                os.chmod(self._key_path, 0o600)
            except OSError:
                pass
        return self._key_path.read_text().strip().encode()

    def _anchor_mac(self, run_id: str, count: int, last_hash: str) -> str:
        return hmac.new(self._audit_key(), f"{run_id}|{count}|{last_hash}".encode(), hashlib.sha256).hexdigest()

    def write_anchor(self, run_id: str) -> None:
        """Sign (event count, latest hash) outside the events table. Deleting or rewriting anything
        recorded before this point can no longer be hidden by recomputing the in-database chain."""
        rows = self._q("SELECT hash FROM events WHERE run_id=? ORDER BY seq", (run_id,))
        if not rows:
            return
        self.anchor_dir.mkdir(parents=True, exist_ok=True)
        anchor = {"run_id": run_id, "count": len(rows), "last_hash": rows[-1]["hash"]}
        anchor["mac"] = self._anchor_mac(run_id, anchor["count"], anchor["last_hash"])
        (self.anchor_dir / f"{run_id}.json").write_text(json.dumps(anchor))

    # --------------------------------------------------- interventions
    def create_intervention(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO interventions(run_id, kind, payload_json, created) VALUES (?,?,?,?)",
                (run_id, kind, json.dumps(payload, default=str), time.time()),
            )
            return int(cur.lastrowid)

    def get_intervention(self, iid: int) -> dict[str, Any]:
        rows = self._q("SELECT * FROM interventions WHERE id=?", (iid,))
        if not rows:
            raise KeyError(f"unknown intervention {iid}")
        r = rows[0]
        return {
            "id": r["id"], "run_id": r["run_id"], "kind": r["kind"], "status": r["status"],
            "payload": json.loads(r["payload_json"]),
            "response": json.loads(r["response_json"]) if r["response_json"] else None,
        }

    def resolve_intervention(self, iid: int, response: dict[str, Any]) -> None:
        self._q(
            "UPDATE interventions SET status='resolved', response_json=?, resolved=? WHERE id=? AND status='pending'",
            (json.dumps(response), time.time(), iid),
        )

    def pending_interventions(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM interventions WHERE status='pending'"
        args: tuple = ()
        if run_id:
            sql += " AND run_id=?"
            args = (run_id,)
        return [self.get_intervention(r["id"]) for r in self._q(sql + " ORDER BY id", args)]

    # ----------------------------------------------------------- memory
    def add_episode(self, run_id: str, goal: str, outcome: str, lessons: list[str]) -> None:
        self._q(
            "INSERT INTO episodes(run_id, goal, outcome, lessons_json, created) VALUES (?,?,?,?,?)",
            (run_id, goal, outcome, json.dumps(lessons), time.time()),
        )

    def episodes(self) -> list[dict[str, Any]]:
        return [
            {"run_id": r["run_id"], "goal": r["goal"], "outcome": r["outcome"], "lessons": json.loads(r["lessons_json"])}
            for r in self._q("SELECT * FROM episodes ORDER BY id DESC")
        ]

    def add_feedback(self, text: str, run_id: str | None = None, scope: str = "global", source: str = "human") -> None:
        self._q(
            "INSERT INTO feedback(run_id, text, scope, source, created) VALUES (?,?,?,?,?)",
            (run_id, text, scope, source, time.time()),
        )

    def feedback(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM feedback ORDER BY id DESC")]
