"""Browser connector (Playwright). Reads pages as structured text + numbered interactive elements.

Playwright's sync API is bound to the thread that created it, but a run is advanced from many
threads (CLI, web UI, resume after approval). Each run therefore owns a dedicated browser thread
and every call is marshalled onto it. Cookies are persisted per run so a paused run can resume
even after a process restart.
"""
from __future__ import annotations

import json
import queue
import re
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from ..models import Observation
from .base import Tool, ToolContext, fail

READ_JS = """() => {
  document.querySelectorAll('[data-op-id]').forEach(e => e.removeAttribute('data-op-id'));
  const els = [...document.querySelectorAll('a[href],button,input,select,textarea,[role=button]')];
  let n = 0; const out = [];
  for (const e of els) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || (!r.width && !r.height) || e.type === 'hidden') continue;
    n++; e.setAttribute('data-op-id', n);
    const label = (e.labels && e.labels[0] ? e.labels[0].innerText : (e.getAttribute('aria-label') || e.placeholder || '')).trim();
    const it = {id: n, tag: e.tagName.toLowerCase()};
    if (e.type && e.tagName !== 'BUTTON') it.type = e.type;
    if (e.name) it.name = e.name;
    if (label) it.label = label;
    const text = (e.tagName === 'A' || e.tagName === 'BUTTON' ? e.innerText : '').trim().slice(0, 80);
    if (text) it.text = text;
    if (e.tagName === 'INPUT' && e.type !== 'password' && e.value) it.value = e.value;
    if (e.tagName === 'A') it.href = e.getAttribute('href');
    if (e.tagName === 'SELECT') { it.options = [...e.options].map(o => o.value); it.value = e.value; }
    out.push(it);
  }
  return {url: location.href, title: document.title, text: document.body.innerText.slice(0, 2500), elements: out};
}"""

DESCRIBE_JS = """(e) => {
  const label = (e.labels && e.labels[0] ? e.labels[0].innerText : (e.getAttribute('aria-label') || '')).trim();
  return [e.tagName.toLowerCase(), label, e.name || '', (e.innerText || '').trim().slice(0, 80)].filter(Boolean).join(' | ');
}"""


class BrowserSession:
    def __init__(self, run_dir: Path, headless: bool):
        self.run_dir = run_dir
        self.evidence = run_dir / "evidence"
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.state_file = run_dir / "browser_state.json"
        self.shots = len(list(self.evidence.glob("*.png")))
        self.last_status: int | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._boot_error: Exception | None = None
        self._thread = threading.Thread(target=self._loop, args=(headless,), daemon=True, name="browser")
        self._thread.start()
        self._ready.wait(60)
        if self._boot_error:
            raise self._boot_error

    # -- worker thread -------------------------------------------------
    def _loop(self, headless: bool) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                kwargs: dict[str, Any] = {"viewport": {"width": 1100, "height": 800}}
                meta = self.run_dir / "browser_meta.json"
                if self.state_file.exists():
                    kwargs["storage_state"] = str(self.state_file)
                ctx = browser.new_context(**kwargs)
                ctx.set_default_timeout(6000)
                page = ctx.new_page()
                page.on(
                    "response",
                    lambda r: setattr(self, "last_status", r.status)
                    if r.request.resource_type == "document" and r.frame == page.main_frame
                    else None,
                )
                if meta.exists():
                    try:
                        page.goto(json.loads(meta.read_text())["url"], timeout=10000)
                    except Exception:
                        pass
                self.page, self.context, self.meta_file = page, ctx, meta
                self._ready.set()
                while True:
                    job = self._jobs.get()
                    if job is None:
                        break
                    fn, fut = job
                    try:
                        fut.set_result(fn(page))
                    except BaseException as e:  # propagate to caller thread
                        fut.set_exception(e)
                ctx.close()
                browser.close()
        except Exception as e:
            self._boot_error = e
            self._ready.set()

    def call(self, fn: Callable[[Any], Any], timeout: float = 45) -> Any:
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut.result(timeout)

    def close(self) -> None:
        self._jobs.put(None)

    # -- helpers executed on the worker thread ------------------------
    def snapshot_and_save(self, page, label: str) -> str:
        self.shots += 1
        rel = f"evidence/{self.shots:03d}-{re.sub(r'[^a-z]+', '-', label.lower()).strip('-')}.png"
        try:
            page.screenshot(path=str(self.run_dir / rel))
        except Exception:
            rel = ""
        try:
            self.context.storage_state(path=str(self.state_file))
            self.meta_file.write_text(json.dumps({"url": page.url}))
        except Exception:
            pass
        return rel


