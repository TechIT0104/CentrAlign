# CentrAlign: an autonomous company operator

CentrAlign turns a short company request ("Onboard Priya Sharma into Engineering, starting Monday") into
**completed, verified, audited work** across a browser, files, spreadsheets, email and APIs. It asks a human
only when policy or ambiguity requires it, and it proves the outcome against the real systems.

The idea in one line: **the model reasons; deterministic code decides what is allowed, what happened, and
whether the job is actually done.**

```
 request ─▶ Understand ─▶ Plan ─▶ Execute ─▶ Observe ─▶ Adapt ─▶ Verify ─▶ Report
              │            │         │  ▲        │                  │
              ▼            ▼         ▼  │        ▼                  ▼
        company memory   criteria    policy   anomaly /      independent checks
        (SOPs, rules,    frozen,     gate on  recovery       against the real
         past runs)      append-only the real  replan →       system of record
                                     effects  escalate
```

## What is different about it

| Idea | How it works |
|---|---|
| **"Done" is not the agent's say-so.** | Acceptance criteria are written before the work, in a separate model call that never sees the plan, and they are **append-only**: a replan can add checks, never remove or weaken one. After execution, code re-reads the real end state (HR system API, files, CSV rows) with its own read-only access. A check that cannot run counts as a failure. |
| **Approval follows what an action does, not what a button says.** | Every state-changing request the browser sends is checked against policy, in the page before the form submits and again at the network layer. Rename "Deactivate employee" to "Disable account" and the approval still fires, and the reviewer sees the real request (`POST /employees/3/deactivate`). A blocked action keeps the page intact, so an approved action is simply repeated. |
| **Company context is data.** | SOPs, policies and learned rules are retrieved (BM25), cited in the plan, and drive the steps and approvals. A new workflow is a markdown file plus policy rules. |
| **Durable, exclusive runs.** | State is saved every tick. Only the holder of a run **lease** may advance it, so two processes cannot drive one run. A worker resumes runs whose worker died and starts scheduled tasks. |
| **Tamper-evident audit.** | Append-only table + SHA-256 hash chain per run + an **HMAC-signed anchor** (count, latest hash) kept outside the events table. See "What the audit log does and does not protect" below. |
| **Layered recovery.** | Tool retry → adapt to what was observed → replan → escalate to a human with context. A human's explicit "stop" is final and is never re-asked. After an irreversible action with a lost confirmation, it checks the record instead of retrying on a guess. |
| **Learns from outcomes.** | A declined action with a reason becomes a standing rule; each run's lessons are stored and retrieved next time. |
| **Secrets never reach the model.** | `{{secret:NAME}}` placeholders are injected at execution time and redacted from every observation, log and report. |
| **Reliability is measured.** | `centralign eval` runs tasks × failure conditions × repeats and scores each run against **ground-truth checks written by the task author**, reporting success rate, actions, human load, replans, tokens and cost. |

## Quick start

Python 3.11+. Use a virtual environment (installing globally can upgrade shared packages such as NumPy).

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

