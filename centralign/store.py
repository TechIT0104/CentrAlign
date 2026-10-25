"""SQLite persistence: run state, tamper-evident audit log, interventions, episodic memory, feedback."""
from __future__ import annotations

import hashlib
import json
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

GENESIS = "0" * 64


def _hash(prev: str, run_id: str, ts: float, type_: str, data_json: str) -> str:
    return hashlib.sha256(f"{prev}|{run_id}|{ts!r}|{type_}|{data_json}".encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ runs
    def create_run(self, goal: str) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        state = RunState(goal=goal)
        now = time.time()
        self._q(
            "INSERT INTO runs VALUES (?,?,?,?,?,?)",
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
        return True, "audit chain intact"

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
