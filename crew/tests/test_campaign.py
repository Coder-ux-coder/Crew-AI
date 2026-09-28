"""Regression tests from the debugging campaign (debug-campaign.md at the repository root).

Each test names the ledger entry it proves (B-nn): it failed before the fix and passes after it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CREW_HOME", tempfile.mkdtemp(prefix="crew-campaign-home-"))

from crewlib.store import Store  # noqa: E402


def _rpc_session(module: str, env: dict, messages: list[dict]) -> dict[int, dict]:
    """Run an MCP server module over stdio with the given messages; return the replies by id."""
    proc = subprocess.run([sys.executable, "-m", module], input="\n".join(json.dumps(m, ensure_ascii=False)
                                                                            for m in messages).encode("utf-8"),
                          capture_output=True, env=env, timeout=60, cwd=str(ROOT))
    replies = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.strip():
            msg = json.loads(line)
            replies[msg.get("id")] = msg
    return replies


class ToolServersSpeakUtf8(unittest.TestCase):
    """B-01: Codex starts the team's tool servers with only a few environment variables, so on Windows Python
    used the console code page (cp1252) for stdio: a team message with "→" or "✔" (symbols Crew itself writes)
    or any letter outside cp1252 broke every workhorse tool call."""

    def windows_like_env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0", PYTHONPATH=str(ROOT), **extra)
        return env

    def test_team_tools_read_and_write_utf8(self):
        d = Path(tempfile.mkdtemp(prefix="crew-utf8-"))
        st = Store(d / "team.db")
        st.upsert_seat("ada", role="lead", vendor="claude", account="claude-1", status="idle")
        st.upsert_seat("boole", role="member", vendor="codex", account="codex-1", status="idle")
        st.post("crew", "system", "Task #1 merged into the team's result. ✔")
        st.post("ada", "update", "@boole → the owner wrote: add the Łódź office to the chart")
        env = self.windows_like_env(CREW_DB=st.path, CREW_SEAT="boole", CREW_ROLE="member")
        replies = _rpc_session("crewlib.mcp_server", env, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "team_chat_read", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "team_chat_post", "arguments": {"text": "@ada Łódź added — done ✔", "kind": "answer"}}},
        ])
        read = replies[2]["result"]
        self.assertFalse(read["isError"], read)
        self.assertIn("✔", read["content"][0]["text"])
        self.assertIn("Łódź", read["content"][0]["text"])
        self.assertFalse(replies[3]["result"]["isError"], replies[3])
        self.assertEqual(st.recent_messages(1)[0]["text"], "@ada Łódź added — done ✔")

    def test_device_tools_pass_non_english_text_through_exactly(self):
        got: list[dict] = []
        app = _FakeApp(got)
        try:
            env = self.windows_like_env(CREW_APP_URL=app.url, CREW_APP_TOKEN="t")
            text = "Grüße aus Łódź → ✔"  # Ł is U+0141: its UTF-8 holds byte 0x81, which cp1252 cannot decode
            replies = _rpc_session("crewapp.devices_mcp", env, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "computer_type", "arguments": {"text": text}}},
            ])
        finally:
            app.close()
        self.assertIn(2, replies, "the device tool server died reading non-English text")
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        self.assertEqual(got, [{"text": text}])


class _FakeApp:
    """A stand-in for the Crew app's /internal endpoints: records what the agent's tools send."""

    def __init__(self, got: list):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                got.append(json.loads(body or b"{}"))
                data = b'{"result": {"ok": true}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    unittest.main()