python -m centralign run "Onboard Priya Sharma into Engineering, starting 2026-11-02" --yes
python -m centralign serve          # web UI at http://127.0.0.1:8000
python -m pytest                    # 61 tests, including real-browser scenarios
```

No `.env` is needed for the demo (the fake demo password is defaulted). Copy `.env.example` to `.env` to add a key.

### Two brains, one interface

* **`claude`** (automatic when `ANTHROPIC_API_KEY` is set): plans from company context, picks tools, writes the
  independent acceptance criteria, adapts to anything the tools allow. Default model `claude-opus-5-5`
  (`CENTRALIGN_MODEL` to change).
* **`scripted`** (default without a key): a deterministic stand-in that only knows onboarding and offboarding. It
  still reads what tools return and adapts, which is what lets the whole runtime be demonstrated and tested offline and
  in CI. It is not the product's intelligence; for any other request it stops and says so.

### Status of the model-driven path (read this)

Everything above the brain (policy, approvals, leases, verification, audit, eval) runs against a real browser and
is covered by 61 tests. The **Claude brain is tested against a fake client** (wire format, tool mapping, history
pairing, refusals, usage and cost accounting) but **has not yet been run end to end against the live API**; I did not
have a key while building. Generalisation to unseen tasks is therefore designed and instrumented, not yet demonstrated.
Three commands produce that evidence:

```bash
export ANTHROPIC_API_KEY=...                       # or put it in .env
python -m centralign run "Change Leila Haddad's department to Design" --yes
python -m centralign eval --brain claude --repeat 3   # all tasks incl. the 3 unseen ones, with tokens and cost
```

The three `unseen-*` tasks (change a department, export a headcount CSV, email everyone starting this week) have no
code written for them: only an SOP each in `demo_company/knowledge/`. Their ground truth is checked independently.
Expect some to fail on a first run; the eval report says which check failed and why, and that is the input for
tuning prompts, context or tools.

## Evaluation harness

`demo_company/eval_tasks.yaml` defines tasks, failure conditions and ground-truth checks. Offline results with the
scripted brain ([docs/eval-offline.md](docs/eval-offline.md)):

| | |
|---|---|
| Runs | 14 (3 `requires: claude` tasks skipped, not faked) |
| Success (status allowed **and** all ground-truth checks pass) | **14 / 14** |
| Conditions | none, HTTP 500 on save, session expiry, renamed button, relabelled Deactivate button, form outage |
| Average per run | 18 actions, 1.2 human interventions, 0.2 replans |
| Safe stop on an unseen failure | New required form field → the scripted brain has no recovery, escalates, and **creates nothing** |

Two defects were found by this harness while building it (an approval that fired twice for one action, and a
deactivation whose lost confirmation page triggered a pointless replan), which is the point of having it. A run that
claims success without doing the work scores zero: a "lazy brain" test proves it.

## The demo company

`demo_company/` is a self-contained company: **AcmeHR** (a plain web app the agent must drive through the browser:
login, forms, directory, edit page), SOPs for onboarding, offboarding, changes, reports and reminders, a brand-voice
guide, a roster CSV and a policy file. Failure injection (`--chaos flaky,rename,expiry,slow,relabel,newfield,shuffle,weekday,outage`)
simulates the ways real internal tools misbehave.

| Request | What it demonstrates |
|---|---|
| `Onboard Priya Sharma into Engineering, starting 2026-11-02` | Full loop; SOP-derived steps; approval before the welcome email; independent verification |
| same, with `--chaos all` | HTTP 500 on save, session expiry, renamed button: checks the directory before retrying (no duplicates), re-logs in, finds the button by purpose |
| `Onboard Rahul Verma into Sales, …` | Duplicate → asks a human. `stop` ends cleanly; `reuse` ends **completed_with_issues** because the verifier sees the old record is still inactive |
| `Onboard Dana Kim … with admin access` | Privileged access needs approval; declining is respected and only that criterion is skipped |
| `Offboard Sam Okafor --chaos relabel` | Approval still required after the button is renamed (effect-based policy) |
| `Onboard Dana Lee` | Missing department and start date → asks instead of guessing |

Each run writes `.centralign/runs/<id>/report.md` (outcome, plan, acceptance table, every action with its policy
decision, human involvement, problems handled, lessons, tokens and cost), `report.json` and `evidence/*.png`.

## Running it in the background

```bash
python -m centralign schedule add "Onboard Priya Sharma into Engineering, starting 2026-11-02" --every 1w
python -m centralign worker            # starts due schedules, resumes runs whose worker died
python -m centralign runs              # runs and pending approvals
python -m centralign approve 12        # or: deny 12 "reason" / answer 13 "Design"
```

## Architecture

```
centralign/
  runtime.py     state machine: plan → execute → verify; leases; pause/resume; replan; escalate; worker
  brains/        Brain interface + ClaudeBrain (tool use, independent checks, usage) + ScriptedBrain (offline)
  policy.py      auto/approve/deny: tool-level rules AND request-level (effect) rules, from policy.yaml
  tools/         Tool = schema + risk tier + preview() + run();  fs, csv, email, http, knowledge, browser
  verifier.py    independent acceptance checks (file, csv, http_json, llm_judge)
  evals.py       tasks × conditions × repeats against ground truth        memory.py   BM25 + episodes + rules
  store.py       SQLite: runs + leases, hash-chained audit log, anchors, interventions, schedules, episodes
  report.py      evidence report     web/  FastAPI + single-page UI     cli.py  command line
demo_company/    AcmeHR mock app (failure injection), SOPs, policy.yaml, eval tasks, seed files
tests/           61 tests: units, scenarios with a real browser, ops (leases/audit), web API, Claude wire format, evals
```

Design choices worth knowing:

* **Stateless brain, durable runtime.** The brain rebuilds context from recorded history on each call, which makes
  pause/resume, restarts and auditing straightforward.
* **One action at a time**, each observed before the next.
* **Browser as structured text, not pixels.** `read_page` returns visible text plus numbered interactive elements;
  screenshots are captured as evidence after every action. A dedicated thread owns each run's Playwright session.
* **Connector = one class.** To add Slack, a ticketing API or a desktop-automation backend, implement `Tool` and
  register it; policy, audit, retries, redaction and verification work unchanged.
* **Failure of the checker is failure.** A check that errors, or refers to a file outside the sandbox, fails.

## Models, APIs and components used

* **Model:** Anthropic Claude via the official `anthropic` Python SDK. Default `claude-opus-5-5`; configurable. One
  tool per turn with `tool_choice: auto` (forced tool use is not available on current models), no sampling
  parameters, `output_config.effort` per call (`high` for planning and independent criteria, `medium` for actions).
* **Browser:** Playwright (Chromium). **Backend/UI:** FastAPI + Uvicorn, a single-file vanilla-JS UI (no build step).
* **Storage:** SQLite (WAL). **Retrieval:** a small BM25 implementation (`memory.py`), swappable for embeddings
  behind the same `Index` interface. **Config/policy:** YAML. **Tests:** pytest.

## Assumptions

* The company's systems of record expose a read-only way to verify state (the demo gives the HR app a read-only
  API key used only by the verifier). Where none exists, verification falls back to what a read-only browser session can see.
* Each SOP states its own completion criteria; the independent criteria call relies on them.
* A human reviewer is reachable for approvals, but not necessarily within seconds.
* Browsing and HTTP are limited to approved internal hosts; everything else is denied by policy.

## Security notes and honest limits

**What the audit log does and does not protect.** SQLite triggers block UPDATE/DELETE; each event's hash includes the
previous one; and the latest (count, hash) is HMAC-signed into `anchors/<run>.json` at the end of every advance. That
detects edited events, **and** deletion or rehashed rewriting of anything recorded before the latest anchor
(tests cover both). It does **not** stop someone who controls both the database and the signing key, and events
written after the last anchor are not covered until the next one. For production, take the key from a secret store
(`CENTRALIGN_AUDIT_KEY`) and ship anchors to an external append-only store.

**The web UI has no authentication.** Anyone who can reach the port can approve actions. Fine for a localhost demo;
in production, approvals must be bound to a signed-in reviewer's identity and role.

**Prompt injection.** Page and file text goes to the model, so a malicious page could try to steer it. The design
answer is that the model's *proposals* are not trusted: host allow-lists, approval rules on real effects, sandboxed
paths, redacted secrets and independent verification hold regardless of what the model is told. This is not a claim
that injection cannot waste a run.

**Other limits.**
* Desktop-application control is not implemented; the `Tool` interface is where it would plug in.
* The form gate re-submits with `form.submit()`, which omits the submitter button's own name/value.
* Resume after a process crash is exact at step boundaries and for non-browser actions. If the process dies *mid-form*,
  the browser reopens the last URL with cookies restored but unsaved fields are lost.
* Retrieval is lexical; email delivery is a file outbox; AcmeHR is a mock.

## What I would build next

1. Run the Claude brain across the eval with repeats, publish the table, and fix what it exposes (prompts, context, tools).
2. A desktop connector as a `Tool` (pywinauto for a native app, or a computer-use backend) to prove the abstraction
   beyond the browser.
3. Per-tenant configuration and knowledge, a browser pool with limits, and cost budgets per task.
4. Embedding retrieval behind `Index`, plus feedback review: rules a human can edit and retire.
5. Reviewer identity, roles and notification channels (Slack/email) for approvals; external anchoring of audit hashes.
