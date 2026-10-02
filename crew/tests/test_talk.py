"""Crew 2.3: talking to one agent (and the CEO) privately, the prompt writer, the team's working rules (names,
passing on the owner's word, no acknowledgements, sharing, each other's strengths), Crew's own quality scan, joint
head-to-head results, and opening Crew reliably from the icon (the launcher)."""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("CREW_HOME", tempfile.mkdtemp(prefix="crew-talk-home-"))

from crewlib import gitops, lessons, prompts, quality, tools  # noqa: E402
from crewlib.store import Store  # noqa: E402

KEY = "sk-ant-api03-Qw3rTy7UiOp9AsDfGhJkLzXcVbNm1234"


def team(prompt_writer: bool = False) -> Store:
    st = Store(Path(tempfile.mkdtemp(prefix="crew-talk-")) / "team.db")
    for name, role in (("ada", "lead"), ("boole", "member"), ("curie", "member")):
        st.upsert_seat(name, role=role, vendor="claude", model="claude-opus-5-5", account=name, status="idle")
    st.set("phase", "build")
    st.set("prompt_writer", prompt_writer)
    return st


# ------------------------------------------------------------------ private messages


class PrivateMessages(unittest.TestCase):
    def test_only_the_addressed_agent_sees_it(self):
        st = team()
        st.post("ada", "update", "Foundation merged.")
        mid = st.owner_message("How far are you?", "boole")
        self.assertEqual(st.messages_after(mid - 1)[0]["kind"], "direct")
        self.assertNotIn(mid, [m["id"] for m in st.unread("ada")])
        self.assertNotIn(mid, [m["id"] for m in st.unread("curie")])
        self.assertIn(mid, [m["id"] for m in st.unread("boole")])
        self.assertNotIn(mid, [m["id"] for m in st.recent_messages(50, public_only=True)])
        self.assertEqual([m["id"] for m in st.owner_asks("boole")], [mid])
        st.post("boole", "direct", "Halfway through task 2.", recipient="you")
        self.assertEqual(st.owner_asks("boole"), [])  # answered
        self.assertEqual([m["sender"] for m in st.conversation("boole")], ["you", "boole"])

    def test_drafts_wait_for_the_prompt_writer(self):
        st = team(prompt_writer=True)
        draft = st.owner_message("pls make the header bigger", "boole")
        ceo = st.owner_message("is the plan sound", "ceo")
        self.assertEqual([m["kind"] for m in st.messages_after(0)], ["draft", "draft"])
        self.assertEqual(st.unread("boole"), [])  # no agent ever sees a draft
        self.assertEqual({m["id"] for m in st.owner_pending()}, {draft, ceo})
        final = st.post("you", "direct", "Make the page header 32 px.", urgent=True, recipient="boole", ref=draft,
                        original="pls make the header bigger")
        st.mark_drafted(draft)
        self.assertEqual([m["id"] for m in st.owner_asks("boole")], [final])
        text = tools.owner_words(st.messages_after(final - 1)[0])
        self.assertIn("Make the page header 32 px.", text)
        self.assertIn("the owner's own words, for intent: \"pls make the header bigger\"", text)
        self.assertEqual([m["id"] for m in st.owner_pending()], [ceo])

    def test_the_agent_is_asked_to_answer_and_to_pass_it_on(self):
        st = team()
        st.owner_message("Tell the team: dark mode is the default.", "boole")
        ctx = tools.Ctx(store=st, seat="boole", role="member")
        text, _ = tools.call(ctx, "team_status", {})
        self.assertIn("THE OWNER ASKS YOU DIRECTLY", text)
        self.assertEqual(st.get("owner_q:boole"), st.owner_asks("boole")[0]["id"])
        self.assertIn("share_with_team", prompts.owner_direct(st.owner_asks("boole")))


# ------------------------------------------------------------------ the team's tools


