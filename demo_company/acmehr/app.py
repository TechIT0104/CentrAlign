"""AcmeHR: a deliberately ordinary internal web app used as the agent's target system.

It has no API the agent is meant to use for writes: the agent has to operate it through the
browser like an employee would. A read-only JSON API exists solely for independent verification.
Failure injection (/__admin/chaos) simulates the ways real internal tools misbehave.
"""
from __future__ import annotations

import asyncio
import html
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DB_PATH = Path(os.getenv("ACMEHR_DB", "acmehr.db"))
USERNAME = "ops-bot"
PASSWORD = os.getenv("ACMEHR_PASSWORD", "ops-pass-123")
API_KEY = os.getenv("ACMEHR_API_KEY", "verifier-readonly-key")
DEPARTMENTS = ["Engineering", "Finance", "Design", "Sales", "People Ops"]
ACCESS = ["standard", "admin", "finance"]

app = FastAPI(title="AcmeHR")
_lock = threading.Lock()
sessions: dict[str, int] = {}  # sid -> authenticated request count
chaos: dict = {}

SEED = [
    ("Anita Rao", "anita.rao@acme.test", "Engineering", "2023-02-01", "admin", "", "active"),
    ("Meera Iyer", "meera.iyer@acme.test", "Finance", "2022-07-15", "finance", "", "active"),
    ("Sam Okafor", "sam.okafor@acme.test", "Design", "2024-01-08", "standard", "Anita Rao", "active"),
    ("Rahul Verma", "rahul.verma@acme.test", "Sales", "2021-05-03", "standard", "", "inactive"),
    ("Leila Haddad", "leila.haddad@acme.test", "Engineering", "2025-03-10", "standard", "Anita Rao", "active"),
]


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def reset_db() -> None:
    with _lock, db() as c:
        c.execute("DROP TABLE IF EXISTS employees")
        c.execute(
            """CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT, email TEXT UNIQUE,
               department TEXT, start_date TEXT, access_level TEXT, manager TEXT, status TEXT, created_at REAL)"""
        )
        c.executemany(
            "INSERT INTO employees(full_name,email,department,start_date,access_level,manager,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(*row, time.time()) for row in SEED],
        )
    sessions.clear()
    chaos.clear()


@app.on_event("startup")
def _startup() -> None:
    reset_db()


