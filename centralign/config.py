"""Runtime configuration, resolved from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: ROOT / ".centralign")
    company_dir: Path = field(default_factory=lambda: ROOT / "demo_company")
    model: str = "claude-sonnet-5-5"
    brain: str = "auto"  # auto | claude | scripted
    acmehr_url: str = "http://127.0.0.1:8765"
    acmehr_api_key: str = "verifier-readonly-key"
    max_actions_per_step: int = 14
    max_replans: int = 2
    max_verify_rounds: int = 2
    max_tool_retries: int = 2
    headless: bool = True

    # derived locations -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "centralign.db"

    @property
    def files_dir(self) -> Path:
        """Sandbox the agent may read/write: the company's shared drive."""
        return self.home / "files"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def knowledge_dir(self) -> Path:
        return self.company_dir / "knowledge"

    @property
    def policy_path(self) -> Path:
        return self.company_dir / "policy.yaml"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.files_dir, self.runs_dir):
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, **overrides) -> "Settings":
        load_dotenv(ROOT / ".env")
        s = cls(
            model=os.getenv("CENTRALIGN_MODEL", cls.model),
            brain=os.getenv("CENTRALIGN_BRAIN", "auto"),
            acmehr_url=os.getenv("ACMEHR_URL", cls.acmehr_url),
            headless=os.getenv("CENTRALIGN_HEADED", "0") != "1",
        )
        if home := os.getenv("CENTRALIGN_HOME"):
            s.home = Path(home)
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def resolve_brain(self) -> str:
        if self.brain != "auto":
            return self.brain
        return "claude" if os.getenv("ANTHROPIC_API_KEY") else "scripted"


def secret(name: str) -> str | None:
    """Secrets are injected into tool arguments at execution time and never shown to the model."""
    return os.getenv(f"SECRET_{name}")