class TeamTools(unittest.TestCase):
    def setUp(self):
        self.st = team()
        self.boole = tools.Ctx(store=self.st, seat="boole", role="member")

    def test_acknowledgements_are_refused(self):
        for text in ("Thanks!", "ok", "@ada agreed.", "Got it 👍"):
            msg, err = tools.call(self.boole, "team_chat_post", {"text": text, "kind": "update"})
            self.assertTrue(err, text)
            self.assertIn("silence means agreement", msg)
        _, err = tools.call(self.boole, "team_chat_post", {"text": "Thanks — the parser now handles blank lines.",
                                                           "kind": "update"})
        self.assertFalse(err)

    def test_a_question_reaches_someone(self):
        msg, err = tools.call(self.boole, "team_chat_post", {"text": "Which port does the API use?", "kind": "question"})
        self.assertFalse(err)
        self.assertIn("went to the lead (@ada)", msg)
        self.assertTrue(self.st.recent_messages(1)[0]["text"].endswith("@ada"))
        tools.call(self.boole, "team_chat_post", {"text": "@lead is the schema final?", "kind": "question"})
        self.assertEqual(self.st.recent_messages(1)[0]["text"], "@ada is the schema final?")

    def test_unknown_names_are_flagged_but_code_is_not_a_name(self):
        msg, _ = tools.call(self.boole, "team_chat_post", {"text": "@bob please use `@property` here @curie",
                                                           "kind": "update"})
        self.assertIn("there is no @bob on this team", msg)
        self.assertNotIn("@property", msg)

    def test_the_owners_instruction_is_passed_on(self):
        text, err = tools.call(self.boole, "team_reply_owner", {
            "text": "Understood — dark mode will be the default.",
            "share_with_team": "The owner wants dark mode as the default.", "mention": ["curie"]})
        self.assertFalse(err, text)
        mine = self.st.conversation("boole")
        self.assertEqual(mine[-1]["text"], "Understood — dark mode will be the default.")
        public = self.st.recent_messages(1, public_only=True)[0]
        self.assertEqual(public["text"], "From the owner (told to me directly): The owner wants dark mode as the "
                                         "default. @ada @curie")
        self.assertTrue(public["urgent"])
        _, err = tools.call(self.boole, "team_reply_owner", {"text": "ok", "share_with_team": "", "mention": ["bob"]})
        self.assertTrue(err)
        tool = tools.TOOLS_BY_NAME["team_reply_owner"]
        self.assertEqual(tool.schema["required"], ["text", "share_with_team"])  # a decision every time

    def test_sharing_a_note_and_a_file(self):
        wt = Path(tempfile.mkdtemp(prefix="crew-talk-wt-"))
        (wt / "docs").mkdir()
        (wt / "docs" / "api.md").write_text("GET /items returns a list.\n")
        self.st.update_seat("boole", worktree=str(wt))
        text, err = tools.call(self.boole, "team_share", {"title": "API contract", "path": "docs/api.md",
                                                          "text": "Build against this.", "for": ["curie", "lead"]})
        self.assertFalse(err, text)
        self.assertIn("@curie @ada notified", text)
        msg = self.st.recent_messages(1)[0]
        self.assertEqual(msg["kind"], "share")
        self.assertIn("team_shared id=1", msg["text"])
        curie = tools.Ctx(store=self.st, seat="curie", role="member")
        full, _ = tools.call(curie, "team_shared", {"id": 1})
        self.assertIn("GET /items returns a list.", full)
        self.assertIn("#1 API contract", tools.call(curie, "team_shared", {})[0])
        _, err = tools.call(self.boole, "team_share", {"title": "x", "path": "../outside.txt"})
        self.assertTrue(err)
        self.assertIn("API contract", tools.call(curie, "team_status", {})[0])

    def test_the_plan_needs_automated_checks(self):
        lead = tools.Ctx(store=self.st, seat="ada", role="lead")
        self.st.create_task("Parser", "Parse.", "ok", ["p.py"], [], size="S")
        text, err = tools.call(lead, "team_plan_ready", {"summary": "One task."})
        self.assertTrue(err)
        self.assertIn("security check", text)
        tools.call(lead, "team_set_checks", {"commands": ["python -m pytest -q"]})
        _, err = tools.call(lead, "team_plan_ready", {"summary": "One task."})
        self.assertFalse(err)


