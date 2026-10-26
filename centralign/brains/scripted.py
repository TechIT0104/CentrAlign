"""Offline, deterministic stand-in for an LLM brain.

It exists so the whole runtime (policy, approvals, recovery, verification, audit, UI) can be run and
tested without an API key or network. It is NOT the product's intelligence: it only knows the
onboarding and offboarding procedures, and it still has to *read* what the tools return and adapt
(re-login, retry 500s, rediscover renamed buttons, ask when a duplicate appears). For anything else
it raises BrainUnsupported and the run fails fast with a clear message.

Each procedure is a generator: it yields Actions/Asks and receives Observations. The brain is
stateless, because it replays the recorded history into a fresh generator to find its place,
which is what lets a paused run resume after an approval, or even after a process restart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Generator

from ..models import Action, Check, Decision, MissingInfo, Observation, Plan, RunState, Step
from .base import Brain, BrainUnsupported, PlanRequest, StepContext

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
JARGON = ["synergy", "leverage", "circle back", "paradigm", "bandwidth", "touch base"]


class Fail(Exception):
    pass


@dataclass
class Done:
    summary: str
    facts: dict[str, Any] | None = None
    skipped: bool = False


@dataclass
class Ask:
    question: str


Handler = Generator[Action | Ask, Observation, Done]


# --------------------------------------------------------------------- helpers
def A(tool: str, thought: str = "", **args: Any) -> Action:
    return Action(tool=tool, args=args, thought=thought)


def find(obs: Observation, *needles: str, tag: str | None = None) -> int | None:
    """Pick an interactive element by purpose (name/label/text), in priority order of needles."""
    els = obs.data.get("elements", [])
    for needle in needles:
        for e in els:
            if tag and e.get("tag") != tag:
                continue
            hay = " ".join(str(e.get(k, "")) for k in ("name", "label", "text")).lower()
            if needle in hay:
                return e["id"]
    return None


def at_login(obs: Observation) -> bool:
    return "/login" in str(obs.data.get("url", ""))


def need(el: int | None, what: str) -> int:
    if el is None:
        raise Fail(f"could not find {what} on the page")
    return el


def login(p: dict) -> Handler:
    page = yield A("browser.read_page", "Read the sign-in form")
    user = need(find(page, "username", tag="input"), "username field")
    pwd = need(find(page, "password", tag="input"), "password field")
    btn = need(find(page, "sign in", tag="button"), "sign-in button")
    yield A("browser.fill", "Enter service-account username", target=user, value="ops-bot")
    yield A("browser.fill", "Enter password from the secret vault", target=pwd, value="{{secret:ACMEHR_PASSWORD}}")
    obs = yield A("browser.click", "Submit credentials", target=btn)
    if at_login(obs):
        raise Fail("sign-in was rejected")
    return Done("signed in")


def nav(p: dict, path: str) -> Generator[Action | Ask, Observation, Observation]:
    """Go to a page; if the session expired, sign back in and retry the navigation."""
    url = p["base"] + path
    obs = yield A("browser.goto", f"Open {path}", url=url)
    if at_login(obs):
        yield from login(p)
        obs = yield A("browser.goto", f"Re-open {path} after signing in again", url=url)
        if at_login(obs):
            raise Fail("redirected to sign-in even after logging in")
    return obs


# ------------------------------------------------------------ onboarding steps
def h_login(ctx) -> Handler:
    p = ctx.step.params
    obs = yield A("browser.goto", "Open AcmeHR", url=p["base"] + "/")
    if not at_login(obs):
        return Done("already signed in")
    return (yield from login(p))


def h_dup_check(ctx) -> Handler:
    p = ctx.step.params
    obs = yield from nav(p, f"/?q={p['email']}")
    page = yield A("browser.read_page", "Look for an existing record")
    text = page.data.get("text", "")
    m = re.search(re.escape(p["email"]) + r".*?\b(inactive|active)\b", text, re.S)
    if not m:
        return Done(f"no existing record for {p['email']}", {"reuse": False})
    answer = yield Ask(
        f"AcmeHR already has a record for {p['email']} (status: {m.group(1)}). The SOP forbids duplicates. "
        "Reply 'reuse' to keep the existing record and continue with the remaining steps, or 'stop' to abort."
    )
    if "reuse" in answer.data.get("answer", "").lower():
        return Done("existing record found; human chose to reuse it", {"reuse": True})
    raise Fail("existing record found; human chose to stop")


def h_create_employee(ctx) -> Handler:
    p = ctx.step.params
    if ctx.facts.get("reuse"):
        return Done("skipped creation: reusing the existing record", skipped=True)
    for attempt in range(1, 5):
        yield from nav(p, "/employees/new")
        page = yield A("browser.read_page", "Read the new-employee form")
        yield A("browser.fill", "Full name", target=need(find(page, "full_name", "full name"), "name field"), value=p["name"])
        yield A("browser.fill", "Work email", target=need(find(page, "email"), "email field"), value=p["email"])
        yield A("browser.select", "Department per request",
                target=need(find(page, "department", tag="select"), "department"), value=p["department"])
        yield A("browser.fill", "Start date", target=need(find(page, "start_date", "start date"), "start date"), value=p["start_date"])
        if p["access"] != "standard":
            yield A("browser.select", "Access level as requested",
                    target=need(find(page, "access_level", "access", tag="select"), "access level"), value=p["access"])
        # The submit button's label changes between releases: choose it by purpose.
        btn = need(find(page, "create employee", "add employee", "add to directory", "save", "submit", tag="button"), "submit button")
        obs = yield A("browser.click", "Submit the form", target=btn)
        text = obs.data.get("page_text", "")
        if at_login(obs):
            continue  # session expired mid-form; nav() will sign in again
        if (obs.data.get("http_status") or 0) >= 500 or "500 Internal Server Error" in text:
            # Per the systems guide: check the directory before retrying in case the save went through.
            yield from nav(p, f"/?q={p['email']}")
            check = yield A("browser.read_page", "Check whether the record was saved despite the error")
            if p["email"] in check.data.get("text", ""):
                return Done(f"{p['email']} exists in AcmeHR (server error but the save went through)")
            continue
        if "already exists" in text:
            raise Fail(f"AcmeHR reports {p['email']} already exists")
        if "Employee created" in text:
            m = re.search(r"/employees/(\d+)", str(obs.data.get("url", "")))
            return Done(f"created employee record in AcmeHR (id {m.group(1) if m else '?'})", {"employee_id": m.group(1) if m else None})
        raise Fail(f"unexpected page after submitting: {text[:120]!r}")
    raise Fail("could not save the employee after 4 attempts")


def h_roster(ctx) -> Handler:
    p = ctx.step.params
    row = {"name": p["name"], "email": p["email"], "department": p["department"], "start_date": p["start_date"], "status": "active"}
    obs = yield A("csv.upsert_row", "Add/refresh the roster row", path="data/team_roster.csv", key="email", row=row)
    if not obs.ok:
        raise Fail(obs.error or "roster update failed")
    back = yield A("csv.read", "Read back to confirm exactly one row", path="data/team_roster.csv", where={"email": p["email"]})
    if len(back.data.get("rows", [])) != 1:
        raise Fail("roster does not contain exactly one row for the employee")
    return Done("roster updated and confirmed")


def welcome_text(p: dict) -> str:
    d = date.fromisoformat(p["start_date"])
    first = p["first"]
    return (
        f"# Welcome to Acme, {first}!\n\n"
        f"Hi {first},\n\nWe're really glad you're joining {p['department']} on "
        f"{d.strftime('%A')}, {d.day} {d.strftime('%B %Y')}.\n\n"
        "## Your first week\n"
        "- Monday: a friendly intro session with People Ops, then laptop and accounts setup.\n"
        "- Wednesday: team lunch, so you can meet everyone.\n\n"
        "Questions before you start? Write to people-ops@acme.test and we'll help.\n"
    )


def h_welcome_doc(ctx) -> Handler:
    p = ctx.step.params
    path = f"outputs/welcome_{p['first'].lower()}_{p['last'].lower()}.md"
    obs = yield A("fs.write", "Write the welcome document", path=path, content=welcome_text(p))
    if not obs.ok:
        raise Fail(obs.error or "could not write welcome doc")
    return Done(f"wrote {path}", {"welcome_path": path})


def h_send_email(ctx) -> Handler:
    p = ctx.step.params
    obs = yield A("email.send", "Send the welcome email", to=p["email"], subject=f"Welcome to Acme, {p['first']}!", body=welcome_text(p))
    if obs.data.get("denied"):
        return Done(f"email not sent: {obs.error}. The welcome document remains in outputs/", skipped=True)
    if not obs.ok:
        raise Fail(obs.error or "email failed")
    return Done(f"welcome email sent to {p['email']}")


# ----------------------------------------------------------- offboarding steps
def h_find_employee(ctx) -> Handler:
    p = ctx.step.params
    yield from nav(p, f"/?q={p['name'].replace(' ', '+')}")
    page = yield A("browser.read_page", "Find the person in the directory")
    links = [e for e in page.data.get("elements", []) if str(e.get("href", "")).startswith("/employees/")
             and p["name"].lower() in str(e.get("text", "")).lower()]
    if not links:
        raise Fail(f"no employee named {p['name']} in AcmeHR")
    chosen = links[0]
    if len(links) > 1:
        ids = [l["href"].rsplit("/", 1)[1] for l in links]
        ans = yield Ask(f"{len(links)} employees match '{p['name']}' (AcmeHR ids {', '.join(ids)}). Reply with the id to use.")
        m = re.search(r"\d+", ans.data.get("answer", ""))
        chosen = next((l for l in links if m and l["href"].endswith("/" + m.group())), None)
        if not chosen:
            raise Fail("human did not pick one of the matches")
    emp_id = chosen["href"].rsplit("/", 1)[1]
    yield from nav(p, f"/employees/{emp_id}")
    detail = yield A("browser.read_page", "Read the employee's record")
    text = detail.data.get("text", "")
    em = re.search(r"[\w.+-]+@[\w.-]+", text)
    status = "inactive" if re.search(r"Status\s+inactive", text) else "active"
    return Done(f"found {p['name']} (id {emp_id}, {status})",
                {"employee_id": emp_id, "email": em.group() if em else "", "already_inactive": status == "inactive"})


def h_deactivate(ctx) -> Handler:
    p = ctx.step.params
    if ctx.facts.get("already_inactive"):
        return Done("employee was already inactive", skipped=True)
    yield from nav(p, f"/employees/{ctx.facts['employee_id']}")
    page = yield A("browser.read_page", "Read the employee page")
    btn = need(find(page, "deactivate", "disable", "remove access", tag="button"), "account-removal button")
    obs = yield A("browser.click", "Deactivate the account", target=btn)
    if obs.data.get("denied"):
        return Done(f"deactivation not performed: {obs.error}", skipped=True)
    if "Employee deactivated" not in obs.data.get("page_text", ""):
        raise Fail("AcmeHR did not confirm the deactivation")
    return Done("account deactivated in AcmeHR")


def h_roster_inactive(ctx) -> Handler:
    email = ctx.facts.get("email")
    if not email:
        raise Fail("no email found for the employee, cannot update the roster")
    obs = yield A("csv.upsert_row", "Mark inactive in the roster", path="data/team_roster.csv", key="email",
                  row={"email": email, "status": "inactive"})
    if not obs.ok:
        raise Fail(obs.error or "roster update failed")
    return Done("roster marked inactive")


HANDLERS = {
    "login": h_login, "dup_check": h_dup_check, "create_employee": h_create_employee, "roster": h_roster,
    "welcome_doc": h_welcome_doc, "send_email": h_send_email, "find_employee": h_find_employee,
    "deactivate": h_deactivate, "roster_inactive": h_roster_inactive,
}


# ------------------------------------------------------------------- the brain
class _Ctx:
    def __init__(self, ctx: StepContext):
        self.step, self.facts = ctx.step, ctx.facts


class ScriptedBrain(Brain):
    name = "scripted"

    def __init__(self, acmehr_url: str):
        self.base = acmehr_url.rstrip("/")

    # -- planning ---------------------------------------------------------
    def plan(self, req: PlanRequest) -> Plan:
        goal = req.goal
        low = goal.lower()
        if re.search(r"\b(offboard|terminate|deactivate|off-board)\b", low):
            return self._plan_offboard(req)
        if re.search(r"\b(onboard|new hire|welcome|hire)\b", low):
            return self._plan_onboard(req)
        raise BrainUnsupported(
            "The offline scripted brain only knows onboarding/offboarding. Set ANTHROPIC_API_KEY to use the Claude brain."
        )

    def _name(self, goal: str, verbs: str) -> str | None:
        m = re.search(rf"(?i:{verbs})\s+(?:(?i:new hire|employee)\s+)?([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){{1,2}})", goal)
        return m.group(1) if m else None

    def _plan_onboard(self, req: PlanRequest) -> Plan:
        goal, ans = req.goal, req.answers
        name = self._name(goal, "onboard|hire|welcome|add")
        sources = [c.ref for c in req.memory["docs"]][:4]
        if not name:
            return Plan(understanding="Onboard a new hire, but no name was found.",
                        missing_info=[MissingInfo(key="name", question="What is the new hire's full name?")])
        first, last = name.split()[0], name.split()[-1]
        email = f"{first}.{last}@acme.test".lower()
        # Read conventions from the retrieved SOP rather than hard-coding them.
        sop = " ".join(c.text for c in (req.search("work email format department must be one of access level") if req.search else req.memory["docs"]))
        depts = (re.search(r"one of ([^.]+)\.", sop) or [None, ""])[1]
        dept_list = [d.strip() for d in depts.split(",")] if depts else []
        dept = ans.get("department") or next((d for d in dept_list if d.lower() in goal.lower()), None)
        start = self._parse_date(goal) or self._parse_date(ans.get("start_date", ""))
        # Only an explicit access request counts; "into Finance" names a department, not a privilege.
        access = next((a for a in ("admin", "finance") if re.search(
            rf"\b{a}\s+(?:access|rights|privileges|permissions|role)\b|\baccess\s+(?:level\s+)?(?:of\s+|as\s+|to\s+)?{a}\b",
            goal.lower())), "standard")
        missing = []
        if not dept:
            missing.append(MissingInfo(key="department", question=f"Which department is {name} joining? ({', '.join(dept_list)})"))
        if not start:
            missing.append(MissingInfo(key="start_date", question=f"What is {name}'s start date (YYYY-MM-DD or a weekday)?"))
        if missing:
            return Plan(understanding=f"Onboard {name}; some required details are missing.", missing_info=missing, sources=sources)
        p = {"name": name, "first": first, "last": last, "email": email, "department": dept,
             "start_date": start.isoformat(), "access": access, "base": self.base}
        d = start
        steps = [
            Step(id="s1", kind="login", description="Sign in to AcmeHR with the ops service account", expected="Signed-in session", params=p),
            Step(id="s2", kind="dup_check", description=f"Search AcmeHR for an existing record for {email}", expected="No duplicate, or human guidance", params=p),
            Step(id="s3", kind="create_employee", description="Create the employee record in AcmeHR via the New employee form", expected="Employee appears as active", params=p),
            Step(id="s4", kind="roster", description="Add the employee to data/team_roster.csv", expected="Exactly one active roster row", params=p),
            Step(id="s5", kind="welcome_doc", description="Write the welcome document following the brand voice guide", expected="outputs/welcome_*.md exists", params=p),
            Step(id="s6", kind="send_email", description="Email the welcome document to the new hire (needs approval)", expected="Email in outbox", params=p),
        ]
        checks = [
            Check(id="c1", description="AcmeHR has the employee as active with the requested department, start date and access level",
                  type="http_json", params={"url": f"{self.base}/api/employees?email={email}",
                                            "expect": {"items.0.status": "active", "items.0.department": dept,
                                                       "items.0.start_date": start.isoformat(), "items.0.access_level": access}}),
            Check(id="c2", description="Roster has exactly one active row for the employee", type="csv_has_row",
                  params={"path": "data/team_roster.csv", "match": {"email": email, "department": dept, "status": "active"}, "count": 1}),
            Check(id="c3", description="Welcome document exists and states the start date in full", type="file_contains",
                  params={"path": f"outputs/welcome_{first.lower()}_{last.lower()}.md", "text": f"{d.day} {d.strftime('%B %Y')}"}),
            Check(id="c4", description="Welcome document follows the brand voice (warm, plain, addresses the new hire by first name)",
                  type="llm_judge", params={"question": "Is this welcome document warm, free of corporate jargon and addressed to the new hire by first name?",
                                            "path": f"outputs/welcome_{first.lower()}_{last.lower()}.md", "first_name": first}),
            Check(id="c5", description="Welcome email was delivered to the new hire", type="file_contains", depends_on_tool="email.send",
                  params={"glob": f"outbox/*{email.replace('@', '_at_')}*.json", "text": email}),
        ]
        return Plan(
            understanding=f"Onboard {name} into {dept}, starting {start:%A %d %B %Y}, with {access} access, following the Onboarding SOP.",
            assumptions=[f"Work email follows the SOP convention: {email}", f"Access level is {access}" + (" (default per SOP)" if access == "standard" else " (explicitly requested)")],
            steps=steps, acceptance=checks, sources=sources,
        )

    def _plan_offboard(self, req: PlanRequest) -> Plan:
        name = self._name(req.goal, "offboard|terminate|deactivate|off-board")
        sources = [c.ref for c in req.memory["docs"]][:4]
        if not name:
            return Plan(understanding="Offboard an employee, but no name was found.",
                        missing_info=[MissingInfo(key="name", question="Which employee should be offboarded?")])
        p = {"name": name, "base": self.base}
        steps = [
            Step(id="s1", kind="login", description="Sign in to AcmeHR with the ops service account", params=p),
            Step(id="s2", kind="find_employee", description=f"Find {name} in the AcmeHR directory", params=p),
            Step(id="s3", kind="deactivate", description="Deactivate the account in AcmeHR (needs approval)", params=p),
            Step(id="s4", kind="roster_inactive", description="Mark the roster row inactive", params=p),
        ]
        checks = [
            Check(id="c1", description=f"AcmeHR shows {name} as inactive", type="http_json", depends_on_tool="browser.click",
                  params={"url": f"{self.base}/api/employees?q={name.replace(' ', '%20')}", "expect": {"items.0.status": "inactive"}}),
            Check(id="c2", description=f"Roster shows {name} as inactive", type="csv_has_row", depends_on_tool="browser.click",
                  params={"path": "data/team_roster.csv", "match": {"name": name, "status": "inactive"}, "count": 1}),
        ]
        return Plan(understanding=f"Offboard {name}: deactivate their AcmeHR account and mark them inactive on the roster.",
                    assumptions=["Only the person named is affected"], steps=steps, acceptance=checks, sources=sources)

    @staticmethod
    def _parse_date(text: str) -> date | None:
        if m := re.search(r"\d{4}-\d{2}-\d{2}", text):
            try:
                return date.fromisoformat(m.group())
            except ValueError:
                return None
        low = text.lower()
        if "tomorrow" in low:
            return date.today() + timedelta(days=1)
        for i, wd in enumerate(WEEKDAYS):
            if wd in low:
                days = (i - date.today().weekday()) % 7 or 7
                return date.today() + timedelta(days=days)
        return None

    # -- execution --------------------------------------------------------
    def next_decision(self, ctx: StepContext) -> Decision:
        handler = HANDLERS.get(ctx.step.kind)
        if not handler:
            return Decision(kind="failed", summary=f"scripted brain has no procedure for step kind {ctx.step.kind!r}")
        gen = handler(_Ctx(ctx))
        try:
            nxt = next(gen)
            for item in ctx.history:  # replay recorded observations to find our place
                nxt = gen.send(item.observation)
        except StopIteration as stop:
            d: Done = stop.value
            return Decision(kind="done", summary=d.summary, facts=d.facts or {}, skipped=d.skipped)
        except Fail as f:
            return Decision(kind="failed", summary=str(f))
        if isinstance(nxt, Ask):
            return Decision(kind="ask", question=nxt.question)
        return Decision(kind="act", action=nxt)

    # -- verification & learning -----------------------------------------
    def judge(self, check: Check, evidence: str) -> tuple[bool, str]:
        first = check.params.get("first_name", "")
        bad = [w for w in JARGON if w in evidence.lower()]
        if not evidence.strip():
            return False, "no evidence to judge"
        if first and first not in evidence:
            return False, f"document never addresses {first}"
        if bad:
            return False, f"contains jargon: {', '.join(bad)}"
        return True, "addresses the new hire by name, plain language, no flagged jargon (heuristic judge)"

    def reflect(self, state: RunState, trail: str) -> list[str]:
        lessons = []
        if "HTTP 500" in trail or "500 Internal Server Error" in trail:
            lessons.append("AcmeHR can return a transient 500 when saving an employee: check the directory, then reload the form and retry.")
        if "expired" in trail.lower() or ("/login" in trail and "ops-bot" in trail):
            lessons.append("AcmeHR sessions can expire mid-task: sign in again as ops-bot and repeat the interrupted action.")
        if "Add to directory" in trail:
            lessons.append("AcmeHR's employee form submit button is sometimes labelled 'Add to directory' instead of 'Create employee'.")
        if state.denied_tools:
            lessons.append(f"A human declined: {', '.join(sorted(set(state.denied_tools)))}. Expect to need approval for these.")
        return lessons
