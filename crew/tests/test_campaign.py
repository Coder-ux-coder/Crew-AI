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


class DeviceToolsIgnoreTheOfficeProxy(unittest.TestCase):
    """B-02: the agents' browser, computer and phone tools reached the app on this computer through the system
    web proxy; an office proxy cannot reach this computer's own programs, so every device tool failed."""

    def test_device_tool_reaches_the_app_with_a_proxy_configured(self):
        got: list[dict] = []
        app = _FakeApp(got)
        try:
            env = {k: v for k, v in os.environ.items() if k.lower() not in ("no_proxy", "http_proxy", "https_proxy")}
            env.update(HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9", PYTHONPATH=str(ROOT),
                       CREW_APP_URL=app.url, CREW_APP_TOKEN="t")
            replies = _rpc_session("crewapp.devices_mcp", env, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "browser_open", "arguments": {"url": "example.com"}}},
            ])
        finally:
            app.close()
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        self.assertEqual(got, [{"url": "example.com"}])


class ChatsNeverStayBusy(unittest.TestCase):
    """B-03: an unexpected error at the end of an answer (a file vanishing while Crew lists the files it made, the
    database busy, an odd line from Codex) left the chat "still answering" until Crew was restarted."""

    @classmethod
    def setUpClass(cls):
        from test_app import ENV, AppServer
        os.environ.update(ENV)
        cls.s = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def new_chat(self, **body):
        cid = self.s.api("POST", "/api/chats", body)["id"]
        return cid, self.s.events(f"/api/chats/{cid}/events")

    @staticmethod
    def done(ev, timeout=60):
        while True:
            name, data = ev.get(timeout=timeout)
            if name == "done":
                return data

    def test_claude_answer_ends_even_when_its_bookkeeping_fails(self):
        from crewapp import chat
        s = self.s
        cid, ev = self.new_chat()
        saved = chat.Session._context_event

        def locked(self):
            raise __import__("sqlite3").OperationalError("database is locked")
        chat.Session._context_event = locked
        try:
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
            first = self.done(ev)
        finally:
            chat.Session._context_event = saved
        self.assertIn("Summary", first["text"])  # the answer itself still reaches the owner
        self.assertFalse(s.api("GET", f"/api/chats/{cid}")["busy"])
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello again"})  # and the conversation carries on
        self.assertIn("Summary", self.done(ev)["text"])

    def test_a_file_vanishing_while_files_are_listed_is_harmless(self):
        from crewapp import chat
        s = self.s
        cid, ev = self.new_chat()
        real_stat = Path.stat
        seen = {"n": 0}

        def flaky(self, *a, **k):  # the file is there when first seen, and gone a moment later
            if self.name == "page.html":
                seen["n"] += 1
                if seen["n"] == 2:
                    raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_stat(self, *a, **k)
        Path.stat = flaky
        try:
            s.api("POST", f"/api/chats/{cid}/send", {"text": "Please make a page for me"})
            done = self.done(ev)
        finally:
            Path.stat = real_stat
        self.assertFalse(done["meta"].get("error"), done)
        self.assertFalse(s.api("GET", f"/api/chats/{cid}")["busy"])
        self.assertGreaterEqual(seen["n"], 1)  # the file was looked at (Crew now looks each file up only once)

    def test_chatgpt_answer_ends_even_when_reading_its_status_fails(self):
        from crewapp import chat
        s = self.s
        before = s.api("GET", "/api/settings")
        s.api("PUT", "/api/settings", {"accounts": before["accounts"] + [{"name": "chatgpt-1", "vendor": "codex",
                                                                           "profile": ""}]})
        saved = chat.read_codex_status
        chat.read_codex_status = lambda *a, **k: (_ for _ in ()).throw(ValueError("odd session file"))
        try:
            cid, ev = self.new_chat(engine="codex")
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
            first = self.done(ev)
            self.assertFalse(s.api("GET", f"/api/chats/{cid}")["busy"], first)
            chat.read_codex_status = saved
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hi again"})
            self.assertTrue(self.done(ev)["text"])
        finally:
            chat.read_codex_status = saved
            s.api("PUT", "/api/settings", {"accounts": before["accounts"]})


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