# ------------------------------------------------------------------ instructions


class Instructions(unittest.TestCase):
    def test_every_agent_knows_the_team_and_its_record(self):
        home = tempfile.mkdtemp(prefix="crew-talk-rec-")
        saved, os.environ["CREW_HOME"] = os.environ["CREW_HOME"], home
        try:
            for i in range(5):
                lessons.record_effort_outcome("docs", "S", "medium", 1, 5, 1000, project="p", tier="workhorse",
                                              model="claude-sonnet-5-5")
            seats = [{"name": "ada", "role": "lead", "vendor": "claude", "tier": "manager", "model": "claude-opus-5-5"},
                     {"name": "curie", "role": "member", "vendor": "claude", "tier": "workhorse",
                      "model": "claude-sonnet-5-5"}]
            roster = prompts.roster_text(seats)
            self.assertIn("- @curie: member, Sonnet 5.5 (Claude Code), workhorse", roster)
            self.assertIn("- @ada: lead, Opus 5.5 (Claude Code), manager", roster)
            self.assertIn("WORKHORSE ENGINEER (Sonnet 5.5)", prompts.member_system("curie", seats, "ada"))
            self.assertIn("The workhorse seats are curie.", prompts.lead_system("ada", seats))
            self.assertIn("Record: 100% of 5 pieces passed the first check; strong at documents (S)", roster)
            system = prompts.member_system("curie", seats, "ada")
            for duty in ("@name", "share_with_team", "team_share", "never suppress a needed correction", "fails without it"):
                self.assertIn(duty, system)
            self.assertIn("team_plan_ready refuses without them", prompts.lead_system("ada", seats))
        finally:
            os.environ["CREW_HOME"] = saved

    def test_rules_are_brought_up_to_date_only_when_unedited(self):
        home = Path(tempfile.mkdtemp(prefix="crew-talk-rules-"))
        saved, os.environ["CREW_HOME"] = os.environ["CREW_HOME"], str(home)
        try:
            old = subprocess.run(["git", "show", "f0df57b:crew/team_rules.md"], cwd=ROOT, capture_output=True,
                                 text=True).stdout
            if old:
                (home / "team_rules.md").write_text(old.replace("\n", "\r\n"), encoding="utf-8", newline="")
                self.assertIn("Name the person", prompts.team_rules())  # an unedited 2.2 copy is refreshed
            (home / "team_rules.md").write_text("# My rules\n1. Always use tabs.\n", encoding="utf-8")
            self.assertEqual(prompts.team_rules(), "# My rules\n1. Always use tabs.\n")  # the owner's edit stays
        finally:
            os.environ["CREW_HOME"] = saved


# ------------------------------------------------------------------ Crew's own scan


