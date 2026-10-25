from __future__ import annotations

from ..config import Settings
from .base import Brain, BrainUnsupported, PlanRequest, StepContext


def make_brain(settings: Settings) -> Brain:
    kind = settings.resolve_brain()
    if kind == "claude":
        from .claude import ClaudeBrain

        return ClaudeBrain(settings.model)
    from .scripted import ScriptedBrain

    return ScriptedBrain(settings.acmehr_url)


__all__ = ["Brain", "BrainUnsupported", "PlanRequest", "StepContext", "make_brain"]
