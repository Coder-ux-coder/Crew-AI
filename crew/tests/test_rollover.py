"""One-to-one chats move on to the next subscription when one reaches its usage limit, and keep the whole
conversation (the owner's request, 2026-09-28).

The fakes remember each conversation in its session file, as Claude Code and Codex do, and answer "what is the code
word?" from what that conversation holds: so these tests see whether a conversation kept its memory when it moved.

    python -m unittest tests.test_rollover -v
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import unittest
from unittest import mock

try:  # `python -m unittest discover -s tests` imports the modules by their own names
    from test_app import ENV, HOME, STATE, AppServer
except ImportError:
    from tests.test_app import ENV, HOME, STATE, AppServer

from crewapp import chat as chat_mod  # noqa: E402
from crewlib import config as cfgmod, usage as usage_log  # noqa: E402

LIVE = STATE / "live-scenario.json"


def live(**values) -> None:
    """Change the fakes' scenario while a conversation's program keeps running."""
    LIVE.write_text(json.dumps(values), encoding="utf-8")


def prompts(kind: str = "assistant") -> list[dict]:
    """Every message the fake Claude ("assistant") or the fake ChatGPT ("codex") has received."""
    path = STATE / f"{kind}-prompts.jsonl"
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


class RolloverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(ENV)
        cls.s = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def setUp(self):
        before = self.s.api("GET", "/api/settings")
        self.addCleanup(self.s.api, "PUT", "/api/settings",
                        {"accounts": before["accounts"], "app": {"chat_account": before["app"].get("chat_account", "")}})
        self.addCleanup(LIVE.unlink, missing_ok=True)

    def subscriptions(self, vendor: str, *names: str) -> None:
        """The owner's subscriptions for this test (each in a profile folder of its own), all with room."""
        others = [a for a in self.s.api("GET", "/api/settings")["accounts"] if a["vendor"] != vendor]
        self.s.api("PUT", "/api/settings", {"accounts": others + [{"name": n, "vendor": vendor, "profile": ""}
                                                                   for n in names], "app": {"chat_account": ""}})
        for name in names:  # a subscription used by an earlier test starts with room again
            self.has_room(name)

    @staticmethod
    def has_room(name: str) -> None:
        usage_log.record_rate(name, {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1,
                                     "unifiedWindows": {"five_hour": {"utilization": 0.1,
                                                                      "resetsAt": int(time.time()) + 3600}}})

    def new_chat(self, **body):
        cid = self.s.api("POST", "/api/chats", body)["id"]
        self.addCleanup(self.s.api, "DELETE", f"/api/chats/{cid}")
        return cid, self.s.events(f"/api/chats/{cid}/events")

    def say(self, cid: str, ev: queue.Queue, text: str, timeout: float = 60, **body) -> tuple[dict, list]:
        """Send a message; return the answer (its last 'done': before, a move to another subscription ended the
        answer with a notice and began a second one) and every event before it."""
        self.s.api("POST", f"/api/chats/{cid}/send", {"text": text, **body})
        seen = []
        while True:
            name, data = ev.get(timeout=timeout)
            seen.append((name, data))
            if name == "done" and not data["meta"].get("notice"):
                return data, seen

    # ------------------------------------------------------------ Claude

    def test_every_subscription_at_its_limit_ends_plainly_not_in_a_loop(self):
        """Two Claude subscriptions, both at their limit: the chat was handed back and forth between them for ever,
        adding "…has reached its usage limit. Continuing on another subscription…" to the conversation each time."""
        self.subscriptions("claude", "claude-ra", "claude-rb")
        live(assistant_limit=["claude-ra", "claude-rb"])
        cid, ev = self.new_chat()
        start = len(prompts())
        self.s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
        answers, deadline = [], time.time() + 25
        while time.time() < deadline and len(answers) < 4 and len(prompts()) - start < 6:
            try:
                name, data = ev.get(timeout=1)
            except queue.Empty:
                continue
            if name == "done":
                answers.append(data)
                if not data["meta"].get("notice"):
                    break
        time.sleep(1.5)  # time for a further hand-over, had there been one
        tried = [p["account"] for p in prompts()[start:]]
        self.assertEqual(sorted(tried), ["claude-ra", "claude-rb"], tried)  # each asked once, no loop
        self.assertEqual(len(answers), 1, [a["text"] for a in answers])
        text = answers[0]["text"]
        self.assertIn("All your Claude subscriptions have reached their usage limits", text)
        self.assertIn("frees up", text)
        self.assertIn("nothing is lost", text.lower())
        messages = self.s.api("GET", f"/api/chats/{cid}")["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])

    def test_a_limit_midway_keeps_the_work_and_the_answer_carries_on(self):
        """The limit arrived in the middle of an answer: what was said so far vanished from the conversation, and
        the other subscription was simply given the owner's message again."""
        self.subscriptions("claude", "claude-rf", "claude-rg")
        live(assistant_limit_midway=["claude-rf"])
        cid, ev = self.new_chat(account="claude-rf")
        start = len(prompts())
        done, seen = self.say(cid, ev, "Write the report.")
        self.assertEqual(done["meta"]["account"], "claude-rg")
        self.assertFalse(done["meta"]["error"])
        self.assertIn("I have started on it", done["text"])  # the part written on claude-rf is kept …
        self.assertIn("Summary", done["text"])  # … and the answer carries on
        self.assertEqual(sum(1 for name, _ in seen if name == "done"), 1)  # one answer, not a notice and a retry
        notices = [d["text"] for name, d in seen if name == "notice"]
        self.assertTrue(any("claude-rf has reached its usage limit" in n and "claude-rg" in n for n in notices), notices)
        steps = done["meta"]["steps"]
        self.assertTrue(any(s["label"] == "Wrote draft.txt" or "draft.txt" in (s.get("detail") or "") for s in steps),
                        steps)
        self.assertTrue(any(s["kind"] == "account" and "claude-rg" in s["label"] for s in steps), steps)
        asked = prompts()[start:]
        self.assertEqual([p["account"] for p in asked], ["claude-rf", "claude-rg"])
        self.assertIn("Write the report.", asked[1]["text"])  # the owner's words go along
        self.assertIn("carry on from where you stopped", asked[1]["text"])
        self.assertEqual(asked[0]["session"], asked[1]["session"])  # the same conversation, moved
        messages = self.s.api("GET", f"/api/chats/{cid}")["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertIn("I have started on it", messages[-1]["text"])

    def test_a_chat_that_moves_back_keeps_what_was_said_meanwhile(self):
        """A chat moved from claude-rc to claude-rd at a limit; when claude-rc had room again, the chat went back to
        its old copy of the conversation there: everything said on claude-rd was forgotten."""
        self.subscriptions("claude", "claude-rc", "claude-rd")
        cid, ev = self.new_chat(account="claude-rc")  # the owner chose claude-rc for this chat
        done, _ = self.say(cid, ev, "The code word is MANGO.")
        self.assertEqual(done["meta"]["account"], "claude-rc")
        live(assistant_limit=["claude-rc"])
        done, _ = self.say(cid, ev, "The code word is now KIWI.")
        self.assertEqual(done["meta"]["account"], "claude-rd")
        done, _ = self.say(cid, ev, "The code word is now LEMON.")  # said only on claude-rd
        self.assertEqual(done["meta"]["account"], "claude-rd")
        live()  # claude-rc has room again
        self.has_room("claude-rc")
        done, _ = self.say(cid, ev, "What is the code word?")
        self.assertEqual(done["meta"]["account"], "claude-rc")
        self.assertIn("The code word is LEMON.", done["text"])

    def test_a_conversation_no_longer_on_disk_continues_from_crews_record(self):
        """Claude Code removes old conversations (after 30 days, by default). Resuming one failed on every message
        with "The assistant stopped unexpectedly. Please send that again." """
        self.subscriptions("claude", "claude-re")
        cid, ev = self.new_chat()
        self.say(cid, ev, "The code word is PAPAYA.")
        chat = self.s.api("GET", f"/api/chats/{cid}")
        self.s.app.chats.sessions[cid].stop_process()  # as after a restart of Crew
        for f in (HOME / "accounts" / "claude-re" / "projects").glob(f"*/{chat['session_id']}.jsonl"):
            f.unlink()
        done, _ = self.say(cid, ev, "What is the code word?")
        self.assertFalse(done["meta"]["error"], done["text"])
        self.assertIn("The code word is PAPAYA.", done["text"])

    def test_claude_code_not_finding_the_conversation_is_not_the_end_of_it(self):
        """Crew found the conversation's file, but Claude Code looked elsewhere (the chat's folder had moved): every
        message ended with "The assistant stopped unexpectedly. Please send that again." """
        self.subscriptions("claude", "claude-rj")
        cid, ev = self.new_chat()
        self.say(cid, ev, "The code word is QUINCE.")
        sid = self.s.api("GET", f"/api/chats/{cid}")["session_id"]
        self.s.app.chats.sessions[cid].stop_process()
        projects = HOME / "accounts" / "claude-rj" / "projects"
        (projects / "an-old-folder").mkdir()
        next(projects.glob(f"*/{sid}.jsonl")).rename(projects / "an-old-folder" / f"{sid}.jsonl")
        done, seen = self.say(cid, ev, "What is the code word?")
        self.assertFalse(done["meta"]["error"], done["text"])
        self.assertIn("The code word is QUINCE.", done["text"])
        self.assertTrue(any(name == "notice" and "from its record" in d["text"] for name, d in seen), seen)

    def test_the_owner_cannot_send_into_the_middle_of_a_move(self):
        """Between the limit's message and the retry on the next subscription the chat looked idle: a message sent
        then went to the subscription at its limit, and the retry of the first message was lost."""
        self.subscriptions("claude", "claude-rh", "claude-ri")
        live(assistant_limit=["claude-rh"])
        cid, ev = self.new_chat(account="claude-rh")
        moving, go_on = threading.Event(), threading.Event()
        real = chat_mod.copy_claude_session

        def slow_copy(*a, **k):
            moving.set()
            go_on.wait(20)
            return real(*a, **k)

        with mock.patch.object(chat_mod, "copy_claude_session", slow_copy):
            self.s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
            self.assertTrue(moving.wait(20))
            err = self.s.api("POST", f"/api/chats/{cid}/send", {"text": "are you there?"}, expect=400)
            self.assertIn("Still answering", err["error"])
            go_on.set()
            while True:
                name, data = ev.get(timeout=60)
                if name == "done" and not data["meta"].get("notice"):
                    break
        self.assertEqual(data["meta"]["account"], "claude-ri")
        self.assertIn("Summary", data["text"])
        self.assertEqual([m["text"] for m in self.s.api("GET", f"/api/chats/{cid}")["messages"]
                          if m["role"] == "user"], ["hello"])

    # ------------------------------------------------------------ ChatGPT

    def test_a_chatgpt_chat_moves_on_with_the_whole_conversation(self):
        """A ChatGPT chat stopped at the limit with "Crew switches to another subscription when you have one", but
        Crew did not: only Claude chats moved on."""
        self.subscriptions("codex", "chatgpt-ra", "chatgpt-rb")
        cid, ev = self.new_chat(engine="codex", account="chatgpt-ra")
        done, _ = self.say(cid, ev, "The code word is MANGO.")
        self.assertEqual(done["meta"]["account"], "chatgpt-ra")
        live(codex_limit=["chatgpt-ra"])
        start = len(prompts("codex"))
        done, seen = self.say(cid, ev, "What is the code word?")
        self.assertEqual(done["meta"]["account"], "chatgpt-rb")
        self.assertFalse(done["meta"]["error"], done["text"])
        self.assertIn("The code word is MANGO.", done["text"])
        self.assertTrue(any(name == "notice" and "chatgpt-ra has reached its usage limit" in d["text"]
                            for name, d in seen), seen)
        asked = prompts("codex")[start:]
        self.assertEqual([p["account"] for p in asked], ["chatgpt-ra", "chatgpt-rb"])
        self.assertFalse(asked[1]["resume"])  # a ChatGPT conversation cannot move: it continues from the record

    def test_switching_a_chatgpt_chat_keeps_all_of_it(self):
        """Moving a ChatGPT chat to another subscription carried only the last 12 messages, each cut to 700
        characters: a long message's end, and anything older, was lost."""
        self.subscriptions("codex", "chatgpt-rc", "chatgpt-rd")
        cid, ev = self.new_chat(engine="codex", account="chatgpt-rc")
        self.say(cid, ev, "Here are my notes. " + "The plan has many parts to it. " * 40 + "The code word is GUAVA.")
        for n in range(7):
            self.say(cid, ev, f"Note {n}: nothing else.")
        done, _ = self.say(cid, ev, "What is the code word?", account="chatgpt-rd")  # the owner switches
        self.assertEqual(done["meta"]["account"], "chatgpt-rd")
        self.assertIn("The code word is GUAVA.", done["text"])

    def test_a_chatgpt_limit_midway_keeps_the_work(self):
        self.subscriptions("codex", "chatgpt-re", "chatgpt-rf")
        live(codex_limit_midway=["chatgpt-re"])
        cid, ev = self.new_chat(engine="codex", account="chatgpt-re")
        start = len(prompts("codex"))
        done, seen = self.say(cid, ev, "Write the report.")
        self.assertEqual(done["meta"]["account"], "chatgpt-rf")
        self.assertIn("I have started on it", done["text"])
        self.assertEqual(sum(1 for name, _ in seen if name == "done"), 1)
        asked = prompts("codex")[start:]
        self.assertIn("Write the report.", asked[1]["text"])
        self.assertIn("I have started on it", asked[1]["text"])  # it knows what it had said …
        self.assertIn("draft.txt", asked[1]["text"])  # … and done
        self.assertIn("carry on from where you stopped", asked[1]["text"].lower())

    def test_only_chatgpt_subscription_at_its_limit_says_when_it_frees_up(self):
        self.subscriptions("codex", "chatgpt-rg")
        live(codex_limit=["chatgpt-rg"])
        cid, ev = self.new_chat(engine="codex")
        done, _ = self.say(cid, ev, "hello")
        self.assertIn("chatgpt-rg has reached its usage limit", done["text"])
        self.assertIn("frees up", done["text"])  # "try again in 2 hours 5 minutes", in the owner's time
        self.assertTrue(done["meta"]["error"])


class CopyTests(unittest.TestCase):
    def test_a_copy_that_was_continued_elsewhere_is_kept(self):
        """Bringing the newest copy of a conversation must never lose a copy that went its own way."""
        import tempfile
        from pathlib import Path

        from crewlib import agents
        root = Path(tempfile.mkdtemp(prefix="crew-copy-"))
        src, dst = cfgmod.Account("a", "claude", str(root / "a")), cfgmod.Account("b", "claude", str(root / "b"))
        for home, text in ((root / "a", "one\ntwo\nthree\n"), (root / "b", "one\n")):
            (home / "projects" / "chat").mkdir(parents=True)
            (home / "projects" / "chat" / "s1.jsonl").write_text(text)
        self.assertTrue(agents.copy_claude_session("s1", src, dst))  # an earlier part of it: simply brought up to date
        self.assertEqual([f.name for f in (root / "b" / "projects" / "chat").iterdir()], ["s1.jsonl"])
        (root / "b" / "projects" / "chat" / "s1.jsonl").write_text("one\ntwo\nsomething else\n")
        self.assertTrue(agents.copy_claude_session("s1", src, dst))
        files = {f.name: f.read_text() for f in (root / "b" / "projects" / "chat").iterdir()}
        self.assertEqual(files.pop("s1.jsonl"), "one\ntwo\nthree\n")
        (kept,) = files.items()
        self.assertTrue(kept[0].startswith("s1.jsonl.kept-"))
        self.assertEqual(kept[1], "one\ntwo\nsomething else\n")


class ChoosingTests(unittest.TestCase):
    """Which subscription a chat uses, from the limits Claude Code and Codex report."""

    def setUp(self):
        self.cfg = cfgmod.Config.__new__(cfgmod.Config)
        self.accounts = [cfgmod.Account("week-full", "claude"), cfgmod.Account("busy", "claude")]
        self.cfg.accounts_for = lambda vendor: list(self.accounts)
        self.manager = chat_mod.ChatManager.__new__(chat_mod.ChatManager)

    def limits(self, **per_account):
        return mock.patch.object(usage_log, "snapshot", return_value={"limits": per_account, "tokens": {}, "now": 0})

    def test_a_weekly_limit_counts_as_at_the_limit(self):
        """Only the 5-hour window was looked at: a subscription at its weekly limit was chosen again and again."""
        now = time.time()
        with self.limits(**{"week-full": {"status": "rejected", "kind": "seven_day", "five_util": 0.1,
                                          "five_reset": None, "week_util": 1.0, "week_reset": now + 3 * 86400},
                            "busy": {"status": "allowed", "five_util": 0.85, "five_reset": now + 3600}}):
            self.assertEqual(self.manager.pick_account(self.cfg, "claude").name, "busy")
            self.assertEqual(self.manager.pick_account(self.cfg, "claude", prefer="week-full").name, "busy")
            self.assertGreater(chat_mod.limited_until({"status": "rejected", "kind": "seven_day",
                                                       "week_reset": now + 60}), now)

    def test_a_limit_that_has_lifted_is_not_shown_as_reached(self):
        """The Usage page said "At its limit" after the limit had lifted, until the subscription was used again; and
        the chat's subscription menu knew only the 5-hour limit."""
        lifted, weekly = f"lifted-{time.time_ns()}", f"weekly-{time.time_ns()}"
        usage_log.record_rate(lifted, {"status": "rejected", "rateLimitType": "five_hour", "utilization": 1.0,
                                       "resetsAt": int(time.time()) - 60})
        usage_log.record_rate(weekly, {"status": "rejected", "rateLimitType": "seven_day", "utilization": 1.0,
                                       "resetsAt": int(time.time()) + 86400})
        limits = usage_log.snapshot(1)["limits"]
        self.assertEqual((limits[lifted]["status"], limits[lifted]["limited_until"]), ("allowed", None))
        self.assertEqual(limits[weekly]["status"], "rejected")
        self.assertGreater(limits[weekly]["limited_until"], time.time() + 86000)

    def test_when_a_limit_lifts_is_read_from_its_message(self):
        now = time.time()
        self.assertEqual(chat_mod.limit_resets_at("Claude AI usage limit reached|1790000000", now), 1790000000)
        self.assertAlmostEqual(chat_mod.limit_resets_at(
            "You've hit your usage limit. Upgrade to Pro, or try again in 2 hours 5 minutes.", now), now + 7500, delta=2)
        self.assertAlmostEqual(chat_mod.limit_resets_at("… or try again in 3 days 1 hour.", now), now + 262800,
                               delta=2)
        self.assertIsNone(chat_mod.limit_resets_at("Something else went wrong.", now))


if __name__ == "__main__":
    unittest.main()
