# CentrAlign: an autonomous company operator

CentrAlign turns a short company request ("Onboard Priya Sharma into Engineering, starting Monday") into
**completed, verified, audited work** across a browser, files, spreadsheets, email and APIs, and asks a human only
when policy or ambiguity requires it.

The idea in one line: **the model reasons; deterministic code decides what is allowed, what happened, and whether the
job is actually done.**

```
 request ─▶ Understand ─▶ Plan ─▶ Execute ─▶ Observe ─▶ Adapt ─▶ Verify ─▶ Report
              │            │         │  ▲        │                  │
              ▼            ▼         ▼  │        ▼                  ▼
        company memory   acceptance  policy   anomaly /      independent checks
        (SOPs, rules,    criteria    engine   recovery       against the real
         past runs)      up front    approve/ replan →       system of record
                                     deny     escalate
```

## What is different about it

| Idea | Why it matters |
|---|---|
| **Acceptance criteria are written at plan time and verified by separate code.** | "Done" is never the agent's say-so. The verifier re-reads the real end state (HR system API, files, CSV rows) with its own read-only access. A `LazyBrain` test that falsely claims success is caught. |
| **Company context is data.** SOPs, policies and learned rules are retrieved (BM25) and cited in the plan. | A 7-word request expands into the right 6-step procedure, the right email format, the right approvals. New workflow = new markdown file. |
| **Policy sits between the brain and every tool.** `policy.yaml` → `auto` / `approve` / `deny`. | Autonomy is bounded by company rules, not by prompt wording. Unknown risk fails safe to `approve`. |
| **Runs are durable.** State is saved after every tick; a run can pause for approval and resume in another process. | Realistic human-in-the-loop: approvals take hours, not milliseconds. |
| **Tamper-evident audit log.** Append-only table + SHA-256 hash chain per run. | Every proposal, policy decision, effect, approval and check is reconstructable. `centralign audit <run>` verifies. |
| **Layered recovery.** Tool retry with backoff → observation-driven adaptation → replan → escalate to a human with context. | Failures are information to react to, not exceptions. |
| **Learns from outcomes and feedback.** Declining an action with a reason becomes a standing rule; each run's lessons are stored as episodic memory and retrieved next time. | The operator gets better at *this* company. |
| **Secrets never reach the model.** `{{secret:NAME}}` placeholders are injected at execution time and redacted from all output. | Credentials stay out of prompts, logs and reports. |

## Quick start (Windows/macOS/Linux, Python 3.11+)

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env            # add ANTHROPIC_API_KEY for the Claude brain (optional)

python -m centralign run "Onboard Priya Sharma into Engineering, starting 2026-11-02" --yes
python -m centralign serve      # web UI at http://127.0.0.1:8000
python -m pytest                # 34 tests incl. real-browser scenarios
```

Two brains share one interface:

* **`claude`** (used automatically when `ANTHROPIC_API_KEY` is set): plans from company context, picks tools, adapts to
  anything. Default model `claude-opus-5-5` (`CENTRALIGN_MODEL` to change). Handles arbitrary tasks within the tool set.
* **`scripted`** (default without a key): a deterministic stand-in that only knows onboarding/offboarding but *still
  reads what tools return and adapts*. It exists so the whole runtime can be demonstrated and tested offline and in CI.
  It is not the product's intelligence, and says so ("set `ANTHROPIC_API_KEY`") for any other request.

> The Claude brain's request/response handling is covered by tests against a fake client; I could not exercise it
> against the live API in the build environment (no key), so treat its first live run as the first integration test.

## The demo company

`demo_company/` is a self-contained company: **AcmeHR** (a plain web app the agent must drive through the browser: login,
forms, directory), SOPs, a brand-voice guide, a roster CSV and a policy file. The task is real, multi-system and
checkable.

Try these (CLI or the UI's example chips):

| Request | What it demonstrates |
|---|---|
| `Onboard Priya Sharma into Engineering, starting 2026-11-02` | Full loop; SOP-derived steps; approval before the welcome email; independent verification |
| same, with `--chaos all` | HTTP 500 on save, session expiry mid-task, renamed submit button → checks directory before retrying (no duplicates), re-logs in, finds the button by purpose |
| `Onboard Rahul Verma into Sales, starting 2026-11-02` | Duplicate found → asks a human. `stop` aborts cleanly; `reuse` ends **completed_with_issues** because the verifier sees the old record is still inactive |
| `Onboard Dana Kim into Engineering with admin access…` | Privileged access always needs approval; declining it is respected and surfaced as an unmet requirement |
| `Onboard Dana Lee` | Missing department and start date → asks instead of guessing |
| `Offboard Sam Okafor` | Second workflow reusing the same tools; irreversible deactivation gated by approval; decline leaves the account untouched |

Outputs per run in `.centralign/runs/<id>/`: `report.md` (outcome, plan, acceptance table, every action with policy
decision, human involvement, problems handled, lessons), `report.json`, and `evidence/*.png` screenshots.

## Architecture

```
centralign/
  runtime.py     state machine: plan → execute → verify; pause/resume; replan; escalate
  brains/        Brain interface + ClaudeBrain (tool use) + ScriptedBrain (offline stand-in)
  policy.py      auto/approve/deny from the company's policy.yaml
  tools/         Tool = schema + risk tier + preview() + run();  fs, csv, email, http, knowledge, browser
  verifier.py    independent acceptance checks (file, csv, http_json, llm_judge)
  memory.py      BM25 knowledge index + episodic memory + feedback rules
  store.py       SQLite: runs, hash-chained audit log, interventions, episodes, feedback
  report.py      evidence report     web/  FastAPI + single-page UI     cli.py  command line
demo_company/    AcmeHR mock app (with failure injection), SOPs, policy.yaml, seed files
```

Design choices worth knowing:

* **Stateless brain, durable runtime.** The brain rebuilds its context from recorded history on each call. That is what
  makes pause/resume, restarts and auditing straightforward, and it lets the scripted brain replay deterministically.
* **One action at a time**, each observed before the next. No fire-and-forget plans.
* **Browser as structured text, not pixels.** `read_page` returns visible text plus numbered interactive elements;
  screenshots are captured as evidence after every action. A dedicated thread owns each run's Playwright session.
* **Connector = one class.** To add Slack, a ticketing API or a desktop-automation backend, implement `Tool`
  (`name`, `input_schema`, `risk`, `run`) and register it; policy, audit, retries, redaction and verification work unchanged.
* **A new workflow needs no code with the Claude brain:** drop an SOP into `knowledge/`, add rules to `policy.yaml`.

## Honest limitations

* Desktop-application control (computer use) is not implemented; the `Tool` interface is where it would plug in.
  Browser, files, CSV, email and HTTP are real.
* Resume-after-restart is exact at step boundaries and for non-browser actions. If the process dies *mid-form*, the
  browser reopens the last URL with cookies restored but unsaved form fields are lost (in-process pauses keep the page alive).
* Retrieval is lexical BM25: transparent and dependency-free, but the interface is the seam for embeddings.
* Email delivery is a file outbox; AcmeHR is a mock. Credentials in the demo are fake.
* The LLM judge is used only for qualities code can't test; it sees raw evidence, never the agent's narrative.