class Scan(unittest.TestCase):
    DIFF = f"""diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,0 +11,5 @@
+def find(cur, name):
+    cur.execute(f"SELECT * FROM users WHERE name = '{{name}}'")
+    key = "{KEY}"
+    token = "sk-ant-EXAMPLE-not-a-real-key"
+    requests.get(url, verify=False)
diff --git a/tests/test_db.py b/tests/test_db.py
--- /dev/null
+++ b/tests/test_db.py
@@ -0,0 +1,2 @@
+password = "correcthorse"
+value = eval("1+1")
"""

    def test_secrets_block_and_risky_lines_go_to_the_reviewer(self):
        found = quality.scan_text(self.DIFF, ["my-real-secret-value"])
        self.assertEqual([(f.where(), f.blocking) for f in found.findings],
                         [("app/db.py:12", False), ("app/db.py:13", True), ("app/db.py:15", False)])
        text = quality.blocking_text(found, "crew/int")
        self.assertIn("app/db.py:13: what looks like an Anthropic API key", text)
        self.assertNotIn(KEY, text)  # a secret is never repeated
        notes = quality.review_notes(found)
        self.assertIn("SQL built from a string", notes)
        self.assertIn("switches off certificate checks", notes)

    def test_the_owners_real_keys_are_recognised(self):
        diff = "+++ b/app/x.py\n@@ -0,0 +1 @@\n+TOKEN = 'my-real-secret-value'\n"
        found = quality.scan_text(diff, ["my-real-secret-value"])
        self.assertTrue(found.blocking)
        self.assertNotIn("my-real-secret-value", quality.blocking_text(found, "base"))

    def test_scan_of_a_real_change(self):
        repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-talk-scan-")))
        (repo / "a.py").write_text("x = 1\n")
        gitops.commit_all(repo, "base")
        base = gitops.head(repo)
        (repo / "a.py").write_text(f"x = 1\nKEY = '{KEY}'\n")
        gitops.commit_all(repo, "change")
        found = quality.scan(repo, base)
        self.assertEqual([f.where() for f in found.blocking], ["a.py:2"])


# ------------------------------------------------------------------ opening Crew (the launcher)


