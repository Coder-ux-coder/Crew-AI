"""Team tools over MCP (stdio, newline-delimited JSON-RPC 2.0).

Claude Code and Codex each launch one copy per agent. Identity comes from the
environment the orchestrator sets: CREW_DB, CREW_SEAT, CREW_ROLE, CREW_TASK.
Nothing but protocol messages is ever written to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from . import __version__
from .store import Store
from .tools import Ctx, call, tools_for

INSTRUCTIONS = (
    "Crew team tools. The team shares ONE group chat (no private messages), a task board with file leases, "
    "and an experience memory. Work first; talk briefly; decisions are binding; evidence beats opinion."
)


def _reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> None:
    db = os.environ.get("CREW_DB")
    if not db:
        sys.stderr.write("crew mcp: CREW_DB is not set\n")
        sys.exit(2)
    store = Store(db)
    ctx = Ctx.from_env(store)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        if not isinstance(msg, dict):  # e.g. a batch: not used by Claude Code or Codex; answered, never fatal
            _reply(None, error={"code": -32600, "message": "invalid request"})
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            if method == "initialize":
                _reply(msg_id, {
                    "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "crew", "version": __version__},
                    "instructions": INSTRUCTIONS,
                })
            elif method == "ping":
                _reply(msg_id, {})
            elif method == "tools/list":
                _reply(msg_id, {"tools": [t.spec() for t in tools_for(ctx.role)]})
            elif method == "tools/call":
                text, is_error = call(ctx, params.get("name", ""), params.get("arguments") or {})
                _reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
            elif msg_id is not None and not str(method or "").startswith("notifications/"):
                _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as exc:  # never let one bad call kill the agent's tool server
            sys.stderr.write(traceback.format_exc())
            if msg_id is not None:
                _reply(msg_id, {"content": [{"type": "text", "text": f"crew tool error: {exc}"}], "isError": True})


if __name__ == "__main__":
    serve()
