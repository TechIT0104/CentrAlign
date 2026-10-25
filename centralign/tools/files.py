"""File-system, CSV, email-outbox and knowledge tools. All paths are sandboxed to the company drive."""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Any

from ..models import Observation
from .base import Tool, ToolContext, fail, safe_path


def _root(ctx: ToolContext):
    ctx.settings.files_dir.mkdir(parents=True, exist_ok=True)
    return ctx.settings.files_dir


class FsRead(Tool):
    name = "fs.read"
    description = "Read a text file from the company drive (path relative to the drive root)."
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def run(self, args, ctx):
        p = safe_path(_root(ctx), args["path"])
        if not p.is_file():
            return fail(f"no such file: {args['path']}")
        text = p.read_text(encoding="utf-8", errors="replace")
        return Observation(ok=True, summary=f"read {len(text)} chars from {args['path']}", data={"content": text[:6000]})


class FsList(Tool):
    name = "fs.list"
    description = "List files in a directory of the company drive."
    input_schema = {"type": "object", "properties": {"path": {"type": "string", "default": ""}}}

    def run(self, args, ctx):
        p = safe_path(_root(ctx), args.get("path", ""))
        if not p.is_dir():
            return fail(f"not a directory: {args.get('path', '')}")
        entries = sorted(f"{c.name}/" if c.is_dir() else c.name for c in p.iterdir())
        return Observation(ok=True, summary=f"{len(entries)} entries", data={"entries": entries})


class FsWrite(Tool):
    name = "fs.write"
    description = "Create or overwrite a text file on the company drive (e.g. under outputs/)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    risk = "write"

    def preview(self, args, ctx):
        return {"text": f"write {len(args.get('content', ''))} chars to {args.get('path')}"}

    def run(self, args, ctx):
        p = safe_path(_root(ctx), args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return Observation(ok=True, summary=f"wrote {args['path']} ({len(args['content'])} chars)")


class CsvRead(Tool):
    name = "csv.read"
    description = "Read a CSV file; optionally filter rows with `where` (column -> exact value)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "where": {"type": "object"}},
        "required": ["path"],
    }

    def run(self, args, ctx):
        p = safe_path(_root(ctx), args["path"])
        if not p.is_file():
            return fail(f"no such file: {args['path']}")
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        where = args.get("where") or {}
        rows = [r for r in rows if all(str(r.get(k, "")).lower() == str(v).lower() for k, v in where.items())]
        return Observation(ok=True, summary=f"{len(rows)} matching rows", data={"rows": rows[:100]})


class CsvUpsert(Tool):
    name = "csv.upsert_row"
    description = (
        "Insert or update a row in a CSV, matching an existing row on `key` (a column name). "
        "Unknown columns are rejected; omitted columns keep their old value / empty."
    )
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "key": {"type": "string"}, "row": {"type": "object"}},
        "required": ["path", "key", "row"],
    }
    risk = "write"

    def preview(self, args, ctx):
        return {"text": f"upsert {args.get('key')}={args.get('row', {}).get(args.get('key'))} in {args.get('path')}"}

    def run(self, args, ctx):
        p = safe_path(_root(ctx), args["path"])
        if not p.is_file():
            return fail(f"no such file: {args['path']}")
        with p.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header, rows = list(reader.fieldnames or []), list(reader)
        key, new = args["key"], {k: str(v) for k, v in args["row"].items()}
        unknown = [c for c in new if c not in header]
        if key not in header or key not in new or unknown:
            return fail(f"bad row: columns are {header}; key={key!r}; unknown columns={unknown}")
        for r in rows:
            if r.get(key, "").lower() == new[key].lower():
                r.update(new)
                action = "updated"
                break
        else:
            rows.append({c: new.get(c, "") for c in header})
            action = "inserted"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        p.write_text(buf.getvalue(), encoding="utf-8")
        return Observation(ok=True, summary=f"{action} row {key}={new[key]} in {args['path']}")


class EmailSend(Tool):
    name = "email.send"
    description = "Send an email (delivered to the company mail outbox). Irreversible; requires approval by policy."
    input_schema = {
        "type": "object",
        "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "subject", "body"],
    }
    risk = "external"

    def preview(self, args, ctx):
        return {"text": f"email to {args.get('to')}: {args.get('subject')}"}

    def run(self, args, ctx):
        out = _root(ctx) / "outbox"
        out.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{args['to'].replace('@', '_at_')}.json"
        (out / name).write_text(json.dumps({**args, "sent_at": time.time()}, indent=2), encoding="utf-8")
        return Observation(ok=True, summary=f"email sent to {args['to']}", data={"outbox_file": f"outbox/{name}"})


class KnowledgeSearch(Tool):
    name = "knowledge.search"
    description = "Search company SOPs, policies and system guides. Use before acting on an unfamiliar procedure."
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def run(self, args: dict[str, Any], ctx: ToolContext):
        hits = ctx.memory.knowledge.search(args["query"], k=4) if ctx.memory else []
        return Observation(
            ok=True,
            summary=f"{len(hits)} passages",
            data={"passages": [{"ref": h.ref, "text": h.text[:900]} for h in hits]},
        )