class _Ping(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"crew": True, "version": "9.9.9"}).encode()
        self.send_response(200 if self.path == "/api/ping" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Launcher(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ["CREW_HOME"]
        os.environ["CREW_HOME"] = tempfile.mkdtemp(prefix="crew-talk-launch-")
        from crewapp import launcher
        self.launcher = launcher

    def tearDown(self):
        os.environ["CREW_HOME"] = self.saved

    def serve(self) -> int:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def test_a_running_crew_is_found_even_behind_an_office_proxy(self):
        port = self.serve()
        saved = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        os.environ.update({"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                           "no_proxy": "", "NO_PROXY": ""})
        try:
            self.assertTrue(self.launcher.answers(port))
            self.assertEqual(self.launcher.find_running(port), port)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_the_icon_shows_the_running_crew_instead_of_starting_another(self):
        from crewapp import server
        port = self.serve()
        opened = []
        real = self.launcher.open_window
        self.launcher.open_window = lambda url: opened.append(url) or True
        try:
            self.assertEqual(server.main(port=port, open_window=True), 0)
        finally:
            self.launcher.open_window = real
        self.assertEqual(opened, [f"http://localhost:{port}"])

    def test_a_crew_that_stopped_answering_is_replaced(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-c", "import socket, sys, time\n"
                                 "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); "
                                 "time.sleep(120)", str(port), "crewlib", "app"])
        self.addCleanup(proc.kill)
        until = time.time() + 10
        while not self.launcher.listening(port) and time.time() < until:
            time.sleep(0.1)
        self.launcher.record(port, "running")
        state = json.loads(self.launcher.state_path().read_text())
        state["pid"] = proc.pid
        self.launcher.state_path().write_text(json.dumps(state))
        fast = self.launcher.wait_for
        self.launcher.wait_for = lambda p, seconds: False  # it never answers: no need to wait in a test
        try:
            self.assertEqual(self.launcher.stuck_server(port), proc.pid)
        finally:
            self.launcher.wait_for = fast
        self.launcher.stop_process(proc.pid, port)
        self.assertIsNotNone(proc.poll())

    def test_a_restart_waits_for_its_own_port(self):
        from crewapp import server
        port = free_port()
        blocker = socket.socket()  # holds the port without listening, like connections Windows keeps after a close
        blocker.bind(("127.0.0.1", port))
        threading.Timer(1.5, blocker.close).start()
        got = server._bind(port, patient=True)
        self.addCleanup(got.server_close)
        self.assertEqual(got.server_address[1], port)
        other = socket.socket()
        other.bind(("127.0.0.1", port + 1 if port < 65000 else port - 1))
        self.addCleanup(other.close)
        quick = server._bind(other.getsockname()[1], patient=False)  # from the icon: move on at once
        self.addCleanup(quick.server_close)
        self.assertNotEqual(quick.server_address[1], other.getsockname()[1])

    def test_the_icons_are_only_touched_on_windows(self):
        with mock.patch.object(self.launcher, "ROOT", Path(tempfile.gettempdir()) / "crew-test"):
            self.assertEqual(self.launcher.heal_shortcuts(), [])

    def test_the_microphone_is_allowed_for_crew_only(self):
        """The owner asked that Crew's window use the microphone without the browser asking each time: Edge's and
        Chrome's allow-lists name Crew's own addresses and nothing else, the owner's own entries stay, and switching
        it off takes Crew's out again."""
        launcher = self.launcher
        ours = launcher.crew_addresses(8765)
        self.assertIn("http://127.0.0.1:8765", ours)
        self.assertIn("http://localhost:8774", ours)
        self.assertNotIn("http://localhost:8775", ours)
        self.assertFalse(any("*" in url for url in ours))  # never another program on this computer
        self.assertIn("http://localhost:9000", launcher.crew_addresses(9000))  # Crew started on another port
        if os.name != "nt":
            self.assertEqual(launcher.allow_devices(), [])
        reg = _Registry()
        edge_mic = r"Software\Policies\Microsoft\Edge\AudioCaptureAllowedUrls"
        chrome_mic = r"Software\Policies\Google\Chrome\AudioCaptureAllowedUrls"
        reg.keys[edge_mic] = {"1": "https://meet.example.org", "3": "https://after-a-gap.example.org"}  # the owner's
        with mock.patch.dict(sys.modules, {"winreg": reg}), \
                mock.patch.object(launcher, "os", types.SimpleNamespace(name="nt")):
            self.assertEqual(len(launcher.allow_devices(True, 8765)), 4)  # microphone and paste, in Edge and Chrome
            listed = reg.keys[edge_mic]
            self.assertEqual(sorted(listed, key=int), [str(n) for n in range(1, len(listed) + 1)])  # 1, 2, 3 …
            self.assertEqual([listed["1"], listed["2"]], ["https://meet.example.org", "https://after-a-gap.example.org"])
            self.assertEqual(list(listed.values())[2:], ours)
            self.assertEqual(list(reg.keys[chrome_mic].values()), ours)
            self.assertEqual(launcher.allow_devices(True, 8765), [])  # already there: nothing is written again
            self.assertEqual(len(launcher.allow_devices(False, 8765)), 4)
            self.assertEqual(reg.keys[edge_mic], {"1": "https://meet.example.org",
                                                  "2": "https://after-a-gap.example.org"})
            self.assertNotIn(chrome_mic, reg.keys)  # nothing left in it: the list is gone
            self.assertEqual(launcher.allow_devices(False, 8765), [])
            self.assertNotIn(chrome_mic, reg.keys)  # and switching off never makes an empty one


class _Registry:
    """Just enough of Windows' winreg for the browsers' allow-lists: each key a path, holding named values."""
    HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, REG_SZ = "HKCU", 1, 2, 1

    def __init__(self):
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKeyEx(self, root, path, reserved=0, access=0):  # noqa: N802 — winreg's own names
        return _RegKey(self.keys.setdefault(path, {}))

    def OpenKeyEx(self, root, path, reserved=0, access=0):  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _RegKey(self.keys[path])

    def EnumValue(self, key, index):  # noqa: N802
        items = list(key.values.items())
        if index >= len(items):
            raise OSError("No more data is available")
        return items[index][0], items[index][1], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, data):  # noqa: N802
        key.values[name] = data

    def DeleteValue(self, key, name):  # noqa: N802
        del key.values[name]

    def DeleteKey(self, root, path):  # noqa: N802
        del self.keys[path]


class _RegKey:
    def __init__(self, values: dict[str, str]):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
