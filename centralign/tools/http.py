"""Generic HTTP/API connector. GET is read-only; any other method is a write the policy can gate."""
from __future__ import annotations

import httpx

from ..models import Observation
from .base import Tool, fail


class HttpRequest(Tool):
    name = "http.request"
    description = "Call an HTTP API. Hosts are restricted by company policy."
    input_schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "url": {"type": "string"},
            "json": {"type": "object"},
            "headers": {"type": "object"},
        },
        "required": ["method", "url"],
    }
    risk = "write"

    def risk_for(self, args):
        m = str(args.get("method", "GET")).upper()
        return "read" if m == "GET" else "destructive" if m == "DELETE" else "write"

    def preview(self, args, ctx):
        return {"text": f"{args.get('method')} {args.get('url')}"}

    def run(self, args, ctx):
        try:
            r = httpx.request(
                args["method"].upper(), args["url"], json=args.get("json"), headers=args.get("headers"), timeout=10
            )
        except httpx.TimeoutException:
            return fail("request timed out", retryable=True)
        except httpx.HTTPError as e:
            return fail(f"network error: {e}", retryable=True)
        body = r.text[:3000]
        try:
            data = {"json": r.json()}
        except ValueError:
            data = {"text": body}
        ok = r.status_code < 400
        return Observation(
            ok=ok,
            summary=f"HTTP {r.status_code}",
            error=None if ok else f"HTTP {r.status_code}",
            retryable=r.status_code >= 500,
            data={"status": r.status_code, **data},
        )
