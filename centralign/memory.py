"""Company memory: semantic (SOPs/policies, BM25), episodic (past runs) and feedback rules.

Retrieval is deliberately dependency-free BM25 so it is deterministic and inspectable; the
`Index` interface is what you would swap for embeddings in production.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .store import Store

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from in is it of on or that the this to with will you your we our".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


@dataclass
class Chunk:
    source: str  # file name
    heading: str
    text: str
    score: float = 0.0

    @property
    def ref(self) -> str:
        return f"{self.source}#{self.heading}" if self.heading else self.source


class Index:
    """Small BM25 index over arbitrary text documents."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.toks = [tokenize(d) for d in docs]
        self.avg = (sum(len(t) for t in self.toks) / len(self.toks)) if self.toks else 0.0
        df: Counter[str] = Counter()
        for t in self.toks:
            df.update(set(t))
        n = len(self.toks)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.tf = [Counter(t) for t in self.toks]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, toks in zip(self.tf, self.toks):
            s = 0.0
            for w in q:
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(toks) / (self.avg or 1)))
            out.append(s)
        return out


class Knowledge:
    """Semantic memory: markdown docs split on headings."""

    def __init__(self, directory: Path):
        self.chunks: list[Chunk] = []
        for path in sorted(directory.glob("*.md")) if directory.exists() else []:
            self.chunks += self._split(path)
        self.index = Index([f"{c.heading} {c.text}" for c in self.chunks])

    @staticmethod
    def _split(path: Path) -> list[Chunk]:
        out, heading, buf = [], "", []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if buf and "".join(buf).strip():
                    out.append(Chunk(path.name, heading, "\n".join(buf).strip()))
                heading, buf = line.lstrip("# ").strip(), []
            else:
                buf.append(line)
        if buf and "".join(buf).strip():
            out.append(Chunk(path.name, heading, "\n".join(buf).strip()))
        return out

    def search(self, query: str, k: int = 4, min_score: float = 0.3) -> list[Chunk]:
        if not self.chunks:
            return []
        ranked = sorted(zip(self.index.scores(query), self.chunks), key=lambda x: -x[0])
        return [Chunk(c.source, c.heading, c.text, s) for s, c in ranked[:k] if s >= min_score]


class Memory:
    """Facade used by the planner/executor to assemble context for a goal or step."""

    def __init__(self, knowledge_dir: Path, store: Store):
        self.knowledge = Knowledge(knowledge_dir)
        self.store = store

    def recall(self, query: str, k_docs: int = 4, k_episodes: int = 2) -> dict:
        docs = self.knowledge.search(query, k=k_docs)
        eps = self.store.episodes()
        if eps:
            idx = Index([f"{e['goal']} {' '.join(e['lessons'])}" for e in eps])
            ranked = sorted(zip(idx.scores(query), eps), key=lambda x: -x[0])
            eps = [e for s, e in ranked[:k_episodes] if s > 0.3]
        rules = [f["text"] for f in self.store.feedback()][:10]
        return {"docs": docs, "episodes": eps, "rules": rules}

    @staticmethod
    def render(ctx: dict) -> str:
        parts = []
        if ctx["docs"]:
            parts.append("## Company knowledge\n" + "\n\n".join(f"[{c.ref}]\n{c.text}" for c in ctx["docs"]))
        if ctx["rules"]:
            parts.append("## Standing rules learned from human feedback\n" + "\n".join(f"- {r}" for r in ctx["rules"]))
        if ctx["episodes"]:
            lines = []
            for e in ctx["episodes"]:
                lessons = "; ".join(e["lessons"]) or "no notable lessons"
                lines.append(f"- Past task: {e['goal']} -> {e['outcome']}. Lessons: {lessons}")
            parts.append("## Relevant past runs\n" + "\n".join(lines))
        return "\n\n".join(parts) or "(no relevant company context found)"
