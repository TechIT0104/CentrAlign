"""Tool abstraction: a connector is one class with a typed schema, a risk tier and a run() method.

Adding a new company workflow's integration means adding a file that registers a Tool; the
runtime, policy engine, audit log and verifier need no changes.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, secret
from ..models import Action, Observation

RISKS = ("read", "write", "external", "destructive")
_SECRET_RE = re.compile(r"\{\{secret:([A-Z0-9_]+)\}\}")


@dataclass
class ToolContext:
    settings: Settings
    run_id: str
    run_dir: Path
    memory: Any = None  # centralign.memory.Memory
    browsers: Any = None  # centralign.tools.browser.BrowserManager
    emit: Callable[..., None] = lambda *a, **k: None
    # (method, url) -> policy Verdict, consulted for every state-changing request a browser action sends
    request_gate: Callable[[str, str], Any] | None = None


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    risk: str = "read"

    def risk_for(self, args: dict[str, Any]) -> str:
        return self.risk

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Describe what the action would touch, for policy rules and approval prompts."""
        return {"text": json.dumps(args, default=str)[:300]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Observation:  # pragma: no cover
        raise NotImplementedError

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def fail(msg: str, *, retryable: bool = False, **data: Any) -> Observation:
    return Observation(ok=False, summary=msg, error=msg, retryable=retryable, data=data)


def resolve_secrets(value: Any) -> Any:
    """Replace {{secret:NAME}} placeholders just-in-time so the model never sees the real value."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            val = secret(m.group(1))
            if val is None:
                raise KeyError(f"secret {m.group(1)} is not configured")
            return val
        return _SECRET_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v) for v in value]
    return value


def redact(text: str) -> str:
    import os

    for k, v in os.environ.items():
        if k.startswith("SECRET_") and v and len(v) >= 4:
            text = text.replace(v, "[REDACTED]")
    return text


def safe_path(root: Path, rel: str) -> Path:
    """Resolve rel inside root; reject traversal outside the sandbox."""
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"path {rel!r} escapes the sandbox")
    return p


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        assert tool.risk in RISKS, tool.risk
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def execute(self, action: Action, ctx: ToolContext, retries: int = 0) -> tuple[Observation, int]:
        """Run a tool with secret injection, transient-failure retries and output redaction."""
        tool = self.get(action.tool)
        if tool is None:
            return fail(f"unknown tool {action.tool!r}; available: {', '.join(self.names())}"), 0
        attempts = 0
        while True:
            attempts += 1
            try:
                obs = tool.run(resolve_secrets(action.args), ctx)
            except KeyError as e:
                obs = fail(f"missing secret/argument: {e}")
            except PermissionError as e:
                obs = fail(f"permission denied: {e}")
            except Exception as e:  # tool bugs must never crash the run
                obs = fail(f"{type(e).__name__}: {e}")
            if obs.ok or not obs.retryable or attempts > retries:
                break
            ctx.emit("action.retry", tool=action.tool, attempt=attempts, error=obs.error)
            time.sleep(0.4 * attempts)
        safe = redact(obs.model_dump_json())
        return Observation.model_validate_json(safe), attempts - 1