class BrowserManager:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def session(self, run_id: str, run_dir: Path) -> BrowserSession:
        with self._lock:
            if run_id not in self._sessions:
                self._sessions[run_id] = BrowserSession(run_dir, self.headless)
            return self._sessions[run_id]

    def close(self, run_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(run_id, None)
        if s:
            s.close()


def _locate(page, target: str):
    t = str(target).strip()
    sel = f'[data-op-id="{t}"]' if re.fullmatch(r"\d+", t) else t
    loc = page.locator(sel).first
    loc.wait_for(state="attached", timeout=4000)
    return loc


def _page_obs(sess: BrowserSession, page, label: str, summary: str) -> Observation:
    try:
        page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    shot = sess.snapshot_and_save(page, label)
    try:
        text = page.inner_text("body")[:900]
    except Exception:
        text = ""
    return Observation(
        ok=True,
        summary=summary,
        data={"url": page.url, "title": page.title(), "http_status": sess.last_status, "page_text": text},
        artifacts=[shot] if shot else [],
    )


class _BrowserTool(Tool):
    def _sess(self, ctx: ToolContext) -> BrowserSession:
        return ctx.browsers.session(ctx.run_id, ctx.run_dir)

    @staticmethod
    def _element_failure(target: str, e: Exception) -> Observation:
        if type(e).__name__ == "TimeoutError":
            return fail(f"element not found or not actionable: {target!r}. Call browser.read_page for fresh element ids.")
        return fail(f"{type(e).__name__}: {str(e)[:200]}")


class Goto(_BrowserTool):
    name = "browser.goto"
    description = "Navigate the browser to a URL. Returns the resulting URL, title, HTTP status and page text."
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    risk = "write"  # navigation can have side effects; host allow-list is enforced by policy

    def preview(self, args, ctx):
        return {"text": f"navigate to {args.get('url')}"}

    def run(self, args, ctx):
        sess = self._sess(ctx)

        def go(page):
            try:
                page.goto(args["url"], wait_until="load", timeout=15000)
            except Exception as e:
                if type(e).__name__ == "TimeoutError":
                    return fail("navigation timed out", retryable=True)
                return fail(f"navigation failed: {str(e)[:200]}", retryable="ERR_CONNECTION" in str(e))
            return _page_obs(sess, page, "goto", f"loaded {page.url}")

        return sess.call(go)


class ReadPage(_BrowserTool):
    name = "browser.read_page"
    description = (
        "Read the current page: visible text plus a numbered list of interactive elements. "
        "Use the element `id` as `target` for click/fill/select. Ids are refreshed on every call."
    )
    input_schema = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        sess = self._sess(ctx)

        def read(page):
            data = page.evaluate(READ_JS)
            data["http_status"] = sess.last_status
            return Observation(
                ok=True,
                summary=f"{data['title']!r} at {data['url']} with {len(data['elements'])} interactive elements",
                data=data,
            )

        return sess.call(read)


class Click(_BrowserTool):
    name = "browser.click"
    description = "Click an element by id from browser.read_page (or a CSS/text= selector)."
    input_schema = {"type": "object", "properties": {"target": {"type": ["string", "integer"]}}, "required": ["target"]}
    risk = "write"

    def preview(self, args, ctx):
        sess = self._sess(ctx)

        def d(page):
            try:
                return {"text": _locate(page, args["target"]).evaluate(DESCRIBE_JS)}
            except Exception:
                return {"text": f"click {args.get('target')}"}

        return sess.call(d)

    def run(self, args, ctx):
        sess = self._sess(ctx)

        def click(page):
            try:
                _locate(page, args["target"]).click(timeout=5000)
            except Exception as e:
                return self._element_failure(str(args["target"]), e)
            return _page_obs(sess, page, "click", f"clicked {args['target']}")

        return sess.call(click)


class Fill(_BrowserTool):
    name = "browser.fill"
    description = (
        "Type into a text/date/password input. Use {{secret:NAME}} for credentials; the value is injected "
        "at execution time and never shown to you."
    )
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": ["string", "integer"]}, "value": {"type": "string"}},
        "required": ["target", "value"],
    }
    risk = "write"

    def preview(self, args, ctx):
        sess = self._sess(ctx)

        def d(page):
            try:
                return {"text": f"{_locate(page, args['target']).evaluate(DESCRIBE_JS)} <- {args.get('value')}"}
            except Exception:
                return {"text": f"fill {args.get('target')}"}

        return sess.call(d)

    def run(self, args, ctx):
        sess = self._sess(ctx)

        def fill(page):
            try:
                _locate(page, args["target"]).fill(args["value"], timeout=5000)
            except Exception as e:
                return self._element_failure(str(args["target"]), e)
            return Observation(ok=True, summary=f"filled {args['target']}")

        return sess.call(fill)


class Select(_BrowserTool):
    name = "browser.select"
    description = "Choose an option (by value or label) in a <select> element."
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": ["string", "integer"]}, "value": {"type": "string"}},
        "required": ["target", "value"],
    }
    risk = "write"

    def preview(self, args, ctx):
        sess = self._sess(ctx)

        def d(page):
            try:
                return {"text": f"{_locate(page, args['target']).evaluate(DESCRIBE_JS)} <- {args.get('value')}"}
            except Exception:
                return {"text": f"select {args.get('value')} in element {args.get('target')}"}

        return sess.call(d)

    def run(self, args, ctx):
        sess = self._sess(ctx)

        def select(page):
            try:
                loc = _locate(page, args["target"])
                try:
                    loc.select_option(value=args["value"], timeout=3000)
                except Exception:
                    loc.select_option(label=args["value"], timeout=3000)
            except Exception as e:
                return self._element_failure(str(args["target"]), e)
            return Observation(ok=True, summary=f"selected {args['value']} in {args['target']}")

        return sess.call(select)


class Screenshot(_BrowserTool):
    name = "browser.screenshot"
    description = "Capture the current page as evidence."
    input_schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    def run(self, args, ctx):
        sess = self._sess(ctx)
        return sess.call(lambda page: _page_obs(sess, page, args.get("label", "evidence"), "screenshot saved"))


BROWSER_TOOLS = [Goto, ReadPage, Click, Fill, Select, Screenshot]