# ----------------------------------------------------------------- layout
def page(body: str, user: bool = True, title: str = "AcmeHR") -> HTMLResponse:
    nav = (
        '<nav><a href="/">Directory</a> <a href="/employees/new">New employee</a>'
        f'<span class="who">Signed in as {USERNAME}</span> <a href="/logout">Sign out</a></nav>'
        if user
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui;margin:0;background:#f6f7f9;color:#1b1f24}}nav{{background:#1f3a5f;padding:12px 24px;color:#fff}}
nav a{{color:#cfe0ff;margin-right:16px;text-decoration:none}}.who{{margin-left:32px;opacity:.8;margin-right:16px}}
main{{max-width:880px;margin:24px auto;background:#fff;padding:24px 32px;border-radius:8px;box-shadow:0 1px 3px #0002}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #e3e6ea}}
label{{display:block;margin:12px 0 4px;font-weight:600}}input,select{{padding:8px;width:340px}}
button{{margin-top:16px;padding:9px 18px;background:#1f6feb;color:#fff;border:0;border-radius:6px;cursor:pointer}}
button.danger{{background:#c62828}}.flash{{background:#e6f4ea;border:1px solid #8bc79a;padding:10px;border-radius:6px}}
.error{{background:#fdecea;border:1px solid #e5a29b;padding:10px;border-radius:6px}}</style></head>
<body>{nav}<main>{body}</main></body></html>"""
    )


def auth(request: Request):
    """Returns None when authenticated, or a redirect to the login page."""
    sid = request.cookies.get("sid")
    if sid not in sessions:
        return RedirectResponse("/login", status_code=303)
    sessions[sid] += 1
    limit = chaos.get("session_expiry_after")
    if limit and sessions[sid] > limit:  # one-shot expiry mid-task
        sessions.pop(sid, None)
        chaos.pop("session_expiry_after", None)
        return RedirectResponse("/login?expired=1", status_code=303)
    return None


def row_html(r: sqlite3.Row) -> str:
    return (
        f'<tr><td><a href="/employees/{r["id"]}">{html.escape(r["full_name"])}</a></td><td>{html.escape(r["email"])}</td>'
        f'<td>{r["department"]}</td><td>{r["start_date"]}</td><td>{r["access_level"]}</td><td>{r["status"]}</td></tr>'
    )


# ------------------------------------------------------------------ pages
@app.get("/login", response_class=HTMLResponse)
def login_form(expired: int = 0):
    note = '<p class="error">Your session expired. Please sign in again.</p>' if expired else ""
    return page(
        f"""<h1>Sign in</h1>{note}<form method="post" action="/login">
<label for="u">Username</label><input id="u" name="username">
<label for="p">Password</label><input id="p" name="password" type="password">
<br><button type="submit">Sign in</button></form>""",
        user=False,
        title="Sign in - AcmeHR",
    )


@app.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    if username != USERNAME or password != PASSWORD:
        return page('<h1>Sign in</h1><p class="error">Invalid credentials.</p><a href="/login">Try again</a>', user=False)
    sid = secrets.token_hex(8)
    sessions[sid] = 0
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sid", sid)
    return resp


@app.get("/logout")
def logout(request: Request):
    sessions.pop(request.cookies.get("sid", ""), None)
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def directory(request: Request, q: str = "", msg: str = ""):
    if (r := auth(request)) is not None:
        return r
    with db() as c:
        like = f"%{q}%"
        rows = c.execute(
            "SELECT * FROM employees WHERE full_name LIKE ? OR email LIKE ? ORDER BY full_name", (like, like)
        ).fetchall()
    flash = f'<p class="flash">{html.escape(msg)}</p>' if msg else ""
    body = "".join(row_html(r) for r in rows) or '<tr><td colspan="6">No employees found.</td></tr>'
    return page(
        f"""<h1>Employee directory</h1>{flash}<form method="get" action="/">
<label for="q">Search</label><input id="q" name="q" value="{html.escape(q)}"><button type="submit">Search</button></form>
<table><tr><th>Name</th><th>Email</th><th>Department</th><th>Start date</th><th>Access</th><th>Status</th></tr>{body}</table>"""
    )


@app.get("/employees/new", response_class=HTMLResponse)
def new_form(request: Request):
    if (r := auth(request)) is not None:
        return r
    label = "Add to directory" if chaos.get("rename_button") else "Create employee"
    dept = "".join(f"<option>{d}</option>" for d in DEPARTMENTS)
    acc = "".join(f"<option>{a}</option>" for a in ACCESS)
    return page(
        f"""<h1>New employee</h1><form method="post" action="/employees">
<label for="n">Full name</label><input id="n" name="full_name">
<label for="e">Work email</label><input id="e" name="email">
<label for="d">Department</label><select id="d" name="department">{dept}</select>
<label for="s">Start date</label><input id="s" name="start_date" type="date">
<label for="a">Access level</label><select id="a" name="access_level">{acc}</select>
<label for="m">Manager (optional)</label><input id="m" name="manager">
<br><button type="submit">{label}</button></form>"""
    )


@app.post("/employees")
async def create(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    department: str = Form(""),
    start_date: str = Form(""),
    access_level: str = Form("standard"),
    manager: str = Form(""),
):
    if (r := auth(request)) is not None:
        return r
    if chaos.get("slow_create"):
        await asyncio.sleep(float(chaos["slow_create"]))
    if chaos.get("flaky_create", 0) > 0:
        chaos["flaky_create"] -= 1
        return HTMLResponse("<h1>500 Internal Server Error</h1><p>Upstream HR service unavailable.</p>", status_code=500)
    if not (full_name.strip() and "@" in email and start_date):
        return page('<h1>New employee</h1><p class="error">Name, valid email and start date are required.</p>')
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO employees(full_name,email,department,start_date,access_level,manager,status,created_at)"
                " VALUES (?,?,?,?,?,?,'active',?)",
                (full_name.strip(), email.strip().lower(), department, start_date, access_level, manager.strip(), time.time()),
            )
            new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return page(f'<h1>New employee</h1><p class="error">An employee with email {html.escape(email)} already exists.</p>')
    return RedirectResponse(f"/employees/{new_id}?msg=Employee+created", status_code=303)


@app.get("/employees/{emp_id}", response_class=HTMLResponse)
def detail(request: Request, emp_id: int, msg: str = ""):
    if (r := auth(request)) is not None:
        return r
    with db() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not e:
        return page("<h1>Not found</h1>")
    flash = f'<p class="flash">{html.escape(msg)}</p>' if msg else ""
    action = (
        f'<form method="post" action="/employees/{emp_id}/deactivate"><button class="danger" type="submit">Deactivate employee</button></form>'
        if e["status"] == "active"
        else "<p>This employee is inactive.</p>"
    )
    return page(
        f"""<h1>{html.escape(e['full_name'])}</h1>{flash}<table>
<tr><th>Email</th><td>{html.escape(e['email'])}</td></tr><tr><th>Department</th><td>{e['department']}</td></tr>
<tr><th>Start date</th><td>{e['start_date']}</td></tr><tr><th>Access level</th><td>{e['access_level']}</td></tr>
<tr><th>Manager</th><td>{html.escape(e['manager'] or '-')}</td></tr><tr><th>Status</th><td>{e['status']}</td></tr></table>{action}"""
    )


@app.post("/employees/{emp_id}/deactivate")
def deactivate(request: Request, emp_id: int):
    if (r := auth(request)) is not None:
        return r
    with db() as c:
        c.execute("UPDATE employees SET status='inactive', access_level='standard' WHERE id=?", (emp_id,))
    return RedirectResponse(f"/employees/{emp_id}?msg=Employee+deactivated", status_code=303)


# --------------------------------------------- verification API (read-only)
@app.get("/api/employees")
def api_employees(email: str = "", q: str = "", x_api_key: str = Header("")):
    if x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")
    with db() as c:
        if email:
            rows = c.execute("SELECT * FROM employees WHERE lower(email)=lower(?)", (email,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM employees WHERE full_name LIKE ? OR email LIKE ?", (f"%{q}%", f"%{q}%")).fetchall()
    return {"items": [{k: r[k] for k in r.keys() if k != "created_at"} for r in rows]}


# ------------------------------------------------------------- chaos admin
@app.post("/__admin/chaos")
async def set_chaos(request: Request):
    chaos.update(await request.json())
    return {"chaos": chaos}


@app.post("/__admin/reset")
def admin_reset():
    reset_db()
    return {"ok": True}


@app.get("/__admin/health")
def health():
    return JSONResponse({"ok": True, "chaos": chaos})
