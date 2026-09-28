"""App tests: the Crew app's server end to end — security, settings, skills, captures, the Assistant
(streaming, attachments, refusals), team projects, phone and computer control (simulated), the agent
device tools, static files, and the Markdown renderer. Uses the scripted fakes in tests/fakes."""

from __future__ import annotations

import glob
import http.client
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zlib
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="crew-app-home-"))
STATE = Path(tempfile.mkdtemp(prefix="crew-app-fake-"))
ENV = {
    "CREW_HOME": str(HOME), "CREW_CLAUDE_BIN": str(FAKES / "fake_claude"), "CREW_CODEX_BIN": str(FAKES / "fake_codex"),
    "CREW_ADB": str(FAKES / "fake_adb"), "CREW_FAKE_STATE": str(STATE), "CREW_FAKE_DESKTOP": "1",
    "CREW_FAKE_SCENARIO": json.dumps({"word_delay": 0.005}),
}
os.environ.update(ENV)

from crewapp import computer, server  # noqa: E402

PNG_1PX = computer.png(b"\x10\x80\x30", 1, 1)


class AppServer:
    """The real app server on a free port, in this process."""

    def __init__(self):
        self.httpd = server.CrewServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        self.app = server.App(self.port, False)
        server.Handler.app = self.app
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method: str, path: str, body=None, headers: dict | None = None, raw: bytes | None = None,
                host: str = "127.0.0.1"):
        conn = http.client.HTTPConnection(host, self.port, timeout=60)
        hdrs = dict(headers or {})
        data = None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def api(self, method: str, path: str, body=None, raw: bytes | None = None, expect: int = 200):
        headers = {"X-Crew": "1"} if method != "GET" else {}
        status, _, payload = self.request(method, path, body, headers, raw)
        data = json.loads(payload or b"{}")
        if status != expect:
            raise AssertionError(f"{method} {path} -> {status} {data}")
        return data

    def events(self, path: str) -> "queue.Queue[tuple[str, dict]]":
        """Collect a live stream's events in the background."""
        out: queue.Queue = queue.Queue()
        ready = threading.Event()

        def run():
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
            conn.request("GET", path)
            resp = conn.getresponse()
            ready.set()
            event = None
            while True:
                line = resp.fp.readline()
                if not line:
                    return
                line = line.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    out.put((event, json.loads(line[6:])))
                    event = None

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        time.sleep(0.2)
        return out

    def stop(self):
        self.app.chats.shutdown()
        self.app.set_phone_access(False)
        self.httpd.shutdown()
        self.httpd.server_close()


