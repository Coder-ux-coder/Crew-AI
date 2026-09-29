"""Tests for the second-generation app: Claude Code features in conversations (thinking, steps, to-dos,
helpers, plan mode, slash commands, stop), automatic Claude Code updates, moving to another subscription at a
limit, usage, connections (API keys and MCP servers), workflows, team agents and estimates, one-click updates
that keep the owner's data, and the settings migration. Uses the scripted fakes in tests/fakes."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

try:  # `python -m unittest discover -s tests` imports the modules by their own names
    from test_app import ENV, FAKES, HOME, STATE, AppServer, until
except ImportError:
    from tests.test_app import ENV, FAKES, HOME, STATE, AppServer, until

from crewapp import settings as settings_mod  # noqa: E402
from crewapp import updater  # noqa: E402
from crewlib import claude_cli  # noqa: E402


def collect_turn(ev, timeout=60) -> tuple[list[tuple[str, dict]], dict]:
    """All events up to and including the next 'done'."""
    seen = []
    while True:
        name, data = ev.get(timeout=timeout)
        seen.append((name, data))
        if name == "done":
            return seen, data


def scenario(**values):
    os.environ["CREW_FAKE_SCENARIO"] = json.dumps({"word_delay": 0.005, **values})


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(ENV)
        cls.s = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def tearDown(self):
        os.environ["CREW_FAKE_SCENARIO"] = ENV["CREW_FAKE_SCENARIO"]

    def new_chat(self, **body):
        cid = self.s.api("POST", "/api/chats", body)["id"]
        return cid, self.s.events(f"/api/chats/{cid}/events")

    # ------------------------------------------------------------ Claude Code features

    def test_thinking_steps_todos_helpers_context_and_limits(self):
        s = self.s
        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Work through the steps on a to-do list"})
        seen, done = collect_turn(ev)
        names = [n for n, _ in seen]
        self.assertIn("thinking", names)
        thinking = [d for n, d in seen if n == "thinking" and d.get("tokens")]
        self.assertEqual(thinking[-1]["tokens"], 240)
        todos = [d["items"] for n, d in seen if n == "todos"]
        self.assertEqual([t["text"] for t in todos[-1]], ["Gather the numbers", "Write the summary", "Check the result"])
        self.assertTrue(all(t["status"] == "completed" for t in done["meta"]["todos"]))
        context = [d for n, d in seen if n == "context"][-1]
        self.assertEqual(context["window"], 1000000)
        self.assertGreater(context["used"], 1000)
        rate = [d for n, d in seen if n == "rate"][-1]
        self.assertEqual(rate["account"], "claude-1")
        self.assertGreater(rate["five"]["utilization"], 0)
        self.assertTrue(any(st["label"] == "Added to the to-do list" for st in done["meta"]["steps"]))
        self.assertEqual(done["meta"]["effort"], "auto")  # nobody chose: Claude decides

        s.api("POST", f"/api/chats/{cid}/send", {"text": "Please research this for me"})
        seen, done = collect_turn(ev)
        agents = [d for n, d in seen if n == "agent"]
        self.assertEqual(agents[0]["status"], "running")
        self.assertEqual(agents[0]["description"], "Research the topic")
        self.assertEqual(agents[-1]["status"], "done")
        self.assertEqual(agents[-1]["steps"], 2)
        self.assertEqual(done["meta"]["agents"][0]["status"], "done")
        # the helper's own searches are not shown as the assistant's steps
        self.assertEqual([st["tool"] for st in done["meta"]["steps"]], ["Task"])

        got = s.api("GET", f"/api/chats/{cid}")
        self.assertEqual(got["context"]["window"], 1000000)
        usage = s.api("GET", "/api/usage")
        mine = next(a for a in usage["accounts"] if a["name"] == "claude-1")
        self.assertGreater(mine["limits"]["five_util"], 0)
        self.assertGreater(mine["tokens"]["today"], 0)
        info = s.api("GET", "/api/claude")
        self.assertIn("xlsx", info["info"]["skills"])
        self.assertIn("compact", info["info"]["slash_commands"])

    def test_crews_own_todo_tool(self):
        s = self.s
        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Go through a checklist"})
        seen, done = collect_turn(ev)
        lists = [d["items"] for n, d in seen if n == "todos"]
        self.assertEqual([t["status"] for t in lists[0]], ["in_progress", "pending"])
        self.assertEqual([(t["text"], t["status"]) for t in done["meta"]["todos"]],
                         [("Read the brief", "completed"), ("Write the note", "completed")])
        self.assertEqual({st["label"] for st in done["meta"]["steps"]}, {"Updated the to-do list"})
        system = (HOME / "chats" / cid / ".crew" / "system.md").read_text()
        self.assertIn("todo_write", system)  # Crew's guide is always included, whatever the owner's instructions say

    def test_plan_mode_then_approval(self):
        s = self.s
        cid, ev = self.new_chat(mode="plan")
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["mode"], "plan")
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Make a hello page"})
        seen, done = collect_turn(ev)
        plans = [d["text"] for n, d in seen if n == "plan"]
        self.assertTrue(plans and plans[-1].startswith("# Plan"))
        self.assertIn("# Plan", done["meta"]["plan"])
        workspace = HOME / "chats" / cid
        self.assertFalse((workspace / "hello.html").exists())  # nothing changed before approval
        s.api("POST", f"/api/chats/{cid}/approve", {})
        seen, done = collect_turn(ev)
        self.assertEqual(done["meta"]["mode"], "auto")
        self.assertTrue((workspace / "hello.html").is_file())
        self.assertEqual([f["name"] for f in done["meta"]["files"]], ["hello.html"])
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["mode"], "auto")
        # switching modes without sending
        s.api("POST", f"/api/chats/{cid}/mode", {"mode": "plan"})
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["mode"], "plan")
        s.api("POST", f"/api/chats/{cid}/mode", {"mode": "sideways"}, expect=400)

    def test_slash_commands(self):
        s = self.s
        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
        collect_turn(ev)
        s.api("POST", f"/api/chats/{cid}/send", {"text": "/context"})
        _, done = collect_turn(ev)
        self.assertEqual(done["meta"]["command"], "context")
        self.assertIn("Context Usage", done["text"])
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["title"], "hello")  # a command does not rename
        s.api("POST", f"/api/chats/{cid}/send", {"text": "/compact"})
        seen, done = collect_turn(ev)
        self.assertIn("compacted", [n for n, _ in seen])
        self.assertTrue(done["text"].startswith("Compacted the conversation:"))
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["context"]["used"], 1500)

    def test_stop_keeps_what_was_said(self):
        s = self.s
        scenario(word_delay=0.02)
        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Say something slow"})
        while ev.get(timeout=30)[0] != "delta":
            pass
        s.api("POST", f"/api/chats/{cid}/stop", {})
        _, done = collect_turn(ev)
        self.assertTrue(done["text"].endswith("*Stopped.*"))
        self.assertTrue(done["text"].startswith("word"))
        self.assertFalse(done["meta"]["error"])
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello again"})  # the conversation carries on
        _, done = collect_turn(ev)
        self.assertIn("Summary", done["text"])

    def test_old_claude_code_is_updated_and_the_question_answered(self):
        s = self.s
        (STATE / "claude-version").write_text("2.1.268")
        scenario(assistant_outdated=True)
        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
        _, first = collect_turn(ev)
        self.assertIn("updating it now", first["text"])
        self.assertFalse(first["meta"]["error"])
        notices, second = [], None
        while second is None:
            name, data = ev.get(timeout=60)
            if name == "notice":
                notices.append(data["text"])
            if name == "done":
                second = data
        self.assertTrue(any("2.1.268 to 2.1.290" in n for n in notices), notices)
        self.assertIn("Summary", second["text"])
        self.assertEqual(claude_cli.state()["version"], "2.1.290")

    def test_a_subscription_at_its_limit_hands_over(self):
        s = self.s
        before = s.api("GET", "/api/settings")
        s.api("PUT", "/api/settings", {"accounts": before["accounts"] + [{"name": "claude-2", "vendor": "claude",
                                                                         "profile": ""}],
                                       "app": {"chat_account": "claude-1"}})
        try:
            scenario(assistant_limit=["default"])  # claude-1 uses Claude Code's default profile folder
            cid, ev = self.new_chat()
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
            seen, answer = collect_turn(ev)  # one answer: it carries on on claude-2
            notices = [data["text"] for name, data in seen if name == "notice"]
            self.assertIn("claude-1 has reached its usage limit. Continuing on claude-2 with the whole conversation.",
                          notices)
            self.assertEqual(answer["meta"]["account"], "claude-2")
            self.assertIn("Summary", answer["text"])
            limits = s.api("GET", "/api/usage")["accounts"]
            self.assertEqual(next(a for a in limits if a["name"] == "claude-1")["limits"]["status"], "rejected")
        finally:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"], "app": {"chat_account": ""}})

    def test_vendor_effort_names(self):
        s = self.s
        st = s.api("GET", "/api/settings")
        self.assertEqual(st["efforts"], ["auto", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual(st["codex_efforts"], ["auto", "low", "medium", "high", "xhigh", "max", "ultra"])  # GPT-6
        self.assertEqual((st["models"]["effort_ceo"], st["models"]["effort_work"]), ("max", "auto"))
        from crewlib.config import TeamSettings
        self.assertEqual(TeamSettings().max_hours, 0)  # no timer unless the owner sets one
        cid, ev = self.new_chat()
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "effort": "minimal"}, expect=400)
        self.assertIn("Unknown effort level for Claude", err["error"])
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "effort": "xhigh"})
        _, done = collect_turn(ev)
        self.assertEqual(done["meta"]["effort"], "xhigh")

    def test_chatgpt_chats_use_gpt6_names(self):
        """A ChatGPT chat runs GPT-6 Astra by default; a chat saved with the old "minimal" level runs at "low"."""
        s = self.s
        before = s.api("GET", "/api/settings")
        calls = STATE / "codex-calls.jsonl"
        calls.unlink(missing_ok=True)
        try:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"] + [{"name": "chatgpt-1", "vendor": "codex",
                                                                               "profile": ""}]})
            known = {m["id"]: m["engine"] for m in s.api("GET", "/api/settings")["known_models"]}
            self.assertEqual((known["gpt-6-astra"], known["claude-sonnet-5-5"]), ("codex", "claude"))
            self.assertNotIn("gpt-6-sol", known)  # the owner took GPT-6 Sol out of Crew
            cid, ev = self.new_chat(engine="codex", effort="minimal")
            self.assertEqual(s.api("GET", f"/api/chats/{cid}")["model"], "gpt-6-astra")
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
            _, done = collect_turn(ev)
            self.assertEqual((done["meta"]["engine"], done["meta"]["effort"]), ("codex", "low"))
            call = [json.loads(x) for x in calls.read_text().splitlines()][-1]
            self.assertEqual((call["model"], call["effort"]), ("gpt-6-astra", "low"))
            s.api("POST", f"/api/chats/{cid}/send", {"text": "think hard", "effort": "ultra"})
            _, done = collect_turn(ev)
            self.assertEqual([json.loads(x) for x in calls.read_text().splitlines()][-1]["effort"], "ultra")
            for banned in ("gpt-6-luna", "gpt-6-sol"):
                err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "model": banned}, expect=400)
                self.assertIn("banned", err["error"])
        finally:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"]})

    # ------------------------------------------------------------ connections

    def test_connections_keys_and_mcp_servers(self):
        s = self.s
        out = s.api("POST", "/api/connections/mcp", {"name": "hunter", "type": "http", "url": "https://api.hunter.io/mcp",
                                                     "headers": "Authorization: Bearer abcdefghijklmnop1234"})
        entry = next(m for m in out["mcp"] if m["name"] == "hunter")
        self.assertNotIn("abcdefghijklmnop1234", json.dumps(out))
        self.assertTrue(entry["headers"]["Authorization"].endswith("1234"))
        s.api("POST", "/api/connections/mcp", {"name": "bad name!", "type": "http", "url": "https://x"}, expect=400)
        s.api("POST", "/api/connections/mcp", {"name": "nourl", "type": "http", "url": "ftp://x"}, expect=400)
        s.api("PUT", "/api/secrets", {"name": "HUNTER_API_KEY", "value": "hk_live_0123456789abcdef"})
        conns = s.api("GET", "/api/connections")
        key = next(k for k in conns["keys"] if k["name"] == "HUNTER_API_KEY")
        self.assertIn("Hunter", key["label"])
        self.assertNotIn("0123456789abcdef", json.dumps(conns))

        cid, ev = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/send", {"text": "Which keys can you use?"})
        _, done = collect_turn(ev)
        self.assertIn("HUNTER_API_KEY", done["text"])
        mcp = json.loads((HOME / "chats" / cid / ".crew" / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(mcp["hunter"]["headers"]["Authorization"], "Bearer abcdefghijklmnop1234")
        self.assertIn("crew_devices", mcp)
        system = (HOME / "chats" / cid / ".crew" / "system.md").read_text()
        self.assertIn("HUNTER_API_KEY (Hunter.io", system)
        self.assertNotIn("hk_live", system)

        s.api("POST", "/api/connections/mcp/hunter/toggle", {"enabled": False})
        cid2, ev2 = self.new_chat()
        s.api("POST", f"/api/chats/{cid2}/send", {"text": "hello"})
        collect_turn(ev2)
        mcp = json.loads((HOME / "chats" / cid2 / ".crew" / "mcp.json").read_text())["mcpServers"]
        self.assertNotIn("hunter", mcp)
        s.api("DELETE", "/api/connections/mcp/hunter")
        self.assertEqual([m for m in s.api("GET", "/api/connections")["mcp"] if m["name"] == "hunter"], [])
        s.api("PUT", "/api/secrets", {"name": "HUNTER_API_KEY", "value": None})

    # ------------------------------------------------------------ workflows

    def test_workflows(self):
        s = self.s
        listing = s.api("GET", "/api/workflows")
        self.assertGreaterEqual(len(listing["templates"]), 4)
        s.api("POST", "/api/workflows", {"name": "", "prompt": "x"}, expect=400)
        w = s.api("POST", "/api/workflows", {"name": "Morning note", "prompt": "Give me a summary of today",
                                             "engine": "claude",
                                             "schedule": {"kind": "daily", "time": "07:30", "days": [0, 1, 2, 3, 4]}})
        self.assertEqual(w["when"], "Weekdays at 07:30")
        self.assertGreater(w["next_run"], time.time())
        run = s.api("POST", f"/api/workflows/{w['id']}/run", {})
        self.assertIn("chat", run)
        done = until(lambda: (lambda r: r if r and r[0]["status"] != "running" else None)(
            s.api("GET", f"/api/workflows/{w['id']}/runs")["runs"]), timeout=60)
        self.assertEqual(done[0]["status"], "done")
        self.assertIn("Summary", done[0]["summary"])
        chats = {c["id"]: c for c in s.api("GET", "/api/chats")["chats"]}
        self.assertEqual(chats[run["chat"]]["kind"], "workflow")

        # the clock starts a due workflow once and schedules the next time
        self.s.app.chats.db.x("UPDATE workflows SET next_run=? WHERE id=?", (time.time() - 5, w["id"]))
        self.assertEqual(self.s.app.workflows.tick(), [w["id"]])
        self.assertGreater(s.api("GET", "/api/workflows")["workflows"][0]["next_run"], time.time())
        until(lambda: not s.api("GET", "/api/workflows")["workflows"][0]["running"], timeout=60)
        self.assertEqual(self.s.app.workflows.tick(), [])
        off = s.api("PUT", f"/api/workflows/{w['id']}", {"enabled": False})
        self.assertIsNone(off["next_run"])
        self.assertTrue(s.api("DELETE", f"/api/workflows/{w['id']}")["ok"])

    # ------------------------------------------------------------ team agents and estimates

    def test_team_timer_agents_and_estimate(self):
        s = self.s
        rid = s.api("POST", "/api/runs", {"request": "Build a small feature pack with tests", "mode": "solo",
                                          "hours": 1.5})["id"]
        state = until(lambda: (lambda st: st if st.get("raw_phase") == "done" else None)(
            s.api("GET", f"/api/runs/{rid}")), timeout=240, step=1.5)
        self.assertEqual(state["timer"], 1.5)
        lead = next(a for a in state["agents"] if a["standing"])
        self.assertEqual(lead["product"], "Claude")
        self.assertEqual(lead["role"], "Lead")
        self.assertGreater(lead["tokens"], 0)
        ceo = [a for a in state["agents"] if a["role"] == "CEO"]
        self.assertTrue(ceo and ceo[0]["effort"] == "max")
        self.assertGreater(state["estimate"]["tokens_used"], 0)
        self.assertIn(state["tasks"][0]["effort"], ("low", "medium", "high", "xhigh", "max"))

    # ------------------------------------------------------------ choosing the subscription

    def test_choose_the_subscription_for_a_chat_and_switch_it(self):
        """A chat runs on the subscription the owner picks; switching mid-conversation carries it across."""
        s = self.s
        before = s.api("GET", "/api/settings")
        extra = [{"name": "claude-2", "vendor": "claude", "profile": ""}, {"name": "claude-3", "vendor": "claude", "profile": ""}]
        try:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"] + extra})
            cid, ev = self.new_chat(account="claude-2")
            self.assertEqual(s.api("GET", f"/api/chats/{cid}")["account"], "claude-2")
            s.api("POST", f"/api/chats/{cid}/send", {"text": "remember the word tulip"})
            _, done = collect_turn(ev)
            self.assertEqual(done["meta"]["account"], "claude-2")
            s.api("POST", f"/api/chats/{cid}/send", {"text": "which word?", "account": "claude-3"})
            seen, done = collect_turn(ev)
            self.assertEqual(done["meta"]["account"], "claude-3")
            self.assertFalse(done["meta"].get("error"), done)  # the conversation came along (no "not found")
            self.assertIn("Now using claude-3.", [d.get("text") for n, d in seen if n == "notice"])
            err = s.api("POST", f"/api/chats/{cid}/send", {"text": "x", "account": "nobody"}, expect=400)
            self.assertIn("no Claude subscription called nobody", err["error"])
            s.api("POST", f"/api/chats/{cid}/send", {"text": "back to automatic", "account": ""})
            collect_turn(ev)
            self.assertEqual(s.api("GET", f"/api/chats/{cid}")["account"], "")
        finally:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"]})

    def test_a_workflow_keeps_its_subscription(self):
        s = self.s
        before = s.api("GET", "/api/settings")
        try:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"] + [{"name": "claude-2", "vendor": "claude", "profile": ""}]})
            w = s.api("POST", "/api/workflows", {"name": "Pinned", "prompt": "Say hello briefly.", "engine": "claude",
                                                 "account": "claude-2", "schedule": {"kind": "manual"}})
            self.assertEqual(w["account"], "claude-2")
            err = s.api("POST", "/api/workflows", {"name": "Bad", "prompt": "Say hello briefly.", "engine": "codex",
                                                   "account": "claude-2", "schedule": {"kind": "manual"}}, expect=400)
            self.assertIn("no ChatGPT subscription called claude-2", err["error"])
            s.api("DELETE", f"/api/workflows/{w['id']}")
        finally:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"]})

    def test_a_project_uses_only_the_chosen_subscriptions(self):
        s = self.s
        before = s.api("GET", "/api/settings")
        first = before["accounts"][0]["name"]
        try:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"] + [{"name": "claude-2", "vendor": "claude", "profile": ""}]})
            rid = s.api("POST", "/api/runs", {"request": "Build a small feature pack with tests", "mode": "solo",
                                              "accounts": ["claude-2"]})["id"]
            state = until(lambda: (lambda st: st if st.get("raw_phase") == "done" else None)(
                s.api("GET", f"/api/runs/{rid}")), timeout=240, step=1.5)
            self.assertEqual(state["accounts_chosen"], ["claude-2"])
            used = {a["account"] for a in state["agents"] if a["account"]}
            self.assertEqual(used, {"claude-2"}, state["agents"])
            self.assertNotIn(first, used)
        finally:
            s.api("PUT", "/api/settings", {"accounts": before["accounts"]})

    def test_scorecard_endpoint(self):
        d = self.s.api("GET", "/api/scorecard")
        self.assertEqual(d["window_days"], 180)
        self.assertEqual((d["workhorse"], d["manager"]), ("claude-sonnet-5-5", "claude-opus-5-5"))
        for key in ("models", "matrix", "rules", "contests", "thresholds"):
            self.assertIn(key, d)


class HelperTrackingTests(unittest.TestCase):
    def test_sub_agents_are_reported(self):
        import queue

        from crewlib.agents import ClaudeSeat, ClaudeSetup
        from crewlib.util import Redactor
        tmp = Path(tempfile.mkdtemp())
        events: queue.Queue = queue.Queue()
        setup = ClaudeSetup(model="claude-opus-5-5", effort="auto", work_model="claude-opus-5-5",
                            permission_mode="bypassPermissions", run_dir=tmp, extra_env={})
        seat = ClaudeSeat("builder-1", "member", None, tmp, setup, "system", events, Redactor({}))
        seat._handle({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Task", "input": {"description": "Check the docs",
                                                                         "subagent_type": "Explore"}}]}})
        seat._handle({"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "x"}}]}})
        seat._handle({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
        helper = []
        while not events.empty():
            ev = events.get()
            if ev.kind == "helper":
                helper.append((ev.data["state"], ev.data.get("what")))
        self.assertEqual(helper, [("start", "Check the docs"), ("step", "Grep"), ("done", None)])


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crew-install-"))
        (self.root / "crewlib").mkdir()
        (self.root / "crewlib" / "cli.py").write_text("OLD")
        (self.root / "VERSION.json").write_text(json.dumps({"version": "2.0.0"}))
        (self.root / "requirements-app.txt").write_text("playwright\n")
        self.saved = (updater.ROOT, updater._get)
        updater.ROOT = self.root
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("claude-branch/README.md", "outside crew")
            zf.writestr("claude-branch/crew/crewlib/cli.py", "NEW")
            zf.writestr("claude-branch/crew/crewapp/extra.py", "EXTRA")
            zf.writestr("claude-branch/crew/tests/test_x.py", "TEST")
            zf.writestr("claude-branch/crew/requirements-app.txt", "playwright\n")
            zf.writestr("claude-branch/crew/VERSION.json", json.dumps({"version": "2.1.0", "notes": ["Better"]}))
        self.zip = buf.getvalue()

        def fake_get(url, timeout=30):
            if url.split("?")[0].endswith("VERSION.json"):
                return json.dumps({"version": "2.1.0", "notes": ["Better"]}).encode()
            return self.zip
        updater._get = fake_get

    def tearDown(self):
        updater.ROOT, updater._get = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_check_and_install_keep_the_owners_data(self):
        secrets = HOME / "secrets.env"
        secrets.write_text("HUNTER_API_KEY=keep-me\n")
        info = updater.check()
        self.assertTrue(info["available"])
        self.assertEqual((info["current"], info["latest"], info["notes"]), ("2.0.0", "2.1.0", ["Better"]))
        self.assertEqual(updater.last_check()["latest"], "2.1.0")
        result = updater.install()
        self.assertEqual(result["version"], "2.1.0")
        self.assertEqual((self.root / "crewlib" / "cli.py").read_text(), "NEW")
        self.assertEqual((self.root / "crewapp" / "extra.py").read_text(), "EXTRA")
        self.assertFalse((self.root / "tests").exists())
        self.assertFalse((self.root / "README.md").exists())
        self.assertEqual(updater.current()["version"], "2.1.0")
        self.assertEqual(secrets.read_text(), "HUNTER_API_KEY=keep-me\n")
        self.assertFalse(updater.check()["available"])
        # the next version no longer ships crewapp/extra.py: it goes; the owner's files stay
        (self.root / "crewapp" / "mine.txt").write_text("keep")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("claude-branch/crew/crewlib/cli.py", "NEWER")
            zf.writestr("claude-branch/crew/VERSION.json", json.dumps({"version": "2.2.0"}))
        self.zip = buf.getvalue()
        updater.install()
        self.assertFalse((self.root / "crewapp" / "extra.py").exists())
        self.assertEqual((self.root / "crewapp" / "mine.txt").read_text(), "keep")
        self.assertEqual((self.root / "crewlib" / "cli.py").read_text(), "NEWER")
        secrets.unlink()

    def test_the_program_folder_is_what_updates_replace(self):
        real = Path(updater.__file__).resolve().parent.parent
        self.assertTrue((real / "VERSION.json").is_file())
        self.assertTrue((real / "crewlib" / "cli.py").is_file())
        version = json.loads((real / "VERSION.json").read_text())["version"]
        self.assertGreaterEqual(updater._parse(version), (2, 0, 0))


class ClaudeCodeVersionTests(unittest.TestCase):
    def test_required_version_and_update(self):
        self.assertEqual(claude_cli.required_version(
            "API Error: 400 Claude Code 2.1.268 does not support this model; version 2.1.280 or newer is required."),
            "2.1.280")
        self.assertIsNone(claude_cli.required_version("Some other error"))
        os.environ.update(ENV)  # other test modules in the same run may have pointed these elsewhere
        (STATE / "claude-version").write_text("2.1.270")
        ok, message = claude_cli.update()
        self.assertTrue(ok)
        self.assertIn("2.1.270 to 2.1.290", message)
        ok, message = claude_cli.update()
        self.assertIn("up to date", message)


class FrontendTests(unittest.TestCase):
    """The app is plain JavaScript modules: one missing export stops the whole app from loading."""

    def test_every_import_is_exported(self):
        import re
        static = Path(__file__).resolve().parent.parent / "crewapp" / "static"
        modules = [static / "app.js", *sorted((static / "js").rglob("*.js"))]
        exported: dict[Path, set[str]] = {}
        for m in modules:
            text = m.read_text(encoding="utf-8")
            names = set(re.findall(r"^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", text, re.M))
            for group in re.findall(r"^export\s*\{([^}]*)\}", text, re.M):
                names |= {x.split(" as ")[-1].strip() for x in group.split(",") if x.strip()}
            if re.search(r"^export\s+default", text, re.M):
                names.add("default")
            exported[m.resolve()] = names
        problems = []
        for m in modules:
            text = m.read_text(encoding="utf-8")
            for names, target in re.findall(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'", text):
                path = (m.parent / target).resolve()
                if not path.is_file():
                    problems.append(f"{m.name}: {target} does not exist")
                    continue
                for name in [x.split(" as ")[0].strip() for x in names.split(",") if x.strip()]:
                    if name not in exported.get(path, set()):
                        problems.append(f"{m.name}: '{name}' is not exported by {target}")
            for target in re.findall(r"import\s+\w+\s+from\s*'([^']+)'", text):
                if not (m.parent / target).resolve().is_file():
                    problems.append(f"{m.name}: {target} does not exist")
        self.assertEqual(problems, [])

    def test_icons_used_exist(self):
        import re
        static = Path(__file__).resolve().parent.parent / "crewapp" / "static"
        have = set(re.findall(r'<symbol id="i-([\w-]+)"', (static / "index.html").read_text(encoding="utf-8")))
        used = set()
        for m in [static / "app.js", *(static / "js").rglob("*.js")]:
            text = m.read_text(encoding="utf-8")
            used |= set(re.findall(r"\bicon\('([\w-]+)'", text))
            used |= set(re.findall(r"\bic:\s*'([\w-]+)'", text))
        self.assertEqual(sorted(used - have), [])


class SettingsMigrationTests(unittest.TestCase):
    def test_old_defaults_move_to_the_new_ones_and_choices_stay(self):
        home = Path(tempfile.mkdtemp(prefix="crew-migrate-"))
        saved = os.environ.get("CREW_HOME")
        os.environ["CREW_HOME"] = str(home)
        try:
            (home / "crew.toml").write_text(
                '[team]\nmax_hours = 3.0\n\n[models]\neffort_work = "high"\neffort_light = "medium"\n'
                'effort_ceo = "high"\n\n[app]\ntheme = "dark"\nchat_effort = "high"\n\n'
                '[[account]]\nname = "claude-1"\nvendor = "claude"\n')
            st = settings_mod.load()
            self.assertEqual(st["models"]["effort_work"], "auto")
            self.assertEqual(st["models"]["effort_ceo"], "max")
            self.assertEqual(st["team"]["max_hours"], 0)
            self.assertEqual(st["app"]["chat_effort"], "auto")
            self.assertEqual(st["app"]["theme"], "dark")  # the owner's own choices are kept
            self.assertEqual([a["name"] for a in st["accounts"]], ["claude-1"])
            again = settings_mod.load()
            self.assertEqual(again["app"]["settings_version"], 4)
            # a choice made after the upgrade is not "migrated" again
            settings_mod.save({"models": {"effort_work": "high"}})
            self.assertEqual(settings_mod.load()["models"]["effort_work"], "high")
        finally:
            os.environ["CREW_HOME"] = saved or ""
            shutil.rmtree(home, ignore_errors=True)

    def test_version_2_moves_to_the_three_tiers_and_keeps_later_choices(self):
        home = Path(tempfile.mkdtemp(prefix="crew-migrate-"))
        saved = os.environ.get("CREW_HOME")
        os.environ["CREW_HOME"] = str(home)
        try:
            # What Crew 2.0 wrote: its defaults, plus one choice the owner made after that upgrade.
            (home / "crew.toml").write_text(
                '[team]\nmax_hours = 3.0\n\n[models]\nwork = "claude-opus-5-5"\nceo = "claude-fable-5-1"\ncodex = ""\n'
                'effort_work = "high"\neffort_ceo = "max"\n\n[app]\ncodex_model = ""\ncodex_effort = "minimal"\n'
                'settings_version = 2\n\n[[account]]\nname = "claude-1"\nvendor = "claude"\n')
            st = settings_mod.load()
            m = st["models"]
            self.assertEqual((m["workhorse"], m["work"], m["ceo"], m["ceo_backup"]),
                             ("claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "claude-fable-5-1"))
            self.assertNotIn("codex", m)
            self.assertEqual((st["app"]["codex_model"], st["app"]["codex_effort"]), ("gpt-6-astra", "low"))
            self.assertEqual(m["effort_work"], "high")  # chosen after 2.0: the 2.0 step does not run again
            self.assertEqual(st["team"]["max_hours"], 3.0)
            self.assertEqual(st["team"]["workhorse_seats"], 2)
            self.assertEqual(st["app"]["settings_version"], 4)
            self.assertTrue((home / "crew.toml").read_text().count("gpt-6-astra"))
            settings_mod.save({"models": {"ceo": "claude-fable-5-1"}})  # the owner may still choose Fable
            self.assertEqual(settings_mod.load()["models"]["ceo"], "claude-fable-5-1")
        finally:
            os.environ["CREW_HOME"] = saved or ""
            shutil.rmtree(home, ignore_errors=True)

    def test_version_3_moves_the_workhorse_to_sonnet_and_keeps_everything_else(self):
        """What Crew 2.3.0 wrote for the owner (three Claude subscriptions, one ChatGPT): after the update the
        workhorse is Sonnet 5.5 on Claude, Sonnet is no longer banned, GPT-6 Sol is, and every other choice stays."""
        home = Path(tempfile.mkdtemp(prefix="crew-migrate-"))
        saved = os.environ.get("CREW_HOME")
        os.environ["CREW_HOME"] = str(home)
        try:
            written = settings_mod.dump({
                "team": {"mode": "team", "workhorse_seats": 2, "max_hours": 2.0},
                "models": {"work": "claude-opus-5-5", "ceo": "gpt-6-astra", "ceo_backup": "claude-fable-5-1",
                           "codex": "gpt-6-sol", "allowed": ["claude-opus-5-5", "claude-fable-5-1"],
                           "banned": ["haiku", "sonnet", "terra", "luna", "grok"], "effort_work": "auto",
                           "effort_light": "auto", "effort_ceo": "ultra"},
                "app": {"codex_model": "gpt-6-sol", "codex_effort": "high", "theme": "dark", "settings_version": 3},
                "account": [{"name": "claude-1", "vendor": "claude", "profile": "default"},
                            {"name": "ceo-pbit.gop.pk", "vendor": "claude"},
                            {"name": "zeeshandmg36-gmail.com", "vendor": "claude"},
                            {"name": "mohidzeeshanrana-gmail.com", "vendor": "codex"}]})
            (home / "crew.toml").write_text(written, encoding="utf-8")
            st = settings_mod.load()
            m = st["models"]
            self.assertEqual(m["workhorse"], "claude-sonnet-5-5")
            self.assertEqual(m["allowed"], ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"])
            self.assertEqual(m["banned"], ["haiku", "terra", "luna", "grok", "gpt-6-sol"])  # "grok": the owner's own
            self.assertEqual((m["ceo"], m["effort_ceo"]), ("gpt-6-astra", "ultra"))
            self.assertEqual((st["app"]["codex_model"], st["app"]["codex_effort"], st["app"]["theme"]),
                             ("gpt-6-astra", "high", "dark"))
            self.assertEqual((st["team"]["mode"], st["team"]["max_hours"]), ("team", 2.0))
            self.assertEqual([(s["account"], s["tier"]) for s in st["seats"]],
                             [("claude-1", "manager"), ("ceo-pbit.gop.pk", "manager"),
                              ("zeeshandmg36-gmail.com", "manager"), ("ceo-pbit.gop.pk", "workhorse"),
                              ("zeeshandmg36-gmail.com", "workhorse")])
            self.assertTrue((home / "crew.toml.bak").read_text(encoding="utf-8").count("gpt-6-sol"))  # as it was
            text = (home / "crew.toml").read_text(encoding="utf-8")
            self.assertNotIn("codex = ", text)
            self.assertIn('workhorse = "claude-sonnet-5-5"', text)
            with self.assertRaises(ValueError) as caught:  # banning the workhorse's own model is refused plainly
                settings_mod.save({"models": {"banned": ["haiku", "sonnet"]}})
            self.assertIn("banned", str(caught.exception))
            self.assertEqual(settings_mod.load()["models"]["banned"], ["haiku", "terra", "luna", "grok", "gpt-6-sol"])
            self.assertEqual(settings_mod.load()["app"]["settings_version"], 4)
        finally:
            os.environ["CREW_HOME"] = saved or ""
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
