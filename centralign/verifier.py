"""Independent outcome verification.

The verifier never trusts the executor's narrative. It re-reads the real end state (files, CSVs, the
system of record's API) with its own read-only access, and only uses an LLM judge for qualities code
cannot test, giving that judge raw evidence rather than the agent's summary.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .brains.base import Brain
from .config import Settings
from .models import Check, CheckResult, RunState
from .tools.base import safe_path

INTERNAL_HOSTS = {"127.0.0.1", "localhost"}


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


class Verifier:
    def __init__(self, settings: Settings, brain: Brain):
        self.s, self.brain = settings, brain

    # ------------------------------------------------------------------ api
    def verify(self, checks: list[Check], state: RunState) -> list[CheckResult]:
        return [self._one(c, state) for c in checks]

    def _one(self, c: Check, state: RunState) -> CheckResult:
        if c.depends_on_tool and c.depends_on_tool in state.denied_tools:
            return CheckResult(check_id=c.id, description=c.description, status="skipped",
                               detail=f"skipped: {c.depends_on_tool} was declined by a human/policy")
        fn = getattr(self, f"_check_{c.type}", None)
        try:
            if fn is None:
                raise ValueError(f"unknown check type {c.type}")
            ok, detail, evidence = fn(c.params)
        except Exception as e:  # a check that cannot run is a failure, never a pass
            ok, detail, evidence = False, f"check could not run: {type(e).__name__}: {e}", ""
        return CheckResult(check_id=c.id, description=c.description, status="passed" if ok else "failed",
                           detail=detail, evidence=evidence[:1500])

    # --------------------------------------------------------------- helpers
    def _resolve(self, p: dict) -> list[Path]:
        root = self.s.files_dir.resolve()
        if "glob" in p:
            if ".." in p["glob"] or p["glob"].startswith(("/", "\\")):
                raise PermissionError("glob escapes the sandbox")
            return sorted(m for m in root.glob(p["glob"]) if m.is_file())
        f = safe_path(root, p["path"])
        return [f] if f.is_file() else []

    # ---------------------------------------------------------------- checks
    def _check_file_exists(self, p: dict):
        files = self._resolve(p)
        return bool(files), (f"found {len(files)} file(s)" if files else "no matching file"), "\n".join(str(f.name if hasattr(f, 'name') else f) for f in files)

    def _check_file_contains(self, p: dict):
        files = self._resolve(p)
        if not files:
            return False, f"no file matches {p.get('path') or p.get('glob')}", ""
        text = open(files[0], encoding="utf-8", errors="replace").read()
        ok = p["text"].lower() in text.lower()
        return ok, (f"file contains {p['text']!r}" if ok else f"{p['text']!r} not found in file"), text

    def _check_csv_has_row(self, p: dict):
        files = self._resolve(p)
        if not files:
            return False, f"no such file {p['path']}", ""
        with open(files[0], newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        match = {k: str(v).lower() for k, v in p["match"].items()}
        hits = [r for r in rows if all(str(r.get(k, "")).lower() == v for k, v in match.items())]
        want = p.get("count")
        ok = len(hits) == int(want) if want is not None else len(hits) >= 1
        return ok, f"{len(hits)} row(s) match {p['match']}" + (f" (expected exactly {want})" if want is not None else ""), str(hits)

    def _check_http_json(self, p: dict):
        url = p["url"]
        if (urlparse(url).hostname or "") not in INTERNAL_HOSTS:
            return False, "verifier only queries approved internal hosts", ""
        r = httpx.get(url, headers={"X-API-Key": self.s.acmehr_api_key}, timeout=10)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} from {url}", r.text[:300]
        data = r.json()
        problems = []
        for path, want in p["expect"].items():
            try:
                got = dig(data, path)
            except (KeyError, IndexError, ValueError):
                problems.append(f"{path} missing")
                continue
            if str(got).lower() != str(want).lower():
                problems.append(f"{path}: expected {want!r}, got {got!r}")
        return not problems, ("all expected fields match" if not problems else "; ".join(problems)), str(data)

    def _check_llm_judge(self, p: dict):
        files = self._resolve(p)
        evidence = open(files[0], encoding="utf-8", errors="replace").read() if files else ""
        chk = Check(id="judge", description=p.get("question", ""), type="llm_judge", params=p)
        ok, why = self.brain.judge(chk, evidence)
        return ok, why, evidence