def until(fn, timeout=60, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(ENV)  # other test modules imported in the same run may have changed these
        cls.s = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    # ------------------------------------------------------------ security

    def test_security_rules(self):
        s = self.s
        status, _, _ = s.request("POST", "/api/chats", {})  # no app header: refused (no cross-site requests)
        self.assertEqual(status, 403)
        status, _, _ = s.request("POST", "/api/chats", {}, {"X-Crew": "1", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = s.request("POST", "/internal/browser/open", {"url": "x"})
        self.assertEqual(status, 403)
        status, _, _ = s.request("POST", "/internal/browser/open", {"url": "x"}, {"X-Crew-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_paired_phones_only(self):
        s = self.s
        ips = s.app.lan_ips()
        if not ips:
            self.skipTest("no network address on this machine")
        info = s.api("POST", "/api/phone-access", {"enabled": True})
        self.assertTrue(info["enabled"], info)
        ip = ips[0]
        try:
            status, _, _ = s.request("GET", "/api/overview", host=ip)
            self.assertEqual(status, 401)
            status, _, body = s.request("GET", "/", host=ip)
            self.assertIn(b"Pair this device", body)
            status, _, _ = s.request("GET", "/pair?t=wrong", host=ip)
            self.assertEqual(status, 200)  # the pairing help page, no cookie
            status, headers, _ = s.request("GET", "/pair?t=" + s.app.pair_token, host=ip)
            self.assertEqual(status, 302)
            cookie = headers["Set-Cookie"].split(";")[0]
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            status, _, _ = s.request("GET", "/api/overview", headers={"Cookie": cookie}, host=ip)
            self.assertEqual(status, 200)
            status, _, _ = s.request("GET", "/api/pair", headers={"Cookie": cookie}, host=ip)
            self.assertEqual(status, 403)  # the pairing code is only shown on the computer itself
            new = s.api("POST", "/api/phone-access/new-code", {})
            self.assertTrue(new["enabled"])
            status, _, _ = s.request("GET", "/api/overview", headers={"Cookie": cookie}, host=ip)
            self.assertEqual(status, 401)  # signed out
        finally:
            s.api("POST", "/api/phone-access", {"enabled": False})

    # ------------------------------------------------------------ settings, skills, captures

    def test_settings_round_trip_and_policy(self):
        s = self.s
        st = s.api("GET", "/api/settings")
        self.assertEqual(st["models"]["work"], "claude-opus-5-5")
        self.assertIn("haiku", st["models"]["banned"])
        out = s.api("PUT", "/api/settings", {"app": {"theme": "dark", "voice_rate": 1.2}, "team": {"max_hours": 2.5}})
        self.assertEqual(out["app"]["theme"], "dark")
        self.assertEqual(out["team"]["max_hours"], 2.5)
        err = s.api("PUT", "/api/settings", {"models": {"work": "claude-haiku-4-5"}}, expect=400)
        self.assertIn("banned", err["error"])
        err = s.api("PUT", "/api/settings", {"models": {"effort_work": "extreme"}}, expect=400)
        self.assertIn("effort", err["error"])
        self.assertEqual(s.api("GET", "/api/settings")["models"]["work"], "claude-opus-5-5")  # nothing half-saved
        accounts = st["accounts"] + [{"name": "claude-2", "vendor": "claude", "profile": ""}]
        self.assertEqual(len(s.api("PUT", "/api/settings", {"accounts": accounts})["accounts"]), 2)
        s.api("PUT", "/api/settings", {"accounts": st["accounts"]})
        keys = s.api("PUT", "/api/secrets", {"name": "WEATHER_API_KEY", "value": "abcd1234efgh5678"})["secrets"]
        self.assertEqual(keys, [{"name": "WEATHER_API_KEY", "hint": "••••••5678"}])
        self.assertNotIn("abcd1234", json.dumps(s.api("GET", "/api/settings")))
        s.api("PUT", "/api/secrets", {"name": "BAD NAME", "value": "x"}, expect=400)
        s.api("PUT", "/api/secrets", {"name": "WEATHER_API_KEY", "value": None})
        s.api("PUT", "/api/rules", {"text": "# Rules\n\n1. Be brief."})
        self.assertIn("Be brief", s.api("GET", "/api/settings")["rules"])

    def test_skills(self):
        s = self.s
        skills = s.api("GET", "/api/skills")["skills"]
        builtin = {x["id"]: x for x in skills}
        self.assertIn("visual-check", builtin)
        self.assertTrue(builtin["visual-check"]["summary"])
        made = s.api("POST", "/api/skills", {"name": "Formal letters", "when": "writing any official letter",
                                             "steps": "1. Letterhead. 2. Reference number. 3. Formal closing."})
        self.assertTrue(made["enabled"])
        active = HOME / "skills-active" / "skills"
        self.assertTrue((active / "formal-letters" / "SKILL.md").is_file())
        s.api("POST", "/api/skills/formal-letters/toggle", {"enabled": False})
        self.assertFalse((active / "formal-letters").exists())
        s.api("POST", "/api/skills", {"name": "Formal letters", "when": "again and again", "steps": "x" * 30}, expect=400)
        self.assertTrue(s.api("DELETE", "/api/skills/formal-letters")["ok"])

    def test_captures(self):
        s = self.s
        info = s.api("POST", "/api/captures?ext=png&label=My%20Shot", raw=PNG_1PX)
        self.assertTrue(info["name"].endswith("-my-shot.png"))
        status, headers, body = s.request("GET", info["url"])
        self.assertEqual((status, body), (200, PNG_1PX))
        self.assertIn(info["name"], [c["name"] for c in s.api("GET", "/api/captures")["captures"]])
        s.api("POST", "/api/captures?ext=exe", raw=b"MZ", expect=400)
        self.assertTrue(s.api("DELETE", "/api/captures/" + info["name"])["ok"])

    # ------------------------------------------------------------ assistant

    def test_assistant_streams_and_keeps_history(self):
        s = self.s
        chat = s.api("POST", "/api/chats", {})
        cid = chat["id"]
        ev = s.events(f"/api/chats/{cid}/events")
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Give me a summary please"})
        seen, text = [], ""
        while True:
            name, data = ev.get(timeout=60)
            seen.append(name)
            if name == "delta":
                text += data["text"]
            if name == "done":
                done = data
                break
        self.assertEqual(seen[0], "start")
        self.assertGreater(seen.count("delta"), 5)  # word by word
        self.assertIn("## Summary", done["text"])
        self.assertEqual(text.strip(), done["text"].strip())
        got = s.api("GET", f"/api/chats/{cid}")
        self.assertEqual([m["role"] for m in got["messages"]], ["user", "assistant"])
        self.assertEqual(got["title"], "Give me a summary please")

        # attachments reach the assistant as files in its folder
        att = s.api("POST", f"/api/chats/{cid}/upload?name=Budget%20Notes.pdf", raw=b"%PDF-1.4 test")
        self.assertEqual(att["path"], "attachments/Budget-Notes.pdf")
        s.api("POST", f"/api/chats/{cid}/send", {"text": "", "attachments": [att["path"], "../../etc/passwd"]})
        while True:
            name, data = ev.get(timeout=60)
            if name == "done":
                break
        self.assertIn("attachments/Budget-Notes.pdf", data["text"])
        self.assertNotIn("passwd", data["text"])

        # a file it makes is offered as a preview, served sandboxed
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Please make a page for me"})
        while True:
            name, data = ev.get(timeout=60)
            if name == "done":
                break
        files = data["meta"]["files"]
        self.assertEqual([f["name"] for f in files], ["page.html"])
        status, headers, _ = s.request("GET", files[0]["url"])
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))
        status, headers, _ = s.request("GET", att["url"])
        self.assertNotIn("Content-Security-Policy", headers)  # documents are not scripts
        status, _, _ = s.request("GET", f"/files/chat/{cid}/../../app.json")
        self.assertIn(status, (403, 404))

    def test_assistant_refusals(self):
        s = self.s
        cid = s.api("POST", "/api/chats", {})["id"]
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "model": "claude-sonnet-5"}, expect=400)
        self.assertIn("banned", err["error"])
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "   "}, expect=400)
        self.assertIn("Type or say", err["error"])
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["messages"], [])  # nothing recorded
        s.api("POST", f"/api/chats/{cid}/send", {"text": "first"})
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "second"}, expect=400)
        self.assertIn("Still answering", err["error"])
        until(lambda: not s.api("GET", f"/api/chats/{cid}")["busy"])
        self.assertTrue(s.api("DELETE", f"/api/chats/{cid}")["ok"])
        s.api("GET", f"/api/chats/{cid}", expect=404)

    # ------------------------------------------------------------ team project

    def test_team_project_from_the_app(self):
        s = self.s
        s.api("POST", "/api/runs", {"request": "x"}, expect=400)
        rid = s.api("POST", "/api/runs", {"request": "Build a small feature pack with tests", "mode": "solo"})["id"]
        state = until(lambda: (lambda st: st if st.get("raw_phase") == "done" else None)(
            s.api("GET", f"/api/runs/{rid}")), timeout=240, step=1.5)
        self.assertEqual(state["mode"], "solo")
        self.assertTrue(state["report"])
        self.assertEqual(state["progress"][0], state["progress"][1])
        self.assertTrue(any(m["who"] == "crew" for m in state["messages"]))
        self.assertTrue(s.api("POST", f"/api/runs/{rid}/say", {"text": "thank you"})["ok"])
        later = s.api("GET", f"/api/runs/{rid}?after={state['messages'][-1]['id']}")
        self.assertIn("thank you", [m["text"] for m in later["messages"] if m["who"] == "you"])
        self.assertIn(rid, [r["id"] for r in s.api("GET", "/api/runs")["runs"]])
        # talking to one agent: only a real agent (or the CEO) can be addressed
        lead = next(a["name"] for a in state["agents"] if a.get("standing"))
        err = s.api("POST", f"/api/runs/{rid}/say", {"text": "hello", "to": "nobody"}, expect=400)
        self.assertIn("no agent called nobody", err["error"])
        self.assertTrue(s.api("POST", f"/api/runs/{rid}/say", {"text": "How did it go?", "to": lead})["ok"])
        self.assertTrue(s.api("POST", f"/api/runs/{rid}/say", {"text": "Anything to improve?", "to": "CEO"})["ok"])
        mine = [m for m in s.api("GET", f"/api/runs/{rid}?after={later['messages'][-1]['id']}")["messages"]
                if m["who"] == "you"]
        self.assertEqual([(m["to"], m["kind"]) for m in mine], [(lead, "draft"), ("ceo", "draft")])  # the writer's turn
        self.assertTrue(s.api("POST", f"/api/runs/{rid}/interrupt", {"seat": lead})["ok"])
        self.assertFalse(s.api("POST", f"/api/runs/{rid}/interrupt", {"seat": "nobody"})["ok"])

    def test_ping_prompt_writer_and_update_state(self):
        s = self.s
        ping = s.api("GET", "/api/ping")
        self.assertEqual((ping["crew"], ping["port"]), (True, s.port))
        s.api("POST", "/api/improve", {"text": "  "}, expect=400)
        out = s.api("POST", "/api/improve", {"text": "pls make the button bigger and blue", "reader": "claude"})
        self.assertEqual(out, {"text": "Clarified: pls make the button bigger and blue", "changed": True})
        info = s.api("GET", "/api/update")
        self.assertIn("available", info)
        self.assertIn("auto", info)
        self.assertFalse(s.app.idle())  # the owner just used Crew: an automatic update waits for a quiet moment
        s.app.last_action -= 3600
        self.assertTrue(s.app.idle())

    # ------------------------------------------------------------ phone (simulated)

    def test_phone_connect_and_control(self):
        s = self.s
        self.assertFalse(s.api("GET", "/api/phone/status")["connected"])
        s.api("POST", "/api/phone/pair", {"address": "192.168.1.20:37123", "code": "12"}, expect=400)
        self.assertIn("paired", s.api("POST", "/api/phone/pair", {"address": "192.168.1.20:37123", "code": "123456"})["message"])
        s.api("POST", "/api/phone/connect", {"address": "192.168.1.20:41555"})
        st = s.api("GET", "/api/phone/status")
        self.assertTrue(st["connected"])
        self.assertEqual(st["device"], "SM S928B")
        s.api("POST", "/api/phone/tap", {"x": 0.5, "y": 0.25})
        s.api("POST", "/api/phone/key", {"key": "home"})
        s.api("POST", "/api/phone/open_app", {"name": "WhatsApp"})
        err = s.api("POST", "/api/phone/type", {"text": "Grüße"}, expect=400)
        self.assertIn("English", err["error"])
        elements = s.api("POST", "/api/phone/screen", {})["elements"]
        self.assertIn("WhatsApp", [e["text"] for e in elements])
        shot = s.api("POST", "/api/phone/screenshot", {})
        status, _, body = s.request("GET", shot["url"])
        self.assertTrue(body.startswith(b"\x89PNG"))
        log = json.loads((STATE / "phone.json").read_text())["log"]
        self.assertIn("input tap 540 585", log)
        self.assertIn("monkey -p com.whatsapp -c android.intent.category.LAUNCHER 1", log)

    # ------------------------------------------------------------ computer (simulated)

    def test_computer_control(self):
        s = self.s
        st = s.api("GET", "/api/computer/status")
        self.assertEqual((st["available"], st["width"], st["height"]), (True, 1920, 1080))
        desk = s.app.computer.desk()
        s.api("POST", "/api/computer/click", {"xr": 0.5, "yr": 0.5})
        self.assertEqual(desk.log[-1], "click left 960,540 x1")
        shot = s.app.computer.shot_for_assistant()  # the assistant sees 1280×720 and answers in its pixels
        self.assertIn("1920×1080", shot["text"])
        s.app.computer.act("click", {"x": 640, "y": 360, "button": "right"}, "assistant")
        self.assertEqual(desk.log[-1], "click right 960,540 x1")
        s.app.computer.act("type", {"text": "Grüße → Hello"}, "assistant")
        self.assertEqual(desk.log[-1], "type Grüße → Hello")
        s.app.computer.act("key", {"keys": "ctrl+shift+s"}, "assistant")
        self.assertEqual(desk.log[-1], "keys 0x11+0x10+0x53")
        s.api("POST", "/api/computer/key", {"keys": "ctrl+banana"}, expect=400)
        s.app.computer.act("click", {"x": 0, "y": 0}, "assistant")  # the assistant itself may use the corner
        s.app.computer.act("key", {"keys": "enter"}, "assistant")
        desk.pointer = (1, 1)
        s.app.computer.act("click", {"x": 300, "y": 300}, "assistant")
        desk.pointer = (0, 0)  # the owner pushes the pointer into the corner: the assistant must stop
        with self.assertRaises(computer.ComputerError):
            s.app.computer.act("click", {"x": 10, "y": 10}, "assistant")
        s.api("POST", "/api/computer/click", {"xr": 0.1, "yr": 0.1})  # the owner still can
        s.app.computer.paused_until = 0
        desk.pointer = (500, 500)
        data = s.api("POST", "/api/computer/screenshot", {})
        self.assertEqual(data["kind"], "image")

    def test_agent_device_tools(self):
        s = self.s
        env = {**os.environ, "CREW_APP_URL": f"http://127.0.0.1:{s.port}", "CREW_APP_TOKEN": s.app.internal_token,
               "PYTHONPATH": str(ROOT)}
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "computer_screenshot", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "computer_key", "arguments": {"keys": "win+r"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nothing_here", "arguments": {}}}]
        out = subprocess.run([sys.executable, "-m", "crewapp.devices_mcp"], input="\n".join(json.dumps(m) for m in msgs),
                             capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT)).stdout
        replies = {r["id"]: r for r in map(json.loads, out.splitlines())}
        names = [t["name"] for t in replies[2]["result"]["tools"]]
        for expected in ("browser_open", "phone_tap", "computer_click", "computer_type"):
            self.assertIn(expected, names)
        self.assertEqual(replies[3]["result"]["content"][0]["type"], "image")
        self.assertFalse(replies[4]["result"]["isError"])
        self.assertTrue(replies[5]["result"]["isError"])

    # ------------------------------------------------------------ static files

    def test_static_files(self):
        s = self.s
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/js/pages/chat.js", "text/javascript"),
                           ("/app.css", "text/css"), ("/manifest.webmanifest", "application/manifest+json"),
                           ("/icons/icon-192.png", "image/png"), ("/some/deep/link", "text/html")):
            status, headers, _ = s.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind), (path, headers["Content-Type"]))
        status, headers, body = s.request("HEAD", "/app.css")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(s.request("GET", "/sw.js")[1]["Cache-Control"], "no-cache")
        health = s.api("GET", "/api/health")
        self.assertIn("claude", [i["id"] for i in health["items"]])


