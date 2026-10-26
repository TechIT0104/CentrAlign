"""Helpers to run the bundled demo company: start AcmeHR, reset data, inject failures."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

from .config import ROOT, Settings

CHAOS_PRESETS = {
    "flaky": {"flaky_create": 1},  # first save returns HTTP 500
    "rename": {"rename_button": True},  # submit button label changes
    "expiry": {"session_expiry_after": 5},  # session dies mid-task
    "slow": {"slow_create": 2.0},  # sluggish save
    "relabel": {"rename_deactivate": True},  # "Deactivate employee" becomes "Disable account"
    # Failures the offline scripted brain was NOT written for (a model must adapt, or the run must stop safely):
    "newfield": {"require_employment_type": True},  # the form gains a required field
    "shuffle": {"shuffle_fields": True},  # field names/labels change
    "weekday": {"weekday_only": True},  # validation: start date must be Mon-Fri
    "outage": {"maintenance_once": 1},  # the form page returns 503 once
}


def chaos_from_names(names: str) -> dict:
    out: dict = {}
    for n in (x.strip() for x in names.split(",") if x.strip()):
        if n == "all":
            for k in ("flaky", "rename", "expiry"):
                out.update(CHAOS_PRESETS[k])
        elif n in CHAOS_PRESETS:
            out.update(CHAOS_PRESETS[n])
        else:
            raise SystemExit(f"unknown chaos preset {n!r}; choose from {', '.join(CHAOS_PRESETS)}, all")
    return out


def reset_files(s: Settings) -> None:
    """Restore the company drive to its seed state."""
    if s.files_dir.exists():
        shutil.rmtree(s.files_dir)
    shutil.copytree(s.company_dir / "files_seed", s.files_dir)
    # Keep the roster consistent with AcmeHR's relative-date seed people (see acmehr.app.upcoming_hires).
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from demo_company.acmehr.app import upcoming_hires

    roster = s.files_dir / "data" / "team_roster.csv"
    with roster.open("a", newline="", encoding="utf-8") as f:
        for name, email, dept, start, _acc, _mgr, status in upcoming_hires():
            f.write(f"{name},{email},{dept},{start},{status}\n")


def acmehr_up(s: Settings) -> bool:
    try:
        return httpx.get(f"{s.acmehr_url}/__admin/health", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def start_acmehr(s: Settings) -> subprocess.Popen | None:
    """Start the mock HR app if it is not already running. Returns the process we started (if any)."""
    if acmehr_up(s):
        return None
    port = urlparse(s.acmehr_url).port or 8765
    env = {**os.environ, "ACMEHR_DB": str(s.home / "acmehr.db")}
    s.home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "demo_company.acmehr.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
    )
    for _ in range(50):
        if acmehr_up(s):
            return proc
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("AcmeHR failed to start")


def reset_acmehr(s: Settings, chaos: dict | None = None) -> None:
    httpx.post(f"{s.acmehr_url}/__admin/reset", timeout=5)
    if chaos:
        httpx.post(f"{s.acmehr_url}/__admin/chaos", json=chaos, timeout=5)


def prepare_demo(s: Settings, chaos: dict | None = None, fresh: bool = True) -> subprocess.Popen | None:
    s.ensure_dirs()
    # The demo company's service-account password is a fake; default it so a missing .env cannot break sign-in.
    os.environ.setdefault("SECRET_ACMEHR_PASSWORD", "ops-pass-123")
    os.environ.setdefault("ACMEHR_PASSWORD", "ops-pass-123")
    proc = start_acmehr(s)
    if fresh:
        reset_files(s)
        reset_acmehr(s, chaos)
    elif chaos:
        httpx.post(f"{s.acmehr_url}/__admin/chaos", json=chaos, timeout=5)
    return proc
