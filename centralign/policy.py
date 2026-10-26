"""Policy engine: decides auto / approve / deny for every proposed action.

Rules live in the company's policy.yaml (data, not code). First matching rule wins; otherwise the
tool's risk tier supplies the default. The executor can never bypass this; it sits between the
brain's proposal and the tool call.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .models import Action

VERDICTS = ("auto", "approve", "deny")


@dataclass
class Verdict:
    verdict: str  # auto | approve | deny
    rule: str
    reason: str


class Policy:
    def __init__(self, config: dict[str, Any]):
        self.defaults: dict[str, str] = config.get("default", {})
        self.rules: list[dict[str, Any]] = config.get("rules", [])
        # Effect-level rules: matched against the HTTP requests a browser action actually sends, so a
        # relabelled button cannot slip past a rule written against its text.
        self.request_rules: list[dict[str, Any]] = config.get("request_rules", [])
        self.request_default: str = config.get("request_default", "auto")
        for r in [*self.rules, *self.request_rules]:
            if r.get("action") not in VERDICTS:
                raise ValueError(f"policy rule {r.get('id')} has invalid action {r.get('action')!r}")

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def decide(self, action: Action, risk: str, preview: dict[str, Any] | None = None) -> Verdict:
        preview = preview or {}
        for rule in self.rules:
            if self._matches(rule.get("when", {}), action, preview):
                return Verdict(rule["action"], rule["id"], rule.get("reason", ""))
        default = self.defaults.get(risk, "approve")  # unknown risk => fail safe
        return Verdict(default, f"default:{risk}", f"default policy for '{risk}' actions")

    def decide_request(self, method: str, url: str) -> Verdict:
        host = urlparse(url).hostname or ""
        for rule in self.request_rules:
            w = rule.get("when", {})
            if "method" in w and not re.fullmatch(w["method"], method, re.I):
                continue
            if "url_match" in w and not re.search(w["url_match"], url):
                continue
            if "host_not_in" in w and host in w["host_not_in"]:
                continue
            return Verdict(rule["action"], rule["id"], rule.get("reason", ""))
        return Verdict(self.request_default, "default:request", "default policy for outgoing requests")

    @staticmethod
    def _matches(when: dict[str, Any], action: Action, preview: dict[str, Any]) -> bool:
        if "tool" in when and not fnmatch.fnmatch(action.tool, when["tool"]):
            return False
        for arg, pattern in (when.get("args_match") or {}).items():
            if not re.search(pattern, str(action.args.get(arg, ""))):
                return False
        if "preview_match" in when and not re.search(when["preview_match"], str(preview.get("text", ""))):
            return False
        if "host_not_in" in when:
            url = action.args.get("url", "")
            host = urlparse(url).hostname or ""
            if not url or host in when["host_not_in"]:
                return False
        return True