WINDOWLESS = """
import runpy, sys
sys.stdout = None   # what pythonw.exe (the desktop icon) gives a program: no console at all
sys.stderr = None
sys.argv = ["crewlib"] + sys.argv[1:]
runpy.run_module("crewlib", run_name="__main__", alter_sys=True)
"""


class WindowlessStartTests(unittest.TestCase):
    """Started from the desktop icon there is no console: Crew must still start, and must never fail silently."""

    def launch(self, home: Path, *args: str) -> subprocess.Popen:
        env = {**os.environ, **ENV, "CREW_HOME": str(home), "PYTHONPATH": str(ROOT)}
        return subprocess.Popen([sys.executable, "-X", "utf8", "-c", WINDOWLESS, *args], cwd=str(ROOT), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_starts_without_a_console(self):
        home = Path(tempfile.mkdtemp(prefix="crew-windowless-"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = self.launch(home, "app", "--port", str(port), "--no-open")
        try:
            def healthy():
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", "/api/health")
                    return json.loads(conn.getresponse().read()).get("version")
                except (OSError, ValueError):
                    return None
            self.assertTrue(until(healthy, timeout=60, step=0.5))
            log = (home / "app.log").read_text(encoding="utf-8")
            self.assertIn("Crew starting: app", log)
            self.assertIn(f"Crew is running at http://localhost:{port}", log)
        finally:
            proc.terminate()
            proc.wait(10)

    def test_a_failure_is_written_down(self):
        home = Path(tempfile.mkdtemp(prefix="crew-windowless-"))
        proc = self.launch(home, "app", "--port", "not-a-number", "--no-open")
        self.assertNotEqual(proc.wait(60), 0)
        self.assertIn("invalid int value", (home / "app.log").read_text(encoding="utf-8"))
        proc = self.launch(home, "no-such-command")
        self.assertNotEqual(proc.wait(60), 0)


class PictureTests(unittest.TestCase):
    def test_png_encoder(self):
        rgb = bytes(range(48))  # 4×4 pixels
        data = computer.png(rgb, 4, 4)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), 4 * (1 + 12))
        self.assertEqual(raw[1:13], rgb[:12])

    def test_colour_order_and_fit(self):
        self.assertEqual(computer.bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 255]), 2, 1), bytes([3, 2, 1, 6, 5, 4]))
        self.assertEqual(computer.fit(1920, 1080), (1280, 720))
        self.assertEqual(computer.fit(1024, 768), (1024, 768))
        self.assertEqual(computer.parse_keys("Ctrl + Alt + Delete"), [0x11, 0x12, 0x2E])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class MarkdownTests(unittest.TestCase):
    def render(self, md: str) -> str:
        script = ("import { markdown } from './crewapp/static/js/ui.js';"
                  "process.stdout.write(markdown(process.argv[1]));")
        return subprocess.run(["node", "--input-type=module", "-e", script, md], capture_output=True, text=True,
                              cwd=str(ROOT), timeout=30).stdout

    def test_safe_and_rich(self):
        html = self.render("# Title\n\n**bold** and <script>alert(1)</script> [x](javascript:alert(1)) "
                           "[ok](https://example.com)\n\n- one\n- two\n  - nested\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode <b>\n```")
        self.assertIn("<h2>Title</h2>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('<a href="https://example.com" target="_blank" rel="noopener">ok</a>', html)
        self.assertIn("<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>", html)
        self.assertIn("<th>a</th>", html)
        self.assertIn("<pre><code>code &lt;b&gt;</code></pre>", html)


def _chromium() -> str | None:
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return os.environ.get("CREW_CHROMIUM") or (found[-1] if found else None)


@unittest.skipUnless(_chromium(), "no Chromium for the browser test")
class BrowserTests(unittest.TestCase):
    def test_shared_browser(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        os.environ.setdefault("CREW_CHROMIUM", _chromium())
        site = Path(tempfile.mkdtemp(prefix="crew-site-"))
        (site / "index.html").write_text("<title>Summit</title><label for=n>Name</label><input id=n><h1>Hello</h1>")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        web = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=site,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from crewapp import browser
        from crewapp.server import browser_action
        try:
            time.sleep(0.8)
            b = browser.service
            threads = [threading.Thread(target=b.start) for _ in range(3)]  # started at once from several places
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "crew-browser"), 1)
            self.assertEqual(b.navigate(f"http://127.0.0.1:{port}/")["title"], "Summit")
            browser_action(b, "fill", {"field": "Name", "text": "Zeeshan"}, "assistant")
            page = browser_action(b, "read", {}, "assistant")
            self.assertIn("Hello", page["text"])
            self.assertEqual(b.status()["driver"], "assistant")
            shot = browser_action(b, "screenshot", {}, "you")
            self.assertTrue(shot["name"].endswith(".png"))
            browser_action(b, "device", {"device": "phone"}, "you")
            self.assertEqual(b.status()["device"], "phone")
        finally:
            browser.service.stop()
            web.terminate()


if __name__ == "__main__":
    unittest.main()
