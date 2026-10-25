"""Thin helper so components emit audit events without knowing about the store."""
from __future__ import annotations

from typing import Any, Callable

from .store import Store


class Recorder:
    """Writes every decision and effect to the append-only audit log (and optional listeners)."""

    def __init__(self, store: Store, run_id: str):
        self.store, self.run_id = store, run_id
        self.listeners: list[Callable[[str, dict[str, Any]], None]] = []

    def __call__(self, type_: str, **data: Any) -> None:
        self.store.add_event(self.run_id, type_, data)
        for fn in self.listeners:
            fn(type_, data)
