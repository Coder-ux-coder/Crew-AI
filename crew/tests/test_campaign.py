"""Regression tests from the debugging campaign (DEBUG-CAMPAIGN.md at the repository root, section 12): one per
bug fixed, each failing without its fix and passing with it. The ledger ids (C1, A8 …) name the bug each covers.
Uses the scripted fakes in tests/fakes."""

from __future__ import annotations

import http.client
import io
import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

try:  # `python -m unittest discover -s tests` imports the modules by their own names
    from test_app import ENV, HOME, ROOT, WINDOWLESS, AppServer, _chromium, until
    from test_e2e import dump_chat, make_run, run_orch
except ImportError:
    from tests.test_app import ENV, HOME, ROOT, WINDOWLESS, AppServer, _chromium, until
    from tests.test_e2e import dump_chat, make_run, run_orch

from crewapp import chat as chat_mod  # noqa: E402
from crewapp import launcher  # noqa: E402
from crewapp import phone as phone_mod  # noqa: E402
from crewapp import runs as runs_mod  # noqa: E402
from crewapp import server  # noqa: E402
from crewapp import settings as settings_mod  # noqa: E402
from crewapp import skills as skills_mod  # noqa: E402
from crewapp import updater  # noqa: E402
from crewapp import workflows as workflows_mod  # noqa: E402
from crewapp.sse import hub  # noqa: E402
from crewlib import agents, claude_cli, cli, connections, gitops, lessons, orchestrator, web  # noqa: E402
from crewlib.config import Account  # noqa: E402
from crewlib.store import Store  # noqa: E402
from crewlib import usage as usage_mod, util as util_mod  # noqa: E402
from crewlib.util import Redactor, load_env_file  # noqa: E402


class TempHome:
    """A Crew folder of its own for one test (CREW_HOME points at it while the test runs)."""

    def __enter__(self) -> Path:
        self.saved = os.environ.get("CREW_HOME")
        self.path = Path(tempfile.mkdtemp(prefix="crew-campaign-"))
        os.environ["CREW_HOME"] = str(self.path)
        return self.path

    def __exit__(self, *exc) -> None:
        os.environ["CREW_HOME"] = self.saved or str(HOME)
        shutil.rmtree(self.path, ignore_errors=True)


def drain(q: queue.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def process_running(pid: int) -> bool:
    """Linux: is this process still there (a zombie waiting to be collected counts as ended)?"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


# ====================================================================== the team engine


class LessonTests(unittest.TestCase):
    def test_c1_updates_do_not_make_built_in_lessons_count_for_more(self):
        with TempHome():
            lessons.ensure_seeded()
            before = {x["text"]: x["weight"] for x in lessons.top(1000)}
            self.assertTrue(before)
            # Crew 2.3.0 stored the seed file's date as its version, and every update rewrites that file.
            db = lessons._db()
            db.execute("UPDATE memo SET value='1700000000.0' WHERE key='seeded'")
            db.close()
            lessons.ensure_seeded()
            lessons.ensure_seeded()
            self.assertEqual({x["text"]: x["weight"] for x in lessons.top(1000)}, before)

    def test_c2_an_effort_level_crew_does_not_know(self):
        with TempHome():
            lessons.record_effort_outcome("build", "M", "high", 1, 5, 1000)
            db = lessons._db()
            for effort in ("minimal", None, "ultra"):  # an older record, a hand edit
                for _ in range(3):
                    db.execute("INSERT INTO effort_outcomes(ts,project,kind,size,effort,rounds,first_pass,minutes,tokens) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), "old", "build", "M", effort, 1, 1, 5.0, 1000))
            db.close()
            stats = lessons.effort_stats()
            self.assertEqual(len(stats), 4)
            self.assertEqual(stats[0]["effort"], "high")  # the levels Crew knows come first
            self.assertIn("Your record so far", lessons.render_for_ceo())
            self.assertEqual(lessons.derive_ceo_lessons(), [])  # nothing is learned about unknown levels

    def test_c29_the_effort_record_is_each_models_own(self):
        """The workhorse became Sonnet 5.5, but the CEO's effort record is kept per tier: GPT-6 Sol's results were
        shown to the CEO as the workhorse's (Sonnet's), and lessons worked out from them named Sonnet 5.5."""
        now_models = {"workhorse": "claude-sonnet-5-5", "manager": "claude-opus-5-5"}
        with TempHome():
            for _ in range(4):  # Sol struggled with routine fixes at medium effort
                lessons.record_effort_outcome("fix", "S", "medium", 2, 5, 1000, tier="workhorse", model="gpt-6-sol")
            for _ in range(3):  # Sonnet gets docs right first time at low effort
                lessons.record_effort_outcome("docs", "S", "low", 1, 3, 800, tier="workhorse", model="claude-sonnet-5-5")
            lessons.record_effort_outcome("build", "M", "high", 1, 9, 5000, tier="manager", model="claude-opus-5-5")
            db = lessons._db()  # a record from before models were written down (2.0): the manager's
            db.execute("INSERT INTO effort_outcomes(ts,project,kind,size,effort,rounds,first_pass,minutes,tokens) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), "old", "build", "M", "high", 1, 1, 7.0, 4000))
            db.close()
            stats = lessons.effort_stats(now_models)
            self.assertEqual([(r["tier"], r["kind"], r["effort"], r["n"]) for r in stats],
                             [("workhorse", "docs", "low", 3), ("manager", "build", "high", 2)])
            shown = lessons.render_for_ceo(now_models)
            self.assertNotIn("fix S", shown)  # Sol's results are not Sonnet's
            learned = lessons.derive_ceo_lessons(now_models)
            self.assertEqual(len(learned), 1)
            self.assertIn("docs tasks of size S built by the workhorse (Sonnet 5.5) at low effort", learned[0])
            self.assertEqual(len(lessons.effort_stats()), 3)  # nothing was deleted: Sol's record is still there

    def test_c12_the_ceos_effort_lessons_follow_its_record(self):
        """A lesson worked out from the record kept its first numbers for ever; worse, when the record turned, the
        new lesson (same words, other verdict) only reinforced the old "high is enough"."""
        with TempHome():
            lessons.add("ceo", "Ask the owner before any payment step.", source="agent:ceo")  # the CEO's own
            for _ in range(3):
                lessons.record_effort_outcome("build", "M", "high", 1, 5, 1000)
            lessons.derive_ceo_lessons()
            lessons.derive_ceo_lessons()  # confirmed again after the next project: it counts for more
            ceo = {x["text"]: x["weight"] for x in lessons.top(10, categories=("ceo",))}
            enough = next(t for t in ceo if "high is enough" in t)
            self.assertIn("100% of the time (3 tasks", enough)
            self.assertEqual(ceo[enough], 2)
            for _ in range(9):  # the record turns: most such tasks now need a second round
                lessons.record_effort_outcome("build", "M", "high", 2, 5, 1000)
            lessons.derive_ceo_lessons()
            shown = lessons.render_for_ceo()
            self.assertIn("only 25% of the time (12 tasks): give such work more effort", shown)
            self.assertNotIn("high is enough", shown)
            self.assertIn("Ask the owner before any payment step.", shown)  # its own lessons are never touched


class OrchestratorTests(unittest.TestCase):
    def test_c3_a_damaged_or_locked_cost_model_is_ignored(self):
        with TempHome():
            db = lessons._db()
            db.execute("INSERT INTO memo(key,value) VALUES('cost_model','{not json')")
            db.close()
            self.assertEqual(orchestrator.lessons_cost_model(), {})
            for value, want in (("[1, 2]", {}),
                                ('{"tokens_per_unit": {"a": "lots", "b": 3}, "util_per_token": null}',
                                 {"tokens_per_unit": {"b": 3.0}, "util_per_token": {}})):
                db = lessons._db()
                db.execute("UPDATE memo SET value=? WHERE key='cost_model'", (value,))
                db.close()
                self.assertEqual(orchestrator.lessons_cost_model(), want)
            with mock.patch.object(lessons, "_db", side_effect=sqlite3.OperationalError("database is locked")):
                self.assertEqual(orchestrator.lessons_cost_model(), {})

    def test_c4_a_delivered_project_stays_delivered(self):
        with TempHome() as home:
            st = Store(home / "team.db")
            logged: list[str] = []
            # The retrospective saves what it can when the cost model cannot be written …
            fake = types.SimpleNamespace(store=st, cfg=None, seats={}, lead_name="ada", events=queue.Queue(),
                                         log=logged.append)
            with mock.patch.object(orchestrator, "update_cost_model", side_effect=sqlite3.OperationalError("locked")):
                orchestrator.Orchestrator.retrospective(fake)
            self.assertTrue((home / "PLAYBOOK.md").is_file())
            self.assertTrue(any("cost model" in line for line in logged))
            # … and a retrospective that fails altogether never turns the delivery into a failure.
            phases: list[str] = []
            fake = types.SimpleNamespace(
                cfg=types.SimpleNamespace(team=types.SimpleNamespace(deliver="branch")), store=st,
                integration="crew/demo/main", repo=home, write_report=lambda where: None, say=lambda *a, **k: None,
                retrospective=mock.Mock(side_effect=sqlite3.OperationalError("database is locked")),
                log=logged.append, set_phase=phases.append)
            orchestrator.Orchestrator.deliver(fake)
            self.assertEqual(phases, ["done"])
            self.assertTrue(st.get("delivered"))
            st.close()

    def test_c14_a_task_sent_back_again_and_again_is_not_progress(self):
        with TempHome() as home:
            st = Store(home / "team.db")
            tid = st.create_task("Foundation", "Create the package skeleton.", "package imports", ["app/**"], [])
            fake = types.SimpleNamespace(store=st, task_snapshot={}, task_stage={}, last_progress=0.0, stall_count=3)
            orchestrator.Orchestrator.track_progress(fake)
            self.assertEqual(fake.stall_count, 0)  # a new task is progress
            for status in ("in_progress", "review"):  # and so is each step it takes for the first time
                fake.last_progress = 0.0
                st.update_task(tid, status=status)
                orchestrator.Orchestrator.track_progress(fake)
                self.assertGreater(fake.last_progress, 0.0, status)
            fake.stall_count = 2
            for _ in range(3):  # sent back again and again (its checks can never pass): going round in circles
                for status in ("changes", "in_progress", "review"):
                    fake.last_progress = 0.0
                    st.update_task(tid, status=status)
                    orchestrator.Orchestrator.track_progress(fake)
                    self.assertEqual((fake.last_progress, fake.stall_count), (0.0, 2), status)
            st.update_task(tid, status="approved")  # a real step forward
            orchestrator.Orchestrator.track_progress(fake)
            self.assertGreater(fake.last_progress, 0.0)
            self.assertEqual(fake.stall_count, 0)
            st.close()

    def test_c20_a_limit_parks_the_subscription_until_it_really_lifts(self):
        """A seat at its limit parked its subscription until the time left over from an earlier limit: already
        past, so it was parked for one minute, tried again, failed again, and so on, and the team was told it
        "resets" at a time already gone (ChatGPT seats report the limit only in words)."""
        with TempHome() as home:
            st = Store(home / "team.db")
            st.upsert_account("chatgpt-1", vendor="codex", status="allowed", parked_until=int(time.time() - 86400))
            said, moved = [], []
            fake = types.SimpleNamespace(store=st, say=lambda text, **k: said.append(text),
                                         failover=lambda rt, reason: moved.append(reason),
                                         answer_owner=lambda *a, **k: None, log=lambda *a: None)
            fake.park = lambda *a: orchestrator.Orchestrator.park(fake, *a)
            rt = types.SimpleNamespace(name="sol-1", busy=True, owner_interrupt=0.0, pending=[],
                                       account=Account("chatgpt-1", "codex"))
            orchestrator.Orchestrator.on_result(fake, rt, {"limit_hit": True, "is_error": True, "text":
                "You've hit your usage limit. Upgrade to Pro, or try again in 2 hours 5 minutes."})
            until = st.account("chatgpt-1")["parked_until"]
            self.assertAlmostEqual(until, time.time() + 7500, delta=60)  # when the message says it lifts
            self.assertEqual(moved, ["usage limit"])
            self.assertIn(time.strftime("%H:%M", time.localtime(until)), said[0])
            st.upsert_account("chatgpt-1", parked_until=int(time.time() - 60))
            orchestrator.Orchestrator.on_result(fake, rt, {"limit_hit": True, "is_error": True,
                                                           "text": "Usage limit reached."})  # no time given
            self.assertGreater(st.account("chatgpt-1")["parked_until"], time.time() + 3000)  # an hour, not a minute
            st.upsert_account("chatgpt-1", parked_until=int(time.time() - 60))  # a reviewer on ChatGPT, the same way
            orchestrator.Orchestrator.park(fake, "chatgpt-1", "You've hit your usage limit. Try again in 3 hours.")
            self.assertAlmostEqual(st.account("chatgpt-1")["parked_until"], time.time() + 10800, delta=60)
            st.close()

    def test_c22_final_checks_that_keep_failing_end_with_an_honest_stop(self):
        """Every task merged, but the final checks failed: the lead was sent back to fix them, again and again,
        with nothing to end it (no task was open, so the stall guard did not look)."""
        with TempHome() as home:
            st = Store(home / "team.db")
            said, phases = [], []
            lead = types.SimpleNamespace(pending=[])
            fake = types.SimpleNamespace(store=st, say=lambda text, **k: said.append(text), lead_name="ada",
                                         seats={"ada": lead}, set_phase=phases.append, log=lambda *a: None)
            for _ in range(2):
                orchestrator.Orchestrator.on_final_done(fake, "red:test_app.py failed")
            self.assertEqual(phases, ["build", "build"])  # two rounds of fixes …
            orchestrator.Orchestrator.on_final_done(fake, "red:test_app.py failed")
            self.assertEqual(phases[-1], "stopped")  # … then an honest stop, with the report
            self.assertIn("final checks still fail", said[-1])
            st.close()

    def test_c24_a_subscription_that_just_reached_its_limit_is_not_chosen_again(self):
        """A reviewer ran out on claude-1 a moment ago: its recorded mode still says "normal" until the main loop's
        next pass, but the next reviewer must go to claude-2 (even though the author works there)."""
        with TempHome() as home:
            st = Store(home / "team.db")
            st.upsert_account("claude-1", vendor="claude", mode="normal", parked_until=int(time.time()) + 600)
            st.upsert_account("claude-2", vendor="claude", mode="normal")
            fake = types.SimpleNamespace(store=st, cfg=types.SimpleNamespace(
                accounts=[Account("claude-1", "claude"), Account("claude-2", "claude")]))
            fake.accounts = lambda: orchestrator.Orchestrator.accounts(fake)
            modes = orchestrator.Orchestrator.modes(fake)
            self.assertEqual(modes, {"claude-1": "parked", "claude-2": "normal"})
            pick = orchestrator.scheduler.pick_account(fake.accounts(), modes, avoid="claude-2")
            self.assertEqual(pick["name"], "claude-2")
            st.close()

    def test_c25_a_task_its_suggested_seat_cannot_take_goes_to_another(self):
        """The lead suggested boole for a medium task, but boole's subscription is nearly used up, so boole only
        takes small or light work for now. Every other seat left the task to boole because boole was free: it
        waited until that subscription recovered (days, for a weekly limit), while curie sat idle."""
        with TempHome() as home:
            st = Store(home / "team.db")
            st.set("phase", "build")
            st.upsert_account("claude-1", vendor="claude")
            st.upsert_account("claude-2", vendor="claude", util_5h=0.95, reset_5h=int(time.time()) + 4 * 3600)
            places = {"ada": "claude-1", "boole": "claude-2", "curie": "claude-1"}
            for name, account in places.items():
                st.upsert_seat(name, vendor="claude", tier="manager", role="lead" if name == "ada" else "member",
                               account=account, model="claude-opus-5-5", status="idle")
            tid = st.create_task("Checkout page", "Build the checkout page.", "it works", ["checkout.py"], [],
                                 size="M", kind="build", suggested_owner="boole", created_by="ada", tier="manager")
            seats = {name: types.SimpleNamespace(name=name, down=False, busy=name == "ada", runner=object(),
                                                 pending=[], account=types.SimpleNamespace(name=account))
                     for name, account in places.items()}
            given: list[tuple[str, int]] = []
            fake = types.SimpleNamespace(store=st, seats=seats, lead_name="ada", grace_until={},
                                         cfg=types.SimpleNamespace(accounts=[Account("claude-1", "claude"),
                                                                             Account("claude-2", "claude")]),
                                         workhorse_seats=lambda modes=None: [], manager_may_help=lambda ready: set(),
                                         give_task=lambda rt, task, mode: given.append((rt.name, task["id"])) or True)
            fake.accounts = lambda: orchestrator.Orchestrator.accounts(fake)
            modes = orchestrator.Orchestrator.modes(fake)
            self.assertEqual(modes["claude-2"], "conserve")
            orchestrator.Orchestrator.assign_work(fake, modes)
            self.assertEqual(given, [("curie", tid)])
            st.close()

    def test_a4_the_heartbeat_says_whether_a_project_runs(self):
        with TempHome() as home:
            st = Store(home / "team.db")
            self.assertIsNone(st.orchestrator_alive())  # a project from a Crew without the heartbeat
            fake = types.SimpleNamespace(store=st)
            orchestrator.Orchestrator.start_heartbeat(fake)
            self.assertTrue(st.orchestrator_alive())
            orchestrator.Orchestrator.stop_heartbeat(fake)
            self.assertFalse(st.orchestrator_alive())
            st.set("alive", {"pid": 1, "at": time.time() - 3600})
            self.assertFalse(st.orchestrator_alive())
            st.set("alive", {"pid": 1, "at": "garbage"})
            self.assertFalse(st.orchestrator_alive())
            st.close()

    def test_a4_a_running_project_is_never_started_twice(self):
        with TempHome() as home:
            rid = "20260928-120000-bakery-site"
            (home / "runs" / rid).mkdir(parents=True)
            st = Store(home / "runs" / rid / "team.db")
            st.set("phase", "build")
            st.set("alive", {"pid": 1, "at": time.time()})  # quiet (all subscriptions at their limit), but alive
            manager = runs_mod.RunManager()
            with mock.patch.object(manager, "_spawn") as spawn:
                self.assertTrue(manager.alive(rid))
                self.assertFalse(manager.resume(rid))
                spawn.assert_not_called()
                st.set("alive", {"pid": 1, "at": time.time(), "stopped": True})
                self.assertFalse(manager.alive(rid))
                self.assertTrue(manager.resume(rid))
                spawn.assert_called_once()
            st.close()
            for s in manager._stores.values():
                s.close()


class StartTests(unittest.TestCase):
    def test_a5_a_request_that_looks_like_an_option_starts_its_project(self):
        with TempHome():
            manager = runs_mod.RunManager()
            for request in ("--dark-mode-everywhere", "Make my bakery a website. " * 3000):  # a flag; 81,000 letters
                with mock.patch.object(manager, "_spawn") as spawn:
                    rid = manager.start(request)
                args = spawn.call_args[0][1]
                self.assertLess(sum(len(a) for a in args), 2000)  # Windows allows about 32,000 on a command line
                seen = {}

                def fake_run(cfg, run_dir, repo, req, run_id, seen=seen, **kw):
                    seen.update(request=req, run_id=run_id)
                    return 0

                with mock.patch.object(cli, "_run", fake_run), mock.patch.object(gitops, "ensure_repo", lambda p: p), \
                        mock.patch.object(cli.config_mod, "load", lambda *a, **k: mock.MagicMock()):
                    self.assertEqual(cli.main(args), 0)
                self.assertEqual(seen, {"request": request.strip(), "run_id": rid})

    def test_a7_two_projects_started_together_get_their_own_folders(self):
        with TempHome() as home:
            root = home / "runs"
            root.mkdir()
            with mock.patch.object(cli, "new_run_id", return_value="20260928-120000-a-website-for-my-bakery"):
                first = cli.claim_run_dir(root, "a website for my bakery")
                second = cli.claim_run_dir(root, "a website for my bakery")
            self.assertNotEqual(first[0], second[0])
            self.assertTrue(first[1].is_dir() and second[1].is_dir())


class StuckProjectTests(unittest.TestCase):
    def test_c14_a_task_whose_checks_can_never_pass_ends_with_an_honest_stop(self):
        """Before, the task was sent back for ever (216 rounds in 4 minutes with the fakes): the owner's usage went
        on it without end. Now the lead replans, the CEO rules, and the project stops with a report."""
        with mock.patch.dict(os.environ):  # make_run points CREW_HOME and the fakes at a project of its own
            cfg, run_dir, repo, rid = make_run({"tasks": 1, "broken_checks": True},
                                               [("claude-1", "claude"), ("claude-2", "claude")], ledger=0.1)
            orch = run_orch(cfg, run_dir, repo, rid, timeout=150)
            st = orch.store
            chat = dump_chat(st)
            self.assertEqual(st.get("phase"), "stopped", chat)
            self.assertIn("could not make progress", chat)
            self.assertTrue(st.events("escalation"), chat)  # the CEO was asked to rule before the stop
            self.assertTrue((run_dir / "REPORT.md").is_file())


    def test_c21_a_project_resumes_after_a_subscription_it_used_was_removed(self):
        """A seat moved to another subscription at a limit; the owner later removed that subscription in Settings.
        Continuing the project failed at once ("unknown account")."""
        with mock.patch.dict(os.environ):
            cfg, run_dir, repo, rid = make_run({"tasks": 2}, [("claude-1", "claude"), ("claude-2", "claude")])
            orch = run_orch(cfg, run_dir, repo, rid,
                            stop_after=lambda st: any(t["status"] == "merged" for t in st.tasks()))
            self.assertEqual(orch.store.get("phase"), "stopped")
            member = next(s["name"] for s in orch.store.seats() if s["role"] != "lead")
            orch.store.update_seat(member, account="claude-gone", session_id="0000-gone")  # since removed
            orch.store.upsert_account("claude-gone", vendor="claude", util_5h=0.0, util_7d=0.0)  # the freest one
            orch.store.close()
            orch2 = run_orch(cfg, run_dir, repo, rid, resume=True)
            self.assertEqual(orch2.store.get("phase"), "done", dump_chat(orch2.store))
            self.assertIn(orch2.store.seat(member)["account"], ("claude-1", "claude-2"))


class ResumeChoiceTests(unittest.TestCase):
    def test_c23_a_project_whose_chosen_subscriptions_were_removed_still_continues(self):
        """A project the owner limited to some subscriptions: when its Claude subscription was later removed in
        Settings, Continue failed ("choose at least one Claude subscription") and, in the app, seemed to do
        nothing."""
        with TempHome() as home:
            (home / "crew.toml").write_text('[[account]]\nname = "claude-1"\nvendor = "claude"\n\n'
                                            '[[account]]\nname = "codex-1"\nvendor = "codex"\n', encoding="utf-8")
            run_dir = cli.runs_dir() / "20260929-100000-page"
            run_dir.mkdir()
            st = Store(run_dir / "team.db")
            st.set("accounts_chosen", ["claude-gone", "codex-1"])
            st.set("phase", "stopped")
            st.set("repo", str(home))
            st.close()
            ran = []
            args = types.SimpleNamespace(run=run_dir.name, config=str(home / "crew.toml"), no_web=True, headless=True)
            with mock.patch.object(cli, "_run", lambda cfg, *a, **k: ran.append(cfg) or 0):
                self.assertEqual(cli.cmd_resume(args), 0)
            self.assertEqual(sorted(a.name for a in ran[0].accounts), ["claude-1", "codex-1"])


class SeatTests(unittest.TestCase):
    def test_c5_a_codex_turn_that_cannot_run_still_ends(self):
        with TempHome() as home:
            events: queue.Queue = queue.Queue()
            setup = agents.CodexSetup(model="gpt-6-astra", effort="auto", run_dir=home, extra_env={})
            seat = agents.CodexSeat("curie", "member", Account("mohidzeeshanrana-gmail.com", "codex"), home, setup,
                                    "system", events, Redactor({}))
            seat.busy = True
            with mock.patch.object(agents, "_drive_codex", side_effect=RuntimeError("the program vanished")):
                seat._turn("Build the order form.")
            ev = events.get(timeout=5)
            self.assertEqual((ev.kind, ev.data["is_error"]), ("result", True))
            self.assertIn("the program vanished", ev.data["text"])
            self.assertFalse(seat.busy)

    def test_c6_one_odd_message_does_not_silence_a_claude_seat(self):
        with TempHome() as home:
            events: queue.Queue = queue.Queue()
            setup = agents.ClaudeSetup(model="claude-opus-5-5", effort="auto", work_model="claude-opus-5-5",
                                       permission_mode="bypassPermissions", run_dir=home, extra_env={})
            seat = agents.ClaudeSeat("ada", "lead", Account("ceo-pbit.gop.pk", "claude"), home, setup, "system", events,
                                     Redactor({}))
            lines = [json.dumps({"type": "assistant", "message": {"content": ["not a block"]}}) + "\n",
                     json.dumps({"type": "result", "subtype": "success", "result": "done", "usage": {}}) + "\n"]
            seat.proc = types.SimpleNamespace(stdout=iter(lines), wait=lambda timeout=None: 0, poll=lambda: 0)
            seat._read_stdout()
            self.assertEqual([ev.kind for ev in drain(events)], ["result", "exit"])

    def test_c16_a_stray_line_does_not_end_a_one_off_run(self):
        """A reviewer, the CEO or the prompt writer: one output line that is JSON but not a message (null, a list)
        raised AttributeError and ended the run; C6 fixed this for the standing seats only."""
        with TempHome() as home:
            result = json.dumps({"type": "result", "subtype": "success", "result": "APPROVE", "usage": {}})
            out, err = io.StringIO(f"null\n[1]\n{result}\n"), io.StringIO("a warning\n")
            proc = types.SimpleNamespace(stdin=io.StringIO(), stdout=out, stderr=err, pid=0,
                                         wait=lambda timeout=None: 0, poll=lambda: 0)
            setup = agents.ClaudeSetup(model="claude-opus-5-5", effort="auto", work_model="claude-opus-5-5",
                                       permission_mode="bypassPermissions", run_dir=home, extra_env={})
            with mock.patch.object(agents, "_popen", return_value=proc):
                res = agents.run_once_claude("Review task #1.", seat="reviewer-1", role="reviewer",
                                             account=Account("claude-1", "claude"), workdir=home, setup=setup,
                                             redact=Redactor({}), with_team_tools=False, timeout=60)
            self.assertEqual((res.text, res.is_error), ("APPROVE", False))
            self.assertTrue(out.closed)  # C17: its pipes are let go
            until(lambda: err.closed, timeout=10, step=0.05)


class CheckTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX hook scripts")
    def test_c26_the_projects_own_commit_hooks_do_not_stop_crews_merges(self):
        """A project whose hooks refuse Crew's commit messages (commitlint allows only "feat: …", "fix: …"): a
        task's merge failed every time, and a merge that broke the checks could not be undone."""
        with TempHome() as home:
            repo = gitops.ensure_repo(home / "shop")
            base = gitops.current_branch(repo)
            gitops.git(repo, "checkout", "-q", "-b", "crew/task-1")
            (repo / "page.html").write_text("<h1>Menu</h1>\n", encoding="utf-8")
            gitops.commit_all(repo, "task 1 by ada")
            gitops.git(repo, "checkout", "-q", base)
            for name in ("commit-msg", "pre-merge-commit", "pre-commit"):
                hook = Path(gitops.out(repo, "rev-parse", "--git-path", "hooks")) / name
                if not hook.is_absolute():
                    hook = repo / hook
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text("#!/bin/sh\necho 'commitlint: type must be one of [feat, fix]' >&2\nexit 1\n")
                hook.chmod(0o755)
            res = gitops.merge_into(repo, "crew/task-1", "crew: task #1 Menu page")
            self.assertTrue(res.ok, res.message)
            self.assertTrue((repo / "page.html").is_file())
            gitops.revert_last_merge(repo, "task #1 broke the checks")
            self.assertFalse((repo / "page.html").exists())
            self.assertEqual(gitops.out(repo, "log", "-1", "--format=%s"), "crew: revert merge — task #1 broke the checks")
            self.assertFalse(gitops.is_dirty(repo))

    @unittest.skipIf(os.name == "nt", "a POSIX shell command")
    def test_c7_a_check_that_leaves_a_server_running_finishes(self):
        with TempHome() as home:
            pid_file = home / "server.pid"
            started = time.time()
            res = gitops.run_checks(home, [f"sleep 60 & echo $! > '{pid_file}'; echo checked"], home / "check.log",
                                    timeout_s=20)
            self.assertLess(time.time() - started, 15)
            self.assertTrue(res.ok, res.summary)
            self.assertIn("checked", (home / "check.log").read_text(encoding="utf-8"))
            if Path("/proc").is_dir():  # what the check started ended with it
                pid = int(pid_file.read_text())
                until(lambda: not process_running(pid), timeout=10, step=0.2)

    @unittest.skipIf(os.name == "nt", "a POSIX shell command")
    def test_c7_a_check_past_its_time_limit_is_stopped(self):
        with TempHome() as home:
            started = time.time()
            res = gitops.run_checks(home, ["sleep 30"], home / "check.log", timeout_s=1)
            self.assertLess(time.time() - started, 15)
            self.assertFalse(res.ok)
            self.assertIn("timed out after 1s", (home / "check.log").read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX shell commands")
    def test_c15_a_test_runner_that_finds_no_tests_yet_is_not_a_failure(self):
        with TempHome() as home:
            (home / "tests").mkdir()
            (home / "tests" / "__init__.py").write_text("", encoding="utf-8")  # a project's skeleton: no tests yet
            unittest_cmd = f"'{sys.executable}' -m unittest discover -s tests -q"  # exits with 5 from Python 3.12
            pytest_like = "echo 'collected 0 items'; echo '=== no tests ran in 0.01s ==='; exit 5"
            for cmd in (unittest_cmd, pytest_like):
                res = gitops.run_checks(home, [cmd, "echo next-check"], home / "check.log", timeout_s=60)
                self.assertTrue(res.ok, res.summary)
                self.assertIn("next-check", (home / "check.log").read_text(encoding="utf-8"))  # the others still run
            res = gitops.run_checks(home, ["echo 'the database is missing'; exit 5"], home / "check.log", timeout_s=60)
            self.assertFalse(res.ok)  # a real failure that happens to exit with 5 still fails


class LiveViewTests(unittest.TestCase):
    def test_c13_only_the_live_view_itself_can_talk_to_the_team(self):
        with TempHome() as home:
            Store(home / "team.db").close()
            live = web.serve(home, port=0)
            port = live.server_port

            def request(method: str, path: str, headers: dict) -> int:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                body = json.dumps({"text": "Delete the tests."}) if method == "POST" else None
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                resp.read()
                conn.close()
                return resp.status

            try:
                st = Store(home / "team.db")
                self.assertEqual(request("POST", "/api/say", {"Content-Type": "text/plain"}), 403)  # another site
                self.assertEqual(request("POST", "/api/stop", {"Content-Type": "text/plain"}), 403)
                self.assertEqual(request("POST", "/api/say", {"X-Crew": "1", "Host": "evil.example"}), 403)
                self.assertEqual(request("GET", "/api/state", {"Host": "evil.example"}), 403)  # DNS rebinding
                self.assertEqual(st.recent_messages(5), [])
                self.assertIsNone(st.get("stop_requested"))
                self.assertEqual(request("POST", "/api/say", {"X-Crew": "1", "Content-Type": "application/json"}), 200)
                self.assertEqual([m["text"] for m in st.recent_messages(5)], ["Delete the tests."])
                self.assertEqual(request("GET", "/api/state?after=abc", {}), 200)
                st.close()
            finally:
                live.shutdown()
                live.server_close()


class DataFileTests(unittest.TestCase):
    def test_c27_a_file_held_open_for_a_moment_is_still_saved(self):
        """Windows cannot replace a file another program has open (an antivirus scan of the file just written,
        another Crew window reading it): the save failed at once, and its half-step file stayed behind."""
        class Held:  # os, except that the file is held open for the first `busy` replacements
            def __init__(self, busy):
                self.busy = busy

            def __getattr__(self, name):
                return getattr(os, name)

            def replace(self, src, dst):
                if self.busy:
                    self.busy -= 1
                    raise PermissionError(13, "The process cannot access the file because it is being used by "
                                              "another process")
                return os.replace(src, dst)

        with TempHome() as home:
            target = home / "crew.toml"
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(util_mod, "os", Held(2)):
                util_mod.atomic_write(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            with mock.patch.object(util_mod, "os", Held(1000)), self.assertRaises(PermissionError):
                util_mod.atomic_write(target, "newer")  # held for good: it says so …
            self.assertEqual(target.read_text(encoding="utf-8"), "new")  # … the file is as it was …
            self.assertEqual([f.name for f in home.iterdir() if f.name.startswith(".crew.toml")], [])  # … no leftovers

    def test_c28_a_settings_or_keys_file_saved_by_windows_notepad_is_read(self):
        """Notepad (Windows 10 before 1903) saves UTF-8 with a byte-order mark: a hand-edited crew.toml was set aside
        as damaged and the defaults used; the first key in secrets.env was silently ignored."""
        with TempHome() as home:
            (home / "crew.toml").write_bytes("\ufeff[team]\nmode = \"team\"\n".encode("utf-8"))
            self.assertEqual(settings_mod.load()["team"]["mode"], "team")
            self.assertIsNone(settings_mod.problem())
            (home / "secrets.env").write_bytes("\ufeffHUNTER_API_KEY=abc123456789\nOTHER=zzz\n".encode("utf-8"))
            self.assertEqual(load_env_file(home / "secrets.env"),
                             {"HUNTER_API_KEY": "abc123456789", "OTHER": "zzz"})
            settings_mod.save_secret("NEW_KEY", "value-123456")  # a key added from the app keeps the others
            self.assertEqual(sorted(load_env_file(home / "secrets.env")), ["HUNTER_API_KEY", "NEW_KEY", "OTHER"])

    def test_c8_a_connections_file_of_the_wrong_shape(self):
        with TempHome() as home:
            path = home / "connections.json"
            for text in ("[1, 2]", '{"mcp": null}', '{"mcp": [1]}', '"text"'):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(connections.mcp_servers(), {})
                self.assertEqual(connections.listing(), [])
            path.write_text(json.dumps({"mcp": {"a": "odd", "b": {"type": "http"},
                                                "c": {"type": "stdio", "command": "npx", "args": "not-a-list"}}}),
                            encoding="utf-8")
            self.assertEqual(connections.mcp_servers(), {"c": {"type": "stdio", "command": "npx", "args": []}})
            path.write_text("[1, 2]", encoding="utf-8")
            connections.add_mcp("notes", "http", url="https://example.com/mcp")
            self.assertEqual(list(connections.load()["mcp"]), ["notes"])
            kept = list(home.glob("connections.json.damaged-*"))
            self.assertEqual([p.read_text(encoding="utf-8") for p in kept], ["[1, 2]"])  # the old file is kept

    def test_c10_the_team_tool_server_survives_a_message_that_is_not_an_object(self):
        with TempHome() as home:
            env = {**os.environ, "CREW_DB": str(home / "team.db"), "CREW_SEAT": "ada", "CREW_ROLE": "lead",
                   "PYTHONPATH": str(ROOT)}
            lines = "[1, 2]\n" + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}) + "\n"
            proc = subprocess.run([sys.executable, "-m", "crewlib.mcp_server"], input=lines, capture_output=True,
                                  text=True, encoding="utf-8", env=env, cwd=str(ROOT), timeout=60)
            answers = [json.loads(x) for x in proc.stdout.splitlines()]
            self.assertEqual(answers[0]["error"]["code"], -32600)
            self.assertEqual(answers[1]["id"], 7)
            self.assertIn("result", answers[1])

    def test_c11_a_damaged_claude_code_record(self):
        with TempHome() as home:
            for text in ("[1]", '{"last_update": "yesterday"}'):
                (home / "claude-cli.json").write_text(text, encoding="utf-8")
                with mock.patch.object(claude_cli, "update", return_value=(True, "updated")) as update:
                    self.assertEqual(claude_cli.update_if_stale(), (True, "updated"))
                    update.assert_called_once()

    def test_a13_a_damaged_record_of_the_running_crew(self):
        with TempHome() as home:
            for text in ("[1]", '{"port": "abc"}', "{"):
                (home / "running.json").write_text(text, encoding="utf-8")
                self.assertIsInstance(launcher.recorded(), dict)
                self.assertEqual(launcher._ports(8765)[0], 8765)


class SettingsTests(unittest.TestCase):
    def test_a1_a_damaged_settings_file_is_set_aside_and_crew_opens(self):
        with TempHome() as home:
            settings_mod.save({"app": {"theme": "dark"},
                               "accounts": [{"name": "ceo-pbit.gop.pk", "vendor": "claude", "profile": ""}]})
            settings_mod.save({"app": {"owner_name": "Zeeshan"}})  # the first version is kept as crew.toml.bak
            damaged = '[app]\ntheme = "dark\n[[account]\n'  # a typing slip in a hand edit
            (home / "crew.toml").write_text(damaged, encoding="utf-8")
            st = settings_mod.load()
            self.assertEqual(st["app"]["theme"], "dark")
            self.assertEqual([a["name"] for a in st["accounts"]], ["ceo-pbit.gop.pk"])
            problem = settings_mod.problem()
            self.assertEqual(problem["restored"], "the last good copy")
            self.assertEqual((home / problem["kept"]).read_text(encoding="utf-8"), damaged)
            # A value Crew refuses, and a last copy that is no better: the defaults, and both files are kept.
            (home / "crew.toml").write_text('[team]\nmode = "sometimes"\n', encoding="utf-8")
            (home / "crew.toml.bak").write_text("not toml at all [", encoding="utf-8")
            time.sleep(1.1)  # the kept copy is named by the second
            st = settings_mod.load()
            self.assertEqual([a["name"] for a in st["accounts"]], ["claude-1"])
            self.assertEqual(settings_mod.problem()["restored"], "the defaults")
            self.assertEqual((home / "crew.toml.bak").read_text(encoding="utf-8"), "not toml at all [")
            self.assertEqual(len(list(home.glob("crew.toml.damaged-*"))), 2)

    def test_a34_a_hand_edited_setting_of_the_wrong_kind_is_set_aside(self):
        with TempHome() as home:
            settings_mod.save({"team": {"max_hours": 2.0}})
            settings_mod.save({"app": {"owner_name": "Zeeshan"}})  # the version before is the last good copy
            (home / "crew.toml").write_text('[team]\nmax_hours = "two"\n', encoding="utf-8")  # a typing slip
            st = settings_mod.load()
            self.assertEqual(st["team"]["max_hours"], 2.0)  # not "two", which stopped every project
            self.assertEqual(settings_mod.problem()["restored"], "the last good copy")
            kept = home / settings_mod.problem()["kept"]
            self.assertEqual(kept.read_text(encoding="utf-8"), '[team]\nmax_hours = "two"\n')

    def test_a35_a_damaged_file_in_the_old_format_is_kept_as_it_was(self):
        with TempHome() as home:
            settings_mod.save({"team": {"max_hours": 2.0}})
            settings_mod.save({"app": {"owner_name": "Zeeshan"}})
            # Written by hand from crew.toml.example (no settings_version), with a slip: Crew's upgrade of old
            # files rewrote it in its own layout before finding the slip, so the copy kept was not the owner's.
            mine = "# my settings\n[team]\nmode = \"sometimes\"  # I will fix this later\n"
            (home / "crew.toml").write_text(mine, encoding="utf-8")
            settings_mod.load()
            kept = home / settings_mod.problem()["kept"]
            self.assertEqual(kept.read_text(encoding="utf-8"), mine)

    def test_a36_two_settings_saved_at_the_same_moment_both_stay(self):
        with TempHome():
            settings_mod.save({"app": {"owner_name": "Zeeshan"}})
            errors: list[Exception] = []

            def change(key, values):
                for v in values:
                    try:
                        settings_mod.save({"app": {key: v}})
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)

            a = threading.Thread(target=change, args=("theme", ["light", "dark"] * 15 + ["dark"]))
            b = threading.Thread(target=change, args=("voice_rate", [1.1, 1.3] * 15 + [1.5]))
            a.start()
            b.start()
            a.join()
            b.join()
            self.assertEqual(errors, [])
            app = settings_mod.load()["app"]
            self.assertEqual((app["theme"], app["voice_rate"], app["owner_name"]), ("dark", 1.5, "Zeeshan"))

    def test_a23_a_pasted_key_with_a_line_break(self):
        with TempHome() as home:
            settings_mod.save_secret("HUNTER_API_KEY", "  hk_test_0123456789  \n")
            self.assertEqual(load_env_file(home / "secrets.env")["HUNTER_API_KEY"], "hk_test_0123456789")
            with self.assertRaises(ValueError):
                settings_mod.save_secret("OTHER_API_KEY", "abc\nEXTRA_SETTING=1")
            self.assertNotIn("EXTRA_SETTING", (home / "secrets.env").read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                settings_mod.save_secret("OTHER_API_KEY", 12345)


class LogTests(unittest.TestCase):
    def test_a54_the_log_does_not_grow_for_ever(self):
        """Started from the desktop icon (the owner's usual way), every message went to app.log for as long as
        Crew was used: it was never trimmed."""
        home = Path(tempfile.mkdtemp(prefix="crew-windowless-"))
        old = "an old line\n" * 600_000  # about 7 MB
        (home / "app.log").write_text(old, encoding="utf-8")
        env = {**os.environ, **ENV, "CREW_HOME": str(home), "PYTHONPATH": str(ROOT)}
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", WINDOWLESS, "no-such-command"], cwd=str(ROOT),
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess((home / "app.log").stat().st_size, 100_000)  # a fresh log …
        self.assertIn("Crew starting: no-such-command", (home / "app.log").read_text(encoding="utf-8"))
        self.assertEqual((home / "app.log.1").read_text(encoding="utf-8"), old)  # … and the last one is kept
        shutil.rmtree(home, ignore_errors=True)


class DamagedDatabaseTests(unittest.TestCase):
    def test_a52_a_damaged_database_is_kept_and_crew_still_opens(self):
        """A damaged chat history (app.db) stopped Crew from opening at all; a damaged usage record (usage.db)
        made every chat message fail; damaged lessons (memory.db) broke the lessons page."""
        with TempHome() as home:
            junk = b"the disk went wrong here " * 200
            for name in ("app.db", "usage.db", "memory.db"):
                (home / name).write_bytes(junk)
            app = server.App(0, False)  # before: DatabaseError, and Crew did not open
            try:
                cid = app.chats.create()["id"]
                self.assertEqual(app.chats.get(cid)["title"], "New chat")
                self.assertEqual(usage_mod.snapshot(1)["limits"], {})
                lessons.add("process", "Small tasks finish sooner.", source="agent:lead")
                self.assertIn("Small tasks finish sooner.", [x["text"] for x in lessons.top(100)])
            finally:
                app.chats.shutdown()
            for name in ("app.db", "usage.db", "memory.db"):
                (kept,) = home.glob(f"{name}.damaged-*")  # each is kept, as it was
                self.assertEqual(kept.read_bytes(), junk)
            told = util_mod.data_problems()
            self.assertEqual(sorted(p["kept"].split(".damaged-")[0] for p in told), ["app.db", "memory.db", "usage.db"])
            self.assertTrue(all(p["what"] and p["at"] for p in told))

    def test_a52_usage_that_cannot_be_read_does_not_stop_a_chat(self):
        with TempHome():
            with mock.patch.object(usage_mod, "_db", side_effect=sqlite3.OperationalError("disk I/O error")):
                snap = usage_mod.snapshot(1)  # Windows holds the file, or the disk fails: no figures, no stop
            self.assertEqual((snap["limits"], snap["tokens"]), ({}, {}))


class AppPieceTests(unittest.TestCase):
    def test_a14_crew_opens_with_a_damaged_app_file_and_a_locked_skill(self):
        with TempHome() as home:
            (home / "app.json").write_text("[1, 2]", encoding="utf-8")
            with mock.patch.object(server.skills, "build_active_pack", side_effect=PermissionError("in use")):
                app = server.App(0, False)
            try:
                self.assertIsInstance(app.pair_token, str)
                self.assertGreaterEqual(len(app.pair_token), 16)
                self.assertEqual(json.loads((home / "app.json").read_text(encoding="utf-8"))["pair_token"],
                                 app.pair_token)
            finally:
                app.chats.shutdown()

    def test_a11_one_failing_upkeep_step_does_not_skip_the_others(self):
        with TempHome():
            checked = threading.Event()
            fake = types.SimpleNamespace(install_anthropic_skills=lambda: None)

            def check():
                checked.set()
                return {"available": False}

            with mock.patch.object(server.claude_cli, "update_if_stale",
                                   side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")), \
                    mock.patch.object(server.launcher, "heal_shortcuts", side_effect=OSError("the icon is in use")), \
                    mock.patch.object(server.updater, "check", side_effect=check):
                server.App.upkeep(fake)
                self.assertTrue(checked.wait(20))

    def test_an_automatic_update_that_fails_is_told_and_not_retried_at_once(self):
        with TempHome():
            events = hub.subscribe("app")
            try:
                fake = types.SimpleNamespace(idle=lambda: True, port=0)
                info = {"checked": time.time(), "latest": "99.0.0", "available": True}
                with mock.patch.object(server.updater, "last_check", return_value=info), \
                        mock.patch.object(server.updater, "install", side_effect=updater.UpdateError("in use")):
                    self.assertFalse(server.App.update_if_quiet(fake))
                    self.assertFalse(server.App.update_if_quiet(fake))  # not again at once
                    self.assertEqual(server.updater.install.call_count, 1)
                names = [p.split("\n", 1)[0] for p in drain(events)]
                self.assertIn("event: update_failed", names)
            finally:
                hub.unsubscribe("app", events)

    def test_a21_a_skill_description_with_a_colon_and_a_hash(self):
        with TempHome() as home:
            when = "writing a report: the board's #1 format, with notes"
            skill = skills_mod.create("Board report", when,
                                      "Open the template, fill in each section, then check the numbers twice.")
            text = (home / "skills" / "skills" / skill["id"] / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f'description: "Use when {when}"', text)
            self.assertEqual(skill["description"], f"Use when {when}")
            urdu = skills_mod.create("رپورٹ لکھنا", "جب بورڈ کے لیے رپورٹ لکھنی ہو",
                                     "سانچہ کھولیں، ہر حصہ بھریں، پھر اعداد دو بار جانچیں۔")
            self.assertTrue(urdu["id"].startswith("skill-"))
            self.assertIn("رپورٹ لکھنا", urdu["body"])

    def test_a22_a_missing_or_silent_phone_program_is_explained(self):
        service = phone_mod.PhoneService()
        with mock.patch.dict(os.environ, {"CREW_ADB": str(Path(tempfile.gettempdir()) / "no-such-adb")}):
            with self.assertRaises(phone_mod.PhoneError) as cm:
                service.devices()
        self.assertIn("could not run", str(cm.exception))
        silent = types.SimpleNamespace(run=mock.Mock(side_effect=subprocess.TimeoutExpired("adb", 25)),
                                       TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(phone_mod, "subprocess", silent):
            with self.assertRaises(phone_mod.PhoneError) as cm:
                service.devices()
        self.assertIn("did not answer in time", str(cm.exception))

    def test_a33_a_swipe_without_its_points_is_explained(self):
        p = phone_mod.PhoneService()
        with mock.patch.object(p, "size", return_value=(1080, 2400)), mock.patch.object(p, "_adb") as adb:
            with self.assertRaises(phone_mod.PhoneError) as cm:  # was a bare "'y1'"
                server.phone_action(p, "swipe", {"x1": 0.5}, "you")
            self.assertIn("start and end", str(cm.exception))
            adb.assert_not_called()
            server.phone_action(p, "swipe", {"x1": 0.5, "y1": 0.8, "x2": 0.5, "y2": 0.2}, "you")
            adb.assert_called_once()

    def test_a38_null_for_the_words_to_type_or_press_counts_as_not_given(self):
        self.assertEqual(server._text_fields({"text": None, "key": 5, "x": 1}, "text", "key"), {"key": "5", "x": 1})
        p = phone_mod.PhoneService()
        with mock.patch.object(p, "_adb") as adb:
            server.phone_action(p, "type", {"text": None}, "you")  # was a TypeError: a 500
            server.phone_action(p, "key", {"key": None}, "you")  # was an AttributeError: a 500
            self.assertEqual(adb.call_count, 2)

    def test_a39_an_odd_connectors_file_from_the_claude_app(self):
        with TempHome() as home:
            desk = home / "claude_desktop_config.json"
            desk.write_text(json.dumps({"mcpServers": {"files": {"command": "npx", "args": 5, "env": "X=1"},
                                                       "web": {"url": "https://example.com/mcp", "headers": [1]}}}),
                            encoding="utf-8")
            with mock.patch.object(connections, "claude_desktop_config", return_value=desk):
                self.assertEqual(sorted(connections.import_claude_desktop()), ["files", "web"])  # was a TypeError
            saved = connections.load()["mcp"]
            self.assertEqual((saved["files"]["args"], saved["files"]["env"], saved["web"]["headers"]), ([], {}, {}))

    def test_a31_browser_problems_in_plain_words(self):
        from crewapp import browser
        for error, words in (("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://pbit.gov.pkk/\nCall log:\n  - x",
                              "No website was found"),
                             ("Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:5173/", "Nothing answered"),
                             ("Locator.click: Timeout 5000ms exceeded.\nCall log:\n  - waiting", "took too long"),
                             ("Page.evaluate: Something odd\nCall log:\n  - x", "could not do that: Something odd")):
            text = browser.plain(Exception(error))
            self.assertIn(words, text)
            self.assertNotIn("Call log", text)

    def test_a27_addresses_typed_in_the_browser_bar(self):
        from crewapp.browser import normalize_address as address
        self.assertEqual(address("localhost:5173"), "http://localhost:5173")  # a team project's preview
        self.assertEqual(address("127.0.0.1:8080/app"), "http://127.0.0.1:8080/app")
        self.assertEqual(address("http://localhost:3000"), "http://localhost:3000")
        self.assertEqual(address("pbit.punjab.gov.pk"), "https://pbit.punjab.gov.pk")
        self.assertEqual(address("https://example.com"), "https://example.com")
        for words in ("investment news Punjab", "weather", "localhost is slow today"):
            self.assertTrue(address(words).startswith("https://www.google.com/search?q="), words)

    def test_a24_attachments_named_like_windows_devices(self):
        with TempHome():
            chats = chat_mod.ChatManager()
            cid = chats.create()["id"]
            names = [chats.save_attachment(cid, n, b"x")["name"]
                     for n in ("CON.txt", "nul", "com1.tar.gz", "رپورٹ.pdf", "report.pdf")]
            self.assertEqual(names, ["file-CON.txt", "file-nul", "file-com1.tar.gz", "رپورٹ.pdf", "report.pdf"])

    def test_a19_a20_workflows(self):
        with TempHome():
            chats = chat_mod.ChatManager()
            flows = workflows_mod.Workflows(types.SimpleNamespace(chats=chats, runs=runs_mod.RunManager()))
            for schedule in ({"kind": "hourly", "every": "often"}, {"kind": "daily", "days": 5},
                             {"kind": "weekly", "time": "08:00", "days": ["Monday"]}):
                with self.assertRaises(ValueError) as cm:
                    flows.create({"name": "Bad", "prompt": "Say hello briefly.", "engine": "claude",
                                  "schedule": schedule})
                self.assertIn("schedule", str(cm.exception))
            with self.assertRaises(ValueError) as cm:  # the ban list, when the workflow is saved, not when it runs
                flows.create({"name": "Cheap", "prompt": "Say hello briefly.", "engine": "claude",
                              "model": "claude-haiku-4-5"})
            self.assertIn("banned", str(cm.exception))
            # A19: deleting the chat of a running workflow ends that run (it no longer blocks the next one).
            w = flows.create({"name": "Morning briefing", "prompt": "Summarise the news.", "engine": "claude"})
            cid = chats.create()["id"]
            rid = flows.db.x("INSERT INTO workflow_runs(workflow_id,started,status,trigger,chat_id) VALUES(?,?,?,?,?)",
                             (w["id"], time.time(), "running", "you", cid))
            flows._running.add(w["id"])
            chats.delete(cid)
            watcher = threading.Thread(target=flows._watch_chat, args=(w["id"], rid, cid), daemon=True)
            watcher.start()
            watcher.join(30)
            self.assertFalse(watcher.is_alive())
            last = flows.runs(w["id"])[0]
            self.assertEqual(last["status"], "failed")
            self.assertIn("deleted", last["summary"])
            self.assertFalse(flows.get(w["id"])["running"])


@unittest.skipUnless(_chromium(), "no Chromium for the browser test")
class BrowserCampaignTests(unittest.TestCase):
    def test_a31_a32_the_shared_browser_explains_itself_and_cannot_be_wedged(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        from crewapp import browser
        os.environ.setdefault("CREW_CHROMIUM", _chromium())
        b = browser.service
        with socket.socket() as sock:  # a port nothing listens on
            sock.bind(("127.0.0.1", 0))
            closed = sock.getsockname()[1]
        try:
            for action, body, words in (("navigate", {"url": f"http://127.0.0.1:{closed}/"}, "Nothing answered"),
                                        ("click", {"text": "words that are nowhere"}, "Nothing on the page says"),
                                        ("press", {"key": "not-a-key"}, "no key called"),
                                        ("click", {"x": [], "y": 0.5}, "as numbers"),
                                        ("scroll", {"dy": float("nan")}, "as a number"),  # it wedged the browser
                                        ("scroll", {"dy": float("inf")}, "as a number")):
                with self.assertRaises(ValueError) as cm:  # the app answers these with 400 and these words
                    server.browser_action(b, action, body, "you")
                self.assertIn(words, str(cm.exception), action)
                self.assertNotIn("Call log", str(cm.exception))
            started = time.time()
            server.browser_action(b, "scroll", {"dy": 300}, "you")  # still answering
            server.browser_action(b, "type", {"text": "x" * 5000}, "assistant")  # at once, not key by key
            self.assertLess(time.time() - started, 20)
        finally:
            b.stop()


class MarkdownSafetyTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page()
    page.goto(sys.argv[1])
    print(json.dumps(page.evaluate('''async (cases) => {
        const ui = await import('/js/ui.js');
        const out = [];
        for (const md of cases) {
            window.pwned = 0;
            const box = document.createElement('div');
            box.innerHTML = ui.markdown(md);
            document.body.append(box);
            const handlers = [...box.querySelectorAll('*')].flatMap((el) => [...el.attributes]
                .filter((a) => a.name.startsWith('on')).map((a) => el.tagName + ' ' + a.name));
            for (const el of box.querySelectorAll('*')) {
                el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                if (el.focus) el.focus();
            }
            await new Promise((r) => setTimeout(r, 100));
            out.push({html: box.innerHTML, handlers, pwned: window.pwned});
            box.remove();
        }
        return out;
    }''', json.loads(sys.argv[3]))))
    b.close()
"""
    ATTACKS = ["[x](https://a.com/?(https://b.com/onmouseover=window.pwned=1//)",
               "![alt (https://b.com/onmouseover=window.pwned=2//](https://img.example/x.png)",
               "[x](https://a.com/(https://b.com/onfocus=window.pwned=3//autofocus/)"]
    NORMAL = {"see [the **plan**](https://example.com/a_b_c) now": 'see <a href="https://example.com/a_b_c" target="_blank" '
                                                                   'rel="noopener">the <strong>plan</strong></a> now',
              "visit https://example.com/x_y_z.": 'visit <a href="https://example.com/x_y_z" target="_blank" '
                                                  'rel="noopener">https://example.com/x_y_z</a>.',
              "**bold** and *it* and `a < b`": "<strong>bold</strong> and <em>it</em> and <code>a &lt; b</code>",
              "(https://example.com/p)": '(<a href="https://example.com/p" target="_blank" rel="noopener">'
                                         'https://example.com/p</a>)'}

    def test_f10_formatted_text_cannot_run_script_in_crew(self):
        """An answer, a report, a skill or a Markdown file shown in Crew: a link written inside a link or inside an
        image\'s description broke out of its attribute, and the rest became an event handler that ran when the
        owner pointed at it (or when it took focus) — script in Crew\'s own page, with all of Crew\'s powers."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        cases = [*self.ATTACKS, *self.NORMAL]
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium(),
                                  json.dumps(cases)], capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        results = json.loads(out.stdout.strip().splitlines()[-1])
        for md, res in zip(cases[:len(self.ATTACKS)], results, strict=False):
            self.assertEqual((res["handlers"], res["pwned"]), ([], 0), (md, res["html"]))
        for (md, want), res in zip(self.NORMAL.items(), results[len(self.ATTACKS):], strict=True):
            self.assertEqual(res["html"], f'<p dir="auto">{want}</p>', md)


class BlockedStorageTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # What a browser does when it is set not to let sites keep data: every use of the storage throws.
    page.add_init_script('''Object.defineProperty(window, "localStorage", {get() {
        throw new DOMException("The operation is insecure.", "SecurityError"); }});''')
    page.goto(sys.argv[1] + "#/settings")
    page.wait_for_timeout(2500)
    print(json.dumps({"errors": errors, "settings": page.evaluate(
        "!!document.querySelector('.view') && document.querySelector('.view').textContent.includes('Settings')")}))
    b.close()
"""

    def test_f11_crew_opens_in_a_browser_that_keeps_no_site_data(self):
        """A browser set not to let sites keep data (Chrome and Edge have the setting; some phone privacy modes
        too): one unguarded read of the panel width stopped the whole app, and the page stayed blank."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["errors"], [])
        self.assertTrue(res["settings"])


class DoubleSendTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    held = {"runs": [], "chats": []}
    def hold(kind):
        def handler(route, request):
            if request.method == "POST":
                held[kind].append(route)  # the answer comes later, as from a busy computer
            else:
                route.continue_()
        return handler
    page.route("**/api/runs", hold("runs"))
    page.route("**/api/chats", hold("chats"))
    out = {}
    for tab, kind in (("team", "runs"), ("claude", "chats")):
        page.goto(sys.argv[1] + "#/new")
        page.wait_for_selector(".product-tabs button[data-p=" + tab + "]")
        page.click(".product-tabs button[data-p=" + tab + "]")
        page.fill(".composer textarea", "Build a one-page website for the investment conference")
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")  # pressed again before Crew has answered
        page.click(".send-btn")       # and the send button too
        page.wait_for_timeout(700)
        out[kind] = len(held[kind])
        for route in held[kind]:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": "t1", "title": "New chat", "messages": [], "engine": "claude"}))
        held[kind].clear()
        page.wait_for_timeout(300)
    print(json.dumps(out))
    b.close()
"""

    def test_f12_a_message_or_a_project_is_sent_once(self):
        """Enter pressed twice (or the send button clicked as well) before Crew answered: the team started twice on
        the same request, using the subscriptions twice over; a first message made two chats, one left empty."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), {"runs": 1, "chats": 1})


class ProjectPollingTests(unittest.TestCase):
    PROBE = r"""
import json, sys, time
from playwright.sync_api import sync_playwright
STATE = {"title": "Bakery site", "running": True, "starting": False, "raw_phase": "build", "phase": "Building",
         "messages": [], "agents": [], "tasks": [], "accounts": []}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    asked, held = [], []
    def state(route, request):
        asked.append(time.time())
        if len(asked) == 2:
            held.append(route)  # this check is slow to answer: the owner sends a message meanwhile
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(STATE))
    page.route("**/api/runs/t1?after=*", state)
    page.route("**/api/runs/t1/say", lambda route, request: route.fulfill(status=200, content_type="application/json",
                                                                           body="{}"))
    page.goto(sys.argv[1] + "#/projects/t1")
    page.wait_for_timeout(2000)
    page.fill(".say-dock textarea", "Please use our brand colours")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    for route in held:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(STATE))
    start = len(asked)
    page.wait_for_timeout(7600)
    print(json.dumps({"checks": len(asked) - start}))
    b.close()
"""

    def test_f13_a_project_page_checks_on_the_team_once_at_a_time(self):
        """A message sent (or Continue pressed) while the page was checking on the team started a second round of
        checks beside the first, for as long as the page stayed open; every such moment added one more."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        checks = json.loads(out.stdout.strip().splitlines()[-1])["checks"]
        self.assertLessEqual(checks, 6)  # every 1.5 s while the team works: about 5 in 7.6 s, not twice that


class WorkflowFormTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    tries = []
    def save(route, request):
        if request.method != "POST":
            return route.continue_()
        tries.append(1)
        if len(tries) == 1:  # the first save is refused
            return route.fulfill(status=400, content_type="application/json",
                                 body=json.dumps({"error": "That schedule cannot be used."}))
        route.continue_()
    page.route("**/api/workflows", save)
    page.goto(sys.argv[1] + "#/workflows")
    page.click("text=New workflow")
    page.fill(".dialog input[type=text]", "Morning briefing")
    page.fill(".dialog textarea", "Summarise the morning news on Punjab's industry, with sources.")
    page.click(".dialog .btn.primary")
    page.wait_for_timeout(800)
    kept = page.evaluate("document.querySelector('.dialog input[type=text]') ? "
                         "[document.querySelector('.dialog input[type=text]').value, "
                         "document.querySelector('.dialog textarea').value] : null")
    if kept:
        page.click(".dialog .btn.primary")  # the owner tries again
        page.wait_for_timeout(1200)
    names = page.evaluate("[...document.querySelectorAll('.wf b')].map((b) => b.textContent)")
    print(json.dumps({"kept": kept, "open": page.evaluate("!!document.querySelector('.dialog')"), "names": names}))
    b.close()
"""

    def test_f14_a_refused_workflow_keeps_what_the_owner_wrote(self):
        """Crew refused to save a workflow (a schedule it cannot use, a subscription removed meanwhile, Crew not
        answering): the form had already closed, so the name, the instructions and the schedule were gone."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
            for w in s.api("GET", "/api/workflows")["workflows"]:  # leave the shared app as it was
                if w["name"] == "Morning briefing":
                    s.api("DELETE", f"/api/workflows/{w['id']}")
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["kept"], ["Morning briefing",
                                       "Summarise the morning news on Punjab's industry, with sources."])
        self.assertFalse(res["open"])
        self.assertIn("Morning briefing", res["names"])


class SettingsFeedbackTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    page.goto(sys.argv[1] + "#/settings/team")
    page.wait_for_selector("details.advanced")
    page.click("details.advanced summary")
    hours = page.locator("input[type=number]").first
    hours.fill("500")  # more than a week: not a time limit Crew accepts
    page.wait_for_timeout(1200)
    said = page.evaluate("[...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent)")
    page.route("**/api/update?refresh=1", lambda route, request: route.abort())  # the check cannot reach Crew
    page.goto(sys.argv[1] + "#/settings/updates")
    page.wait_for_selector("text=Check now")
    page.click("text=Check now")
    page.wait_for_timeout(1000)
    again = page.evaluate("!document.evaluate(\"//button[contains(., 'Check now')]\", document, null, 9, null).singleNodeValue.disabled")
    print(json.dumps({"said": said, "again": again}))
    b.close()
"""

    def test_f15_settings_say_when_a_number_is_refused_and_a_failed_check_can_be_retried(self):
        """A number out of range (500 hours) was dropped without a word, so the owner believed it saved; a failed
        "Check now" left its button switched off until the page was reloaded."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(any("0 to 168" in t for t in res["said"]), res["said"])
        self.assertTrue(res["again"])


class DictationTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    # A speech recogniser as Chrome's behaves: words arrive, and the end comes a moment after stop().
    page.add_init_script('''window.SpeechRecognition = class {
        start() { setTimeout(() => this.onresult && this.onresult({resultIndex: 0,
            results: [Object.assign([{transcript: "please draft the invitation letter"}], {isFinal: true})]}), 50); }
        stop() { setTimeout(() => this.onend && this.onend(), 200); }
        abort() { this.stop(); }
    };''')
    page.goto(sys.argv[1] + "#/chat/" + sys.argv[3])
    page.wait_for_selector(".composer .mic")
    page.click(".composer .mic")
    page.wait_for_timeout(300)
    heard = page.input_value(".composer textarea")
    page.click(".composer .send-btn")  # the owner sends what was dictated
    page.wait_for_timeout(900)
    print(json.dumps({"heard": heard, "after": page.input_value(".composer textarea")}))
    b.close()
"""

    def test_f16_a_dictated_message_does_not_come_back_after_it_is_sent(self):
        """Sending stops the dictation, and the dictation's end arrives a moment later: it wrote the words it had
        heard back into the box the message had just left, inviting the owner to send them twice."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            cid = s.api("POST", "/api/chats", {"engine": "claude"})["id"]
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium(), cid],
                                 capture_output=True, text=True, timeout=120)
            s.api("DELETE", f"/api/chats/{cid}")
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIn("please draft the invitation letter", res["heard"])
        self.assertEqual(res["after"], "")


class ComputerClickTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    clicks = []
    page.route("**/api/computer/status", lambda route, request: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"available": True, "width": 1920, "height": 1080})))
    page.route("**/api/computer/events", lambda route, request: route.fulfill(
        status=200, content_type="text/event-stream",
        body="event: frame\ndata: " + json.dumps({"data": PIXEL, "mime": "image/png"}) + "\n\n"))
    def click(route, request):
        clicks.append(json.loads(request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
    page.route("**/api/computer/click", click)
    page.goto(sys.argv[1] + "#/computer")
    page.wait_for_selector("canvas.screen:not(.hidden)", timeout=15000)
    page.dblclick("canvas.screen")
    page.wait_for_timeout(1000)
    print(json.dumps({"clicks": clicks}))
    b.close()
"""

    def test_f17_a_double_click_on_the_computer_is_one_double_click(self):
        """Each click on the live screen goes to the computer, where two quick ones make a double-click; the page
        then sent a double-click as well: four clicks, so a file opened twice."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        clicks = json.loads(out.stdout.strip().splitlines()[-1])["clicks"]
        self.assertEqual(len(clicks), 2, clicks)  # two clicks at the same place: the computer makes them a double-click
        self.assertFalse(any(c.get("double") for c in clicks))


class LibraryTabTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    held = []
    page.route("**/api/library", lambda route, request: held.append(route))  # the files list is slow today
    page.goto(sys.argv[1] + "#/library")
    page.wait_for_timeout(500)
    page.click(".seg button:has-text('Captures')")
    page.wait_for_timeout(800)
    for route in held:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [
            {"name": "report.docx", "kind": "doc", "where": "A chat", "modified": 0, "size": 10, "origin": "#/chats",
             "url": "/files/x"}]}))
    page.wait_for_timeout(800)
    print(json.dumps({"tab": page.evaluate("document.querySelector('.seg button.on').textContent"),
                      "gallery": page.evaluate("!!document.querySelector('.gallery')"),
                      "files": page.evaluate("!!document.querySelector('.list .li')")}))
    b.close()
"""

    def test_f18_the_library_shows_the_tab_that_is_chosen(self):
        """Switching tabs while the other one was loading: whichever list arrived last was drawn, so the files list
        could appear under the Captures tab."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         {"tab": "Captures", "gallery": True, "files": False})


class RefusedFormTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    out = {}
    # A connected service whose name Crew refuses (a space): the address and the key must still be there.
    page.goto(sys.argv[1] + "#/connections")
    page.click("text=Add a service")
    page.fill(".dialog label:has-text('Name') input", "My CRM")
    page.fill(".dialog input[type=url]", "https://crm.example.com/mcp")
    page.fill(".dialog textarea", "Authorization: Bearer sk-crm-1234567890")
    page.click(".dialog .btn.primary")
    page.wait_for_timeout(800)
    out["service"] = page.evaluate("document.querySelector('.dialog input[type=url]') ? "
                                   "[document.querySelector('.dialog input[type=url]').value, "
                                   "document.querySelector('.dialog textarea').value] : null")
    if out["service"]:
        page.fill(".dialog label:has-text('Name') input", "my-crm")
        page.click(".dialog .btn.primary")
        page.wait_for_timeout(800)
    out["service_saved"] = page.evaluate("[...document.querySelectorAll('.conn b')].some((b) => b.textContent === 'my-crm')")
    # A skill whose first save fails: the procedure the owner wrote must still be there.
    tries = []
    def skill(route, request):
        if request.method != "POST":
            return route.continue_()
        tries.append(1)
        if len(tries) == 1:
            return route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "Busy, try again."}))
        route.continue_()
    page.route("**/api/skills", skill)
    page.goto(sys.argv[1] + "#/skills")
    page.click("text=Create a skill")
    fields = page.locator(".dialog input, .dialog textarea")
    fields.nth(0).fill("Formal letter format")
    fields.nth(1).fill("writing any official letter")
    fields.nth(2).fill("1. Use the department letterhead. 2. Put the reference number and date at the top.")
    page.click(".dialog .btn.primary")
    page.wait_for_timeout(800)
    out["skill"] = page.evaluate("[...document.querySelectorAll('.dialog input, .dialog textarea')].map((x) => x.value)")
    if out["skill"]:
        page.click(".dialog .btn.primary")
        page.wait_for_timeout(1000)
    out["skill_open"] = page.evaluate("!!document.querySelector('.dialog')")
    print(json.dumps(out))
    b.close()
"""

    def test_f19_a_refused_connection_or_skill_keeps_what_the_owner_wrote(self):
        """Crew refused a connected service (a name with a space) or could not save a skill: the form had already
        closed, so the service's address and pasted key, or the whole written procedure, were gone."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=180)
            s.api("DELETE", "/api/connections/mcp/my-crm")  # leave the shared app as it was
            for sk in s.api("GET", "/api/skills")["skills"]:
                if sk["name"].lower().startswith("formal"):
                    s.api("DELETE", f"/api/skills/{sk['id']}")
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["service"], ["https://crm.example.com/mcp", "Authorization: Bearer sk-crm-1234567890"])
        self.assertTrue(res["service_saved"])
        self.assertEqual(res["skill"], ["Formal letter format", "writing any official letter",
                                        "1. Use the department letterhead. 2. Put the reference number and date at the top."])
        self.assertFalse(res["skill_open"])


class ScorecardLabelTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
cell = {"n": 4, "passed": 3, "rate": 0.75, "label": "fair", "minutes": 5, "tokens": 900}
def model(mid, label, tier):
    return {**{k: v for k, v in cell.items() if k != "label"}, "model": mid, "label": label, "tier": tier,
            "verdict": "fair", "by_size": {}, "strong": [],
            "weak": [], "moved_up": 0, "handovers": 0, "wins": 0, "losses": 0,
            "by_kind": {"docs S": {"kind": "docs", "size": "S", **cell}}}
DATA = {"window_days": 180, "records": 12, "projects": 3, "workhorse": "claude-sonnet-5-5", "manager": "claude-opus-5-5",
        "thresholds": {"evidence": 4, "weak": 0.5, "strong": 0.8}, "rules": {"up": [], "down": []}, "contests": [],
        "models": [model("claude-sonnet-5-5", "Sonnet 5.5", "workhorse"), model("gpt-6-sol", "GPT-6 Sol", "workhorse"),
                   model("claude-opus-5-5", "Opus 5.5", "manager")],
        "matrix": [{"kind": "docs", "size": "S", "best": None, "cells": {"claude-sonnet-5-5": cell, "gpt-6-sol": cell,
                                                                         "claude-opus-5-5": cell}}]}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    page.route("**/api/scorecard", lambda route, request: route.fulfill(status=200, content_type="application/json",
                                                                        body=json.dumps(DATA)))
    page.goto(sys.argv[1] + "#/usage/scorecard")
    page.wait_for_selector(".score-tile")
    print(json.dumps(page.evaluate("Object.fromEntries([...document.querySelectorAll('.score-tile .who')].map((w) => "
                                   "[w.querySelector('b').textContent, w.querySelector('.pill').textContent]))")))
    b.close()
"""

    def test_a_model_no_longer_in_its_tier_is_labelled_as_earlier(self):
        """After the owner replaced GPT-6 Sol with Sonnet 5.5, the scorecard (180 days of record) showed both as the
        Workhorse. The record stays; the model that left says so."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         {"Sonnet 5.5": "Workhorse", "GPT-6 Sol": "Earlier workhorse", "Opus 5.5": "Manager"})


class NoticeTimingTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page()
    page.goto(sys.argv[1])
    print(json.dumps(page.evaluate('''async () => {
        const seen = []; const real = window.setTimeout;
        window.setTimeout = (f, ms, ...a) => { seen.push(ms); return real(f, ms, ...a); };
        const ui = await import('/js/ui.js');
        ui.toast('short', {bad: true}); const short = seen.pop();
        ui.toast('This is a long explanation. '.repeat(9), {bad: true}); const long = seen.pop();
        window.setTimeout = real;
        return {short, long};
    }''')))
    b.close()
"""

    def test_f8_a_long_notice_stays_long_enough_to_read(self):
        """Every red notice went after 6.5 seconds, however long: a notice that explains what to do (a refused
        update, a subscription at its limit) was gone before it could be read."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium()],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        delays = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(delays["short"], 6500)
        self.assertGreaterEqual(delays["long"], 250 * 55)  # about a fifth of a second a word

    LONG_CHAT = r"""
import json, sys, time
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2])
    page = b.new_page(viewport={"width": 1280, "height": 650})
    page.goto(sys.argv[1].rsplit("#", 1)[0] + "#/chats")
    page.wait_for_timeout(500)
    page.goto(sys.argv[1])
    page.wait_for_function("document.querySelectorAll('.turn-ai').length > 0", timeout=60000)
    page.wait_for_timeout(300)
    out = {"drawn": page.evaluate("document.querySelectorAll('.turn-ai').length"),
           "button": page.evaluate("(document.querySelector('.thread > .earlier') || {}).textContent || ''")}
    first = page.evaluate("document.querySelector('.thread > .turn-user .bubble').textContent")
    if out["button"]:
        page.click(".thread > .earlier")
        page.wait_for_timeout(500)
    out["after"] = page.evaluate("document.querySelectorAll('.turn-ai').length")
    out["first_still_in_view"] = page.evaluate('''(text) => {
        const el = [...document.querySelectorAll('.turn-user .bubble')].find((b) => b.textContent === text);
        const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight; }''', first)
    print(json.dumps(out))
    b.close()
"""

    def test_f9_a_long_chat_opens_at_once(self):
        """A chat with hundreds of answers drew every one of them before showing anything (and each answer drawn
        looked through all the ones before it): 1,500 answers took six seconds to appear."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        s = AppServer()
        try:
            cid = s.api("POST", "/api/chats", {})["id"]
            for i in range(200):
                s.app.chats.db.add_message(cid, "user", f"Question {i}", {})
                s.app.chats.db.add_message(cid, "assistant", f"Answer {i} with **some** words.", {"engine": "claude"})
            out = subprocess.run([sys.executable, "-c", self.LONG_CHAT, f"http://127.0.0.1:{s.port}/#/chat/{cid}",
                                  _chromium()], capture_output=True, text=True, timeout=180)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        seen = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertLessEqual(seen["drawn"], 60)  # the newest part first …
        self.assertIn("Show 280 earlier messages", seen["button"])
        self.assertEqual(seen["after"], 200)  # … and the rest when asked,
        self.assertTrue(seen["first_still_in_view"])  # without losing one's place


# ====================================================================== updates


class UpdaterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crew-install-"))
        (self.root / "crewlib").mkdir()
        (self.root / "crewlib" / "cli.py").write_text("OLD", encoding="utf-8")
        (self.root / "VERSION.json").write_text(json.dumps({"version": "2.3.0"}), encoding="utf-8")
        (self.root / "requirements-app.txt").write_text("playwright\n", encoding="utf-8")
        self.saved = (updater.ROOT, updater._get)
        updater.ROOT = self.root
        self.zip = self.make_zip("playwright\n")
        updater._get = lambda url, timeout=30: self.zip

    def tearDown(self):
        updater.ROOT, updater._get = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def make_zip(requirements: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Crew-AI-Crew-AI/crew/crewlib/cli.py", "NEW")
            zf.writestr("Crew-AI-Crew-AI/crew/crewapp/extra.py", "EXTRA")
            zf.writestr("Crew-AI-Crew-AI/crew/requirements-app.txt", requirements)
            zf.writestr("Crew-AI-Crew-AI/crew/VERSION.json", json.dumps({"version": "2.3.1"}))
        return buf.getvalue()

    def program(self) -> dict:
        return {p.relative_to(self.root).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_a8_a_locked_file_leaves_the_old_version_whole(self):
        before = self.program()
        locked = self.root / "requirements-app.txt"  # Windows: another program has it open
        real_replace, real_copy = os.replace, shutil.copy2

        def in_use():
            return PermissionError(13, "The process cannot access the file because it is being used by another "
                                       "process", str(locked))

        def replace(src, dst, *a, **k):
            if Path(src) == locked or Path(dst) == locked:
                raise in_use()
            return real_replace(src, dst, *a, **k)

        def copy2(src, dst, *a, **k):
            if Path(dst) == locked:
                raise in_use()
            return real_copy(src, dst, *a, **k)

        with mock.patch.object(updater.os, "replace", replace), mock.patch.object(updater.shutil, "copy2", copy2):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.install()
        self.assertIn("requirements-app.txt", str(cm.exception))
        self.assertEqual(self.program(), before)  # the old version, whole: nothing new, nothing left beside it

    def test_a8_a_download_cut_short_or_no_network_changes_nothing(self):
        before = self.program()
        updater._get = lambda url, timeout=30: self.zip[: len(self.zip) // 2]
        with self.assertRaises(updater.UpdateError):
            updater.install()

        def offline(url, timeout=30):
            raise OSError("Network is unreachable")

        updater._get = offline
        with self.assertRaises(updater.UpdateError) as cm:
            updater.install()
        self.assertIn("could not be downloaded", str(cm.exception))
        self.assertNotIn("unreachable", str(cm.exception))  # A56: Python's words stay in the log
        self.assertEqual(self.program(), before)

    def test_a8_add_ons_that_do_not_install_never_stop_an_update(self):
        self.zip = self.make_zip("playwright\npillow\n")
        pip = types.SimpleNamespace(run=mock.Mock(side_effect=subprocess.TimeoutExpired("pip", 900)),
                                    DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
                                    TimeoutExpired=subprocess.TimeoutExpired)
        with mock.patch.object(updater, "subprocess", pip):
            self.assertEqual(updater.install()["version"], "2.3.1")
        pip.run.assert_called_once()
        self.assertEqual((self.root / "crewlib" / "cli.py").read_text(encoding="utf-8"), "NEW")

    def test_a8_never_two_installs_at_once(self):
        with updater._installing:
            with self.assertRaises(updater.UpdateError):
                updater.install()
        self.assertEqual(updater.install()["version"], "2.3.1")

    def test_a56_an_update_check_that_fails_says_so_plainly(self):
        """The Settings page showed Python's words when the check failed ("Could not reach GitHub: <urlopen error
        [Errno 11001] getaddrinfo failed>"), even when the network was fine and a proxy had answered with a page
        of its own ("…: Expecting value: line 1 column 1 (char 0)"); release notes written as one line of text
        (not a list) broke the Settings page."""
        import urllib.error
        with TempHome(), mock.patch.object(updater, "_get",
                                           side_effect=urllib.error.URLError("[Errno 11001] getaddrinfo failed")):
            err = updater.check()["error"]
        self.assertIn("Could not reach GitHub", err)
        self.assertNotIn("Errno", err)
        with TempHome(), mock.patch.object(updater, "_get", return_value=b"<html>Sign in to the proxy</html>"):
            err = updater.check()["error"]
        self.assertIn("proxy", err)
        self.assertNotIn("Expecting value", err)
        with TempHome(), mock.patch.object(updater, "_get", return_value=json.dumps(
                {"version": "99.0.0", "notes": "Chats move on at a usage limit"}).encode()):
            info = updater.check()
        self.assertEqual((info["available"], info["notes"]), (True, ["Chats move on at a usage limit"]))

    def test_a8_what_an_interrupted_update_left_is_tidied(self):
        (self.root / "crewlib" / "cli.py.new").write_text("HALF", encoding="utf-8")
        (self.root / "crewlib" / "tools.py.old").write_text("TOOLS", encoding="utf-8")  # its file never came back
        updater.install()
        files = self.program()
        self.assertNotIn("crewlib/cli.py.new", files)
        self.assertEqual(files.get("crewlib/tools.py"), "TOOLS")
        self.assertEqual(files["crewlib/cli.py"], "NEW")


# ====================================================================== the app, end to end


class AppCampaignTests(unittest.TestCase):
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

    @staticmethod
    def done(ev: queue.Queue, timeout: float = 60) -> dict:
        while True:
            name, data = ev.get(timeout=timeout)
            if name == "done":
                return data

    def with_chatgpt(self):
        before = self.s.api("GET", "/api/settings")["accounts"]
        self.s.api("PUT", "/api/settings", {"accounts": before + [{"name": "mohidzeeshanrana-gmail.com",
                                                                     "vendor": "codex", "profile": ""}]})
        self.addCleanup(self.s.api, "PUT", "/api/settings", {"accounts": before})

    def test_a2_a3_requests_of_the_wrong_shape_are_refused_plainly(self):
        s = self.s
        for method, path, raw in (("POST", "/api/chats", b"[1, 2]"), ("PUT", "/api/settings", b'"text"'),
                                  ("POST", "/api/runs", b"null")):
            status, _, payload = s.request(method, path, raw=raw, headers={"X-Crew": "1",
                                                                           "Content-Type": "application/json"})
            self.assertEqual(status, 400, (path, payload))
        cid, _ = self.new_chat()
        s.api("PUT", f"/api/chats/{cid}", {"title": 5}, expect=400)
        s.api("POST", f"/api/chats/{cid}/send", {"text": 5}, expect=400)
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "effort": ["high"]}, expect=400)
        s.api("POST", "/api/chats", {"model": 7}, expect=400)
        s.api("PUT", "/api/settings", {"app": ["dark"]}, expect=400)
        s.api("POST", "/api/runs", {"request": 12345}, expect=400)
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a17_every_chat_can_be_found_however_old(self):
        s = self.s
        cid, _ = self.new_chat()
        s.api("PUT", f"/api/chats/{cid}", {"title": "Budget for the Lahore expo"})
        db = s.app.chats.db
        now = time.time()
        for n in range(310):  # months of newer chats
            db.x("INSERT INTO chats(id,title,created,updated,engine) VALUES(?,?,?,?,?)",
                 (f"bulk-{n:04d}", f"Chat {n}", now + 10 + n, now + 10 + n, "claude"))
        try:
            self.assertNotIn(cid, [c["id"] for c in s.api("GET", "/api/chats")["chats"]])
            found = s.api("GET", "/api/chats?q=lahore%20expo")["chats"]
            self.assertEqual([c["id"] for c in found], [cid])
            self.assertEqual(s.api("GET", "/api/chats?q=100%25")["chats"], [])  # a % is a letter, not a wildcard
        finally:
            db.x("DELETE FROM chats WHERE id LIKE 'bulk-%'")
            s.api("DELETE", f"/api/chats/{cid}")

    def test_a18_a_model_of_the_other_product_is_refused(self):
        s = self.s
        cid, _ = self.new_chat()
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "model": "gpt-6-astra"}, expect=400)
        self.assertIn("ChatGPT model", err["error"])
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["messages"], [])  # nothing recorded
        self.with_chatgpt()
        cid2, _ = self.new_chat(engine="codex")
        err = s.api("POST", f"/api/chats/{cid2}/send", {"text": "hi", "model": "claude-opus-5-5"}, expect=400)
        self.assertIn("Claude model", err["error"])

    def test_a16_a_claude_answer_that_cannot_be_finished_still_ends(self):
        s = self.s
        cid, ev = self.new_chat()
        with mock.patch.object(chat_mod.usage_log, "record_tokens",
                               side_effect=sqlite3.OperationalError("database is locked")):
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})
            done = self.done(ev)
        self.assertTrue(done["meta"]["error"])
        self.assertIn("Here is a short answer", done["text"])  # what was said is kept
        self.assertFalse(s.api("GET", f"/api/chats/{cid}")["busy"])
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello again"})  # and the chat carries on
        self.assertFalse(self.done(ev)["meta"]["error"])

    def test_a15_a_chatgpt_answer_that_cannot_be_finished_still_ends(self):
        s = self.s
        self.with_chatgpt()
        cid, ev = self.new_chat(engine="codex")
        with mock.patch.object(chat_mod, "read_codex_status", side_effect=RuntimeError("the session file vanished")):
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
            done = self.done(ev)
        self.assertTrue(done["meta"]["error"])
        self.assertIn("the session file vanished", done["text"])
        self.assertFalse(s.api("GET", f"/api/chats/{cid}")["busy"])

    def test_a15_stopping_a_chatgpt_answer_says_stopped(self):
        s = self.s
        self.with_chatgpt()
        cid, ev = self.new_chat(engine="codex")
        os.environ["CREW_FAKE_SCENARIO"] = json.dumps({"word_delay": 0.005, "codex_delay": 30})
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
        until(lambda: getattr(s.app.chats.sessions.get(cid), "proc", None) is not None, timeout=20, step=0.1)
        time.sleep(0.5)
        s.api("POST", f"/api/chats/{cid}/stop", {})
        done = self.done(ev, timeout=30)
        self.assertEqual(done["text"], "*Stopped.*")
        self.assertTrue(done["meta"]["stopped"])
        self.assertFalse(done["meta"]["error"])

    def test_a15_deleting_a_chatgpt_chat_while_it_answers(self):
        s = self.s
        self.with_chatgpt()
        cid, _ = self.new_chat(engine="codex")
        os.environ["CREW_FAKE_SCENARIO"] = json.dumps({"word_delay": 0.005, "codex_delay": 30})
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
        session = until(lambda: s.app.chats.sessions.get(cid), timeout=20, step=0.1)
        until(lambda: session.proc is not None, timeout=20, step=0.1)
        s.api("DELETE", f"/api/chats/{cid}")
        until(lambda: not session.busy, timeout=30, step=0.2)
        self.assertEqual(s.app.chats.db.q("SELECT id FROM messages WHERE chat_id=?", (cid,)), [])

    def test_a25_no_route_is_hidden_by_an_earlier_one(self):
        import re
        hidden = []
        for i, (method, rx, name) in enumerate(server.ROUTES):
            path = rx.pattern[1:-1]
            if not re.fullmatch(r"[\w/.-]+", path):
                continue  # a pattern, not a plain address
            hidden += [f"{method} {path} ({name}) is answered by {other}" for m, earlier, other in server.ROUTES[:i]
                       if m == method and earlier.match(path)]
        self.assertEqual(hidden, [])
        state = self.s.api("GET", "/api/skills/anthropic")  # the Skills screen follows the install with this
        self.assertIn(state.get("state"), ("idle", "running", "done", "error"))
        # A26: a subscription name with @ reaches its sign-in (here: no such subscription, said plainly)
        err = self.s.api("POST", "/api/accounts/me%40work.pk/login", {}, expect=400)
        self.assertIn("No such subscription", err["error"])

    def test_a28_a_broken_character_is_refused_plainly_and_changes_nothing(self):
        """Half of a character pair ("\\ud800") in any text sent: the connection dropped without an answer, or a
        refused message was kept in the chat anyway."""
        s = self.s
        cid, _ = self.new_chat()
        for method, path, body in (("POST", "/api/chats", {"account": "\ud800"}),
                                   ("POST", f"/api/chats/{cid}/send", {"text": "hello", "model": "\ud800"}),
                                   ("POST", f"/api/chats/{cid}/send", {"text": "hello \ud800"}),
                                   ("PUT", "/api/settings", {"app": {"owner_name": "\ud800"}}),
                                   ("PUT", "/api/settings", {"app": {"\ud800": 1}})):
            status, _, payload = s.request(method, path, raw=json.dumps(body).encode(),
                                           headers={"X-Crew": "1", "Content-Type": "application/json"})
            self.assertEqual(status, 400, (path, payload))
            self.assertIn("broken character", json.loads(payload)["error"])
        self.assertEqual(s.api("GET", f"/api/chats/{cid}")["messages"], [])  # nothing half-recorded
        # A broken character already in a file on disk (a hand edit): the screen still gets its answer.
        path = HOME / "connections.json"
        saved = path.read_bytes() if path.is_file() else None
        try:
            path.write_text('{"mcp": {"notes\\ud800": {"type": "http", "url": "https://example.com/mcp"}}}',
                            encoding="utf-8")
            self.assertEqual([m["name"] for m in s.api("GET", "/api/connections")["mcp"]], ["notes?"])
        finally:
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(saved)
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a29_a30_odd_project_and_capture_addresses(self):
        s = self.s
        long_id = "a" * 300  # "file name too long" for the computer
        for method, path in (("GET", f"/api/runs/{long_id}"), ("POST", f"/api/runs/{long_id}/stop"),
                             ("POST", f"/api/runs/{long_id}/say"), ("POST", f"/api/runs/{long_id}/resume"),
                             ("POST", f"/api/runs/{long_id}/open-folder")):
            status, _, payload = s.request(method, path, {"text": "hello"}, headers={"X-Crew": "1"})
            self.assertLess(status, 500, (path, payload))
        for method, path in (("GET", f"/api/skills/{long_id}"), ("DELETE", f"/api/skills/{long_id}"),
                             ("POST", f"/api/skills/{long_id}/toggle")):
            status, _, payload = s.request(method, path, {"enabled": True}, headers={"X-Crew": "1"})
            self.assertEqual(status, 404 if method != "DELETE" else 200, (path, payload))
        cid, _ = self.new_chat()
        s.api("POST", f"/api/chats/{cid}/attach-capture", {"name": "A" * 300}, expect=404)
        s.api("DELETE", f"/api/chats/{cid}")
        rid = "20260928-120000-odd-numbers"  # A30: a message number too big for the database
        (HOME / "runs" / rid).mkdir(parents=True, exist_ok=True)
        Store(HOME / "runs" / rid / "team.db").close()
        try:
            for after in ("99999999999999999999", "-5", "abc", "1e999"):
                self.assertEqual(s.api("GET", f"/api/runs/{rid}?after={after}")["id"], rid)
        finally:
            store = s.app.runs._stores.pop(rid, None)
            if store is not None:
                store.close()
            shutil.rmtree(HOME / "runs" / rid, ignore_errors=True)

    def test_a34_settings_of_the_wrong_kind_are_refused_before_anything_is_written(self):
        s = self.s
        path = HOME / "crew.toml"
        before = path.read_bytes() if path.is_file() else None
        for body, words in (({"team": {"max_hours": "abc"}}, "must be a number"),  # saved; every project then failed
                            ({"team": {"stall_minutes": 0}}, "more than 0"),  # every agent interrupted non-stop
                            ({"team": {"ceo_reviews": "no"}}, "true or false"),  # "no" counted as yes
                            ({"models": {"banned": [1, 2]}}, "list of names"),  # was a 500
                            ({"models": {"work": 5}}, "must be text"),  # was a 500
                            ({"app": {"phone_enabled": "false"}}, "on or off"),  # text: phone access on at every start
                            ({"app": {"settings_version": 1}}, "Crew's own record")):  # the migrations ran again
            err = s.api("PUT", "/api/settings", body, expect=400)
            self.assertIn(words, err["error"], body)
        self.assertEqual(path.read_bytes() if path.is_file() else None, before)  # nothing written
        self.assertEqual(list(HOME.glob("crew.toml.damaged-*")), [])
        # What the Settings screen sends is saved as before.
        out = s.api("PUT", "/api/settings", {"team": {"max_hours": 2.5},
                                             "app": {"voice_rate": 1.2, "auto_update": False}})
        self.assertEqual((out["team"]["max_hours"], out["app"]["voice_rate"], out["app"]["auto_update"]),
                         (2.5, 1.2, False))
        s.api("PUT", "/api/settings", {"team": {"max_hours": 0}, "app": {"voice_rate": 1.0, "auto_update": True}})

    def test_a40_a_project_that_cannot_start_says_why(self):
        """Before, the project page said "Getting the team ready…" for ever: for a project that does not exist,
        and for one whose program ended before its team began (git missing, a subscription since removed …)."""
        s = self.s
        s.api("GET", "/api/runs/20260101-000000-no-such-project", expect=404)
        before = sorted(p.name for p in runs_mod.runs_dir().iterdir())
        err = s.api("POST", "/api/runs", {"request": "Build a page about Lahore", "accounts": ["an-old-name"]},
                    expect=400)
        self.assertIn("cannot use those subscriptions", err["error"])
        self.assertEqual(sorted(p.name for p in runs_mod.runs_dir().iterdir()), before)  # nothing was started
        rid = "20260928-120000-cannot-start"
        run_dir = runs_mod.runs_dir() / rid
        run_dir.mkdir()
        (run_dir / "app-run.log").write_text("Traceback (most recent call last):\n  ...\nFileNotFoundError: "
                                             "[Errno 2] No such file or directory: 'git'\n", encoding="utf-8")
        s.app.runs.procs[rid] = types.SimpleNamespace(poll=lambda: 1)  # its program has ended
        try:
            state = s.api("GET", f"/api/runs/{rid}")
            self.assertIn("could not start", state["problem"])
            self.assertIn("No such file or directory: 'git'", state["problem"])
        finally:
            s.app.runs.procs.pop(rid, None)
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_a41_a_model_name_with_a_stray_character_breaks_nothing(self):
        """A NUL in the model's name passed the checks: the owner's message was kept unanswered, and the name was
        saved as the chat's model, so every later message in that chat failed with "embedded null byte"."""
        s = self.s
        cid, ev = self.new_chat()
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hello", "model": "a\u0000b"}, expect=400)
        self.assertIn("not a model's name", err["error"])
        chat = s.api("GET", f"/api/chats/{cid}")
        self.assertEqual((chat["messages"], chat["model"]), ([], "claude-opus-5-5"))  # nothing kept
        with mock.patch.object(chat_mod.ClaudeSession, "send", side_effect=ValueError("an odd failure")):
            out = s.api("POST", f"/api/chats/{cid}/send", {"text": "hello"})  # any failure to start is answered
        self.assertEqual(out, {"ok": False, "error": "an odd failure"})
        self.assertTrue(self.done(ev)["meta"]["error"])  # said in the conversation
        self.assertEqual([m["role"] for m in s.api("GET", f"/api/chats/{cid}")["messages"]], ["user", "assistant"])
        s.api("POST", f"/api/chats/{cid}/send", {"text": "hello again"})  # and the chat carries on
        self.assertFalse(self.done(ev)["meta"]["error"])
        s.api("DELETE", f"/api/chats/{cid}")

    def test_c17_finished_answers_let_go_of_their_pipes(self):
        """Each answer's program left its output pipes open until Python happened to collect them: over weeks of
        use, open handles piled up (the tests showed ResourceWarnings)."""
        s = self.s
        self.with_chatgpt()
        started = []
        real = chat_mod._popen

        def recording(*a, **k):
            started.append(real(*a, **k))
            return started[-1]

        with mock.patch.object(chat_mod, "_popen", recording):
            cid, ev = self.new_chat(engine="codex")  # ChatGPT: one program per answer
            s.api("POST", f"/api/chats/{cid}/send", {"text": "hi"})
            self.done(ev)
            cid2, ev2 = self.new_chat()  # Claude: one program for the conversation, until the chat goes
            s.api("POST", f"/api/chats/{cid2}/send", {"text": "hi"})
            self.done(ev2)
        s.api("DELETE", f"/api/chats/{cid2}")
        self.assertEqual(len(started), 2)
        until(lambda: all(p.stdout.closed and p.stderr.closed for p in started), timeout=20, step=0.1)
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a49_a_web_page_under_another_name_cannot_reach_crew(self):
        """DNS rebinding: a web page whose name its maker points at this computer is, to the browser, its own site.
        It could read every chat, setting and file, and change them: the same-site check compared its address with
        itself, and a request from this computer needs no pairing."""
        s = self.s
        evil = {"Host": f"evil.example:{s.port}", "Origin": f"http://evil.example:{s.port}", "X-Crew": "1"}
        for method, path in (("GET", "/api/settings"), ("GET", "/api/chats"), ("GET", "/api/pair"), ("GET", "/"),
                             ("GET", "/pair?t=x")):
            status, _, payload = s.request(method, path, headers=evil)
            self.assertEqual(status, 403, (path, payload[:200]))
        status, _, _ = s.request("POST", "/api/chats", raw=b"{}", headers={**evil, "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIn(b"localhost", s.request("GET", "/", headers=evil)[2])  # it says where Crew is
        for host in (f"localhost:{s.port}", f"LocalHost:{s.port}", f"127.0.0.1:{s.port}", f"[::1]:{s.port}",
                     f"192.168.1.20:{s.port}", "localhost"):  # this computer, and a phone on the home network
            status, _, _ = s.request("GET", "/api/ping", headers={"Host": host})
            self.assertEqual(status, 200, host)

    def test_a50_every_file_that_could_run_as_a_page_is_sandboxed(self):
        """A page the assistant saved as .shtml or .xht, or a feed (.atom, .rss) that styles itself with a script,
        ran with Crew's own rights when the owner opened it: only .html, .htm, .xhtml, .svg, .xml and .js were
        sandboxed. A file that is not a capture was served from the captures folder too."""
        s = self.s
        cid, _ = self.new_chat()
        folder = s.app.chats.workspace(cid)
        page = "<html><script>fetch('/api/settings')</script></html>"
        for name in ("a.shtml", "b.xht", "c.xhtm", "d.atom", "e.rss", "f.xsl", "g.unknownkind", "h.html", "i.svg"):
            (folder / name).write_text(page, encoding="utf-8")
            status, headers, _ = s.request("GET", f"/files/chat/{cid}/{name}")
            self.assertEqual(status, 200, name)
            self.assertIn("sandbox", headers.get("Content-Security-Policy", ""), name)
        (folder / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (folder / "report.pdf").write_bytes(b"%PDF-1.4\n")
        for name in ("photo.png", "report.pdf"):  # pictures and PDFs are not pages: they open as they are
            status, headers, _ = s.request("GET", f"/files/chat/{cid}/{name}")
            self.assertEqual((status, headers.get("Content-Security-Policy")), (200, None), name)
        from crewapp import captures
        (captures.folder() / "20260928-000000-note.html").write_text(page, encoding="utf-8")
        try:
            self.assertEqual(s.request("GET", "/captures/20260928-000000-note.html")[0], 404)
        finally:
            (captures.folder() / "20260928-000000-note.html").unlink()
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a51_a_connection_that_never_finishes_is_let_go(self):
        """With phone access on, anything on the home network could open connections and never finish them: each
        held one of Crew's threads for ever (and a live view whose reader stopped reading blocked for ever)."""
        self.assertTrue(server.Handler.timeout and server.Handler.timeout <= 300)
        with mock.patch.object(server.Handler, "timeout", 1):
            with socket.create_connection(("127.0.0.1", self.s.port), timeout=10) as sock:
                sock.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # never finished
                started = time.time()
                self.assertEqual(sock.recv(1024), b"")  # Crew closed it
                self.assertLess(time.time() - started, 8)

    def test_a53_a_project_whose_record_is_damaged_is_still_shown_and_explained(self):
        """A project's record (team.db) that could not be read hid the project from the list, and its page and
        its Resume button answered "Something went wrong: file is not a database"."""
        s = self.s
        rid = "20260928-120000-damaged-record"
        run_dir = runs_mod.runs_dir() / rid
        run_dir.mkdir()
        junk = b"the disk went wrong here " * 300
        (run_dir / "team.db").write_bytes(junk)
        (run_dir / "request.txt").write_text("Build a page about Lahore's gardens", encoding="utf-8")
        try:
            listed = {r["id"]: r for r in s.api("GET", "/api/runs")["runs"]}
            self.assertIn(rid, listed)  # still in the list …
            self.assertEqual(listed[rid]["title"], "Build a page about Lahore's gardens")
            self.assertEqual(listed[rid]["phase"], "Record damaged")
            state = s.api("GET", f"/api/runs/{rid}")  # … and its page says what happened, in plain words
            self.assertIn("record is damaged", state["problem"])
            err = s.api("POST", f"/api/runs/{rid}/resume", expect=400)
            self.assertIn("record is damaged", err["error"])
            self.assertEqual((run_dir / "team.db").read_bytes(), junk)  # the record itself is left as it was
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_a55_a_refused_model_is_explained_in_plain_words(self):
        """A model outside the allowed list, or on the banned list, was refused with programmer's words:
        "model 'claude-opus-5' is not on the allowed list ['claude-opus-5-5', 'claude-fable-5-1']"."""
        s = self.s
        cid, _ = self.new_chat()
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "model": "claude-opus-5"}, expect=400)["error"]
        self.assertIn("claude-opus-5 is not one of the Claude models allowed in Settings → Models", err)
        self.assertIn("claude-opus-5-5, claude-sonnet-5-5, claude-fable-5-1", err)
        err = s.api("POST", f"/api/chats/{cid}/send", {"text": "hi", "model": "claude-haiku-4-5"}, expect=400)["error"]
        self.assertIn("claude-haiku-4-5 is on your list of banned models", err)
        for text in (err,):
            self.assertNotIn("['", text)
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a57_update_now_waits_for_work_in_progress(self):
        """"Update now" installed and restarted Crew at once: an answer being written was cut off, and a running
        project carried on with old and new program files mixed (the automatic update already waited)."""
        s = self.s
        installs = []
        with mock.patch.object(updater, "install", side_effect=lambda: installs.append(1) or {"version": "9.9.9"}), \
                mock.patch.object(updater, "restart_later", lambda port: None):
            running = [{"id": "r1", "title": "Build a page about Lahore", "running": True}]
            with mock.patch.object(s.app.runs, "list", return_value=running):
                err = s.api("POST", "/api/update", {}, expect=409)["error"]
            self.assertIn("Build a page about Lahore", err)
            self.assertIn("stop it first", err)
            cid, _ = self.new_chat()
            session = s.app.chats.session(s.app.chats.get(cid))
            session.busy = True
            try:
                err = s.api("POST", "/api/update", {}, expect=409)["error"]
            finally:
                session.busy = False
            self.assertIn("answering in a chat", err)
            s.api("DELETE", f"/api/chats/{cid}")
        self.assertEqual(installs, [])  # nothing was installed

    def test_a58_the_library_lists_what_was_made_not_the_tools(self):
        """A chat where the assistant built something (installed its packages, kept a git history) filled the
        Library with thousands of the tools' own files (node_modules, .git objects, Python's caches), and the
        Library walked every one of them each time it opened."""
        s = self.s
        cid, _ = self.new_chat()
        folder = s.app.chats.workspace(cid)
        for rel in ["page.html", "site/index.html", "attachments/photo.png", ".git/objects/ab/cdef0123",
                    "__pycache__/app.cpython-312.pyc"] + [f"node_modules/pkg{i}/index.js" for i in range(300)]:
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text("x", encoding="utf-8")
        mine = sorted(x["name"] for x in s.api("GET", "/api/library")["items"] if x["origin"] == f"#/chat/{cid}")
        self.assertEqual(mine, ["page.html", "site/index.html"])
        s.api("DELETE", f"/api/chats/{cid}")

    def test_a1_a_damaged_settings_file_is_told_to_the_owner(self):
        s = self.s
        path, backup = HOME / "crew.toml", HOME / "crew.toml.bak"
        saved = {p: p.read_bytes() for p in (path, backup) if p.is_file()}
        try:
            path.write_text('[team]\nmode = "sometimes"\n', encoding="utf-8")
            backup.unlink(missing_ok=True)  # no last good copy: the defaults are used
            problem = s.api("GET", "/api/overview")["settings_problem"]
            self.assertEqual(problem["restored"], "the defaults")
            self.assertTrue((HOME / problem["kept"]).is_file())
            self.assertEqual(s.api("GET", "/api/settings")["problem"]["kept"], problem["kept"])
        finally:
            for p in HOME.glob("crew.toml.damaged-*"):
                p.unlink()
            (HOME / "settings-problem.json").unlink(missing_ok=True)
            for p in (path, backup):
                if p in saved:
                    p.write_bytes(saved[p])
                else:
                    p.unlink(missing_ok=True)


# ====================================================================== fifth continuation: tool servers

def _rpc_session(module: str, env: dict, messages: list[dict]) -> dict:
    """Run one of Crew's tool servers over stdio with these messages, as Claude Code or Codex would; its replies by
    id (a server that died answers nothing more)."""
    proc = subprocess.run([sys.executable, "-m", module], input="\n".join(json.dumps(m, ensure_ascii=False)
                                                                            for m in messages).encode("utf-8"),
                          capture_output=True, env=env, timeout=60, cwd=str(ROOT))
    replies = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.strip():
            msg = json.loads(line)
            replies[msg.get("id")] = msg
    return replies


class _AppStandIn:
    """The Crew app's /internal endpoints, recording what the agents' device tools send."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        got = self.got = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
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


class ToolServerTests(unittest.TestCase):
    @staticmethod
    def windows_like_env(**extra) -> dict:
        """What Codex gives a tool server on Windows: only the variables named in its settings, so Python falls back
        to the console's code page (cp1252) for stdin and stdout."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0", PYTHONPATH=str(ROOT), **extra)
        return env

    def test_c31_the_team_tools_read_and_write_utf8_whatever_the_code_page(self):
        """The CEO's runs on ChatGPT (Codex) start the team tools with only a few variables: on Windows the tool server
        then spoke the console's code page. The symbols Crew itself writes into the team chat ("✔", "→") could not be
        sent back, and a letter outside cp1252 in what an agent sent stopped the tool server altogether."""
        d = Path(tempfile.mkdtemp(prefix="crew-utf8-"))
        st = Store(d / "team.db")
        st.upsert_seat("ada", role="lead", vendor="claude", account="claude-1", status="idle")
        st.post("crew", "system", "Task #1 merged into the team's result. ✔")
        st.post("ada", "update", "@all → the owner wrote: add the Łódź office to the chart")
        env = self.windows_like_env(CREW_DB=st.path, CREW_SEAT="ceo-plan", CREW_ROLE="ceo")
        replies = _rpc_session("crewlib.mcp_server", env, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "team_chat_read", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "team_decide", "arguments": {"text": "Łódź stays in — done ✔"}}},
        ])
        self.assertIn(2, replies, "the team tool server died")
        read = replies[2]["result"]
        self.assertFalse(read["isError"], read)
        self.assertIn("✔", read["content"][0]["text"])
        self.assertIn("Łódź", read["content"][0]["text"])
        self.assertIn(3, replies, "the team tool server died reading a letter outside cp1252")
        self.assertFalse(replies[3]["result"]["isError"], replies[3])
        self.assertEqual(st.recent_messages(1)[0]["text"], "Łódź stays in — done ✔")

    def test_c31_the_device_tools_pass_any_language_through_exactly(self):
        """A ChatGPT chat typing on the owner's computer: text outside cp1252 stopped the device tool server (Ł's
        UTF-8 holds the byte 0x81, which cp1252 cannot read), and what it could read arrived garbled."""
        app = _AppStandIn()
        try:
            text = "Grüße aus Łódź → پنجاب ✔"
            replies = _rpc_session("crewapp.devices_mcp", self.windows_like_env(CREW_APP_URL=app.url, CREW_APP_TOKEN="t"), [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "computer_type", "arguments": {"text": text}}},
            ])
        finally:
            app.close()
        self.assertIn(2, replies, "the device tool server died reading the text")
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        self.assertEqual(app.got, [{"text": text}])

    def test_c31_codex_is_told_to_start_the_tool_servers_in_utf8(self):
        """Codex passes a tool server only the variables named in its settings: those settings now carry UTF-8."""
        setup = agents.CodexSetup(model="gpt-6-astra", effort="high", run_dir=Path(tempfile.mkdtemp()), extra_env={})
        args = agents._codex_config_args(setup, "ceo-plan", "ceo", None, read_only=True)
        env = next(a for a in args if a.startswith("mcp_servers.crew_team.env="))
        self.assertIn('PYTHONUTF8 = "1"', env)
        self.assertIn('PYTHONIOENCODING = "utf-8"', env)

    def test_a59_the_device_tools_reach_crew_directly_behind_an_office_proxy(self):
        """With a web proxy set for the computer (an office network), every browser, computer and phone tool of a
        chat or an agent failed: it asked Crew, on this very computer, through the proxy — which cannot reach it."""
        app = _AppStandIn()
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
        self.assertEqual(app.got, [{"url": "example.com"}])


class WindowsCheckShellTests(unittest.TestCase):
    @staticmethod
    def layout(*files: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="crew-win-"))
        for rel in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"")
        return root

    def test_c32_on_windows_the_checks_run_with_gits_bash(self):
        """The agents are told the checks run with bash (Claude Code runs its own commands in the bash that comes with
        Git), and the plan's own example is `test -s report.md`; on Windows the checks ran in cmd.exe instead, where
        such a check can never pass, so every piece of work was sent back until the project stopped."""
        root = self.layout("Program Files/Git/cmd/git.exe", "Program Files/Git/bin/bash.exe",
                           "Program Files/Git/mingw64/bin/git.exe", "Windows/System32/bash.exe",
                           "Users/Mohid Zeeshan/AppData/Local/Programs/Git/bin/bash.exe")
        git_bash = str(root / "Program Files/Git/bin/bash.exe")
        self.assertEqual(gitops.git_bash(str(root / "Program Files/Git/cmd/git.exe"), {}), git_bash)
        self.assertEqual(gitops.git_bash(str(root / "Program Files/Git/mingw64/bin/git.exe"), {}), git_bash)
        # Git not on PATH (the installer has not refreshed it): where Git for Windows installs itself
        self.assertEqual(gitops.git_bash(None, {"ProgramFiles": str(root / "Program Files")}), git_bash)
        per_user = str(root / "Users/Mohid Zeeshan/AppData/Local/Programs/Git/bin/bash.exe")
        self.assertEqual(gitops.git_bash(None, {"LOCALAPPDATA": str(root / "Users/Mohid Zeeshan/AppData/Local")}),
                         per_user)
        # the path Claude Code itself is told to use comes first
        self.assertEqual(gitops.git_bash(str(root / "Program Files/Git/cmd/git.exe"),
                                         {"CLAUDE_CODE_GIT_BASH_PATH": per_user}), per_user)
        # never System32's bash.exe: that one starts Linux (WSL), not the project's folder on Windows
        self.assertIsNone(gitops.git_bash(None, {"CLAUDE_CODE_GIT_BASH_PATH": str(root / "Windows/System32/bash.exe")}))
        self.assertIsNone(gitops.git_bash(None, {}))
        with mock.patch.object(gitops, "git_bash", lambda *a, **k: git_bash):
            self.assertEqual(gitops.check_shell(windows=True), git_bash)
        self.assertEqual(gitops.check_shell(windows=False), shutil.which("bash"))


class _GoneAway(io.RawIOBase):
    """The answer's connection when the other side has already hung up (a closed tab, a phone that left the Wi-Fi)."""

    def writable(self):
        return True

    def write(self, b):
        raise ConnectionAbortedError(10053, "An established connection was aborted by the software in your host machine")


class ServerRobustnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(ENV)
        cls.s = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_a60_a_pairing_link_or_cookie_with_an_odd_character_is_just_not_paired(self):
        """A pairing link mangled on the way to the phone (a letter with an accent, as a keyboard may autocorrect),
        or a cookie holding one, stopped Crew's answer with an error: the codes were compared in a way that cannot
        handle such letters. It is simply a wrong code: the pairing help page, or "not paired"."""
        s = self.s
        status, _, body = s.request("GET", "/pair?t=%C3%A9t%C3%A9")
        self.assertEqual(status, 200)
        self.assertIn(b"Pair this device", body)
        ips = s.app.lan_ips()
        if not ips:
            self.skipTest("no network address on this machine")
        s.api("POST", "/api/phone-access", {"enabled": True})
        self.addCleanup(s.api, "POST", "/api/phone-access", {"enabled": False})
        status, _, _ = s.request("GET", "/api/overview", headers={"Cookie": "crew_token=\u00e9t\u00e9"}, host=ips[0])
        self.assertEqual(status, 401)
        status, _, body = s.request("GET", "/", headers={"Cookie": "crew_token=\u00e9t\u00e9"}, host=ips[0])
        self.assertIn(b"Pair this device", body)

    def test_a61_a_viewer_that_hangs_up_mid_answer_leaves_no_error_behind(self):
        """Closing a tab or a phone's browser while Crew answers is normal. Each time, app.log got two error reports
        (Crew even tried to send an error page down the closed connection), burying the ones that matter."""
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command, handler.request_version = "/api/ping", "GET", "HTTP/1.1"
        handler.requestline, handler.client_address = "GET /api/ping HTTP/1.1", ("127.0.0.1", 50000)
        handler.headers = http.client.parse_headers(io.BytesIO(b"Host: localhost\r\n\r\n"))
        handler.wfile, handler._head, handler.close_connection = _GoneAway(), False, False
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            handler.do_GET()  # nothing escapes
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(handler.close_connection)
        # the same when the viewer hangs up before asking anything (the server's own report of it)
        for gone in (ConnectionResetError(104, "Connection reset by peer"), BrokenPipeError(32, "Broken pipe")):
            try:
                raise gone
            except ConnectionError:
                with mock.patch.object(sys, "stderr", err):
                    self.s.httpd.handle_error(None, ("192.168.1.20", 50000))
        self.assertEqual(err.getvalue(), "")
        try:  # a real fault is still reported
            raise RuntimeError("a real fault")
        except RuntimeError:
            with mock.patch.object(sys, "stderr", err):
                self.s.httpd.handle_error(None, ("127.0.0.1", 50000))
        self.assertIn("a real fault", err.getvalue())

    def test_a67_a_name_made_of_dots_is_not_a_project(self):
        """"/api/runs/.." answered as if a project were starting there: the id stood for Crew's own folder (and "." for
        the projects folder). Nothing was read or changed, but a name made only of dots is no project's (the
        captures already refused such names)."""
        s = self.s
        for rid in ("..", "."):
            status, _, payload = s.request("GET", f"/api/runs/{rid}")
            self.assertEqual(status, 404, (rid, payload))
            self.assertFalse(runs_mod._run_dir(rid))
        status, _, _ = s.request("POST", "/api/runs/../resume", raw=b"{}",
                                 headers={"X-Crew": "1", "Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_a68_a_broken_character_does_not_cut_a_live_view(self):
        """Half of a character pair (an emoji cut in two, as a web page's shortened title or a damaged file may hold)
        in one live event stopped that live view for everyone watching: the event could not be written, Crew wrote an
        error into the middle of the stream, and nothing more reached the screen until it reconnected."""
        s = self.s
        cid = s.api("POST", "/api/chats", {})["id"]
        ev = s.events(f"/api/chats/{cid}/events")
        hub.publish(f"chat:{cid}", "delta", {"text": "Great deals \ud83c"})
        hub.publish(f"chat:{cid}", "delta", {"text": "and the next words"})
        got = [ev.get(timeout=10) for _ in range(2)]
        self.assertEqual(got, [("delta", {"text": "Great deals ?"}), ("delta", {"text": "and the next words"})])

    def test_a63_a_quoted_argument_with_a_space_stays_whole(self):
        """The owner's own folder has a space in it (C:\\Users\\Mohid Zeeshan). A connection given that folder,
        typed on one line in quotes as the guides show, was cut in two at the space and could not start."""
        from crewlib import connections as conn_mod

        folder = r"C:\Users\Mohid Zeeshan\Documents"
        s = self.s
        s.api("POST", "/api/connections/mcp", {
            "name": "files", "type": "stdio", "command": "npx",
            "args": f'-y @modelcontextprotocol/server-filesystem "{folder}" --root=\'D:\\My Work\' plain'})
        self.addCleanup(conn_mod.remove_mcp, "files")
        self.assertEqual(conn_mod.load()["mcp"]["files"]["args"],
                         ["-y", "@modelcontextprotocol/server-filesystem", folder, "--root=D:\\My Work", "plain"])
        self.assertEqual(conn_mod.split_args(r"C:\tools\server.js  --port 8080 "),
                         [r"C:\tools\server.js", "--port", "8080"])  # backslashes are folder separators
        self.assertEqual(conn_mod.split_args('""'), [""])
        self.assertEqual(conn_mod.split_args('"unclosed folder'), ["unclosed folder"])
        self.assertEqual(conn_mod.split_args("  "), [])


class _PhoneFiles:
    """A phone's own storage, as the commands Crew sends see it. The screen listing fails the way Android's does
    while a video plays or an animation runs ("could not get idle state"): no new file is written."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.screen: str | None = "<?xml version='1.0'?><hierarchy><node text=\"Send\" clickable=\"true\" " \
                                  "bounds=\"[900,2200][1060,2320]\" /></hierarchy>"

    def adb(self, *args: str, **_kw):
        if args[:2] == ("shell", "rm"):
            self.files.pop(args[-1], None)
        elif args[:2] == ("shell", "uiautomator"):
            if self.screen is None:
                return "ERROR: could not get idle state.\n"
            self.files[args[-1]] = self.screen
            return f"UI hierchary dumped to: {args[-1]}\n"
        elif args[:2] == ("shell", "cat"):
            if args[-1] not in self.files:
                raise phone_mod.PhoneError(f"cat: {args[-1]}: No such file or directory")
            return self.files[args[-1]]
        return ""


class PhoneScreenTests(unittest.TestCase):
    def test_a65_a_screen_that_cannot_be_listed_is_never_answered_with_an_earlier_one(self):
        """While a video plays or something moves on the phone, Android cannot list the screen and writes no new
        list. Crew then read the list left from an earlier screen: the assistant was told that screen was showing,
        and "tap Send" tapped where Send had been. A list cut off half-way stopped with a "syntax error"."""
        phone, files = phone_mod.PhoneService(), _PhoneFiles()
        with mock.patch.object(phone, "_adb", files.adb), mock.patch.object(phone, "size", lambda: (1080, 2400)), \
                mock.patch.object(phone, "tap", lambda *a, **k: {"ok": True}) as tap:
            self.assertEqual([e["text"] for e in phone.elements()], ["Send"])
            files.screen = None  # a video starts playing
            for attempt in (phone.elements, lambda: phone.tap_text("Send")):
                with self.assertRaises(phone_mod.PhoneError) as caught:
                    attempt()
                self.assertIn("screenshot", str(caught.exception))
            files.screen = "<?xml version='1.0'?><hierarchy><node text=\"Chats\" bounds=\"[0,0]["  # cut off
            with self.assertRaises(phone_mod.PhoneError) as caught:
                phone.elements()
            self.assertIn("screenshot", str(caught.exception))
        del tap


class KeysFileTests(unittest.TestCase):
    def test_a66_a_key_saves_when_the_keys_file_was_last_saved_by_notepad(self):
        """secrets.env edited by hand and saved by Notepad in Windows' older encoding (a note with "é" or "£" in it):
        every key the owner then added or removed in Settings was refused with a message about a "codec". The key is
        saved, and the owner's own lines stay exactly as they were written."""
        with TempHome():
            path = settings_mod.secrets_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# Cl\xe9s de Mohid \xa3\r\nOPENAI_API_KEY=sk-abc123456789\r\n")
            settings_mod.save_secret("WEATHER_KEY", "w-123456789")
            self.assertIn(b"# Cl\xe9s de Mohid \xa3\n", path.read_bytes())  # byte for byte
            self.assertEqual(load_env_file(path), {"OPENAI_API_KEY": "sk-abc123456789", "WEATHER_KEY": "w-123456789"})
            settings_mod.save_secret("WEATHER_KEY", None)
            self.assertEqual(path.read_bytes(), b"# Cl\xe9s de Mohid \xa3\nOPENAI_API_KEY=sk-abc123456789\n")


class FileNameTests(unittest.TestCase):
    def test_c33_files_named_in_urdu_or_with_spaces_are_named_plainly(self):
        """Git writes a name in another alphabet as escapes in quotes ("b/\\330\\261…py"). Crew's scan took that for
        the name: its type ended in a quote, so the checks for Python never read an Urdu-named Python file, and the
        reviewer, the report and the refiner saw escapes where the name should be. A clash in a file whose name has a
        space in it was reported as two files."""
        from crewlib import quality

        repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-repo-")))
        (repo / "My Report.md").write_text("base\n", encoding="utf-8")
        gitops.commit_all(repo, "base")
        base = gitops.head(repo)
        gitops.git(repo, "checkout", "-q", "-b", "task")
        (repo / "رپورٹ.py").write_text("import subprocess\nsubprocess.run(cmd, shell=True)\n", encoding="utf-8")
        (repo / "My Report.md").write_text("from the task\n", encoding="utf-8")
        gitops.commit_all(repo, "task")
        scan = quality.scan(repo, base)
        self.assertEqual([(f.file, f.line, f.what) for f in scan.findings],
                         [("رپورٹ.py", 2, "runs a shell command with shell=True")])
        self.assertEqual(sorted(gitops.changed_files(repo, base, "task")), ["My Report.md", "رپورٹ.py"])
        self.assertIn("رپورٹ.py", gitops.diffstat(repo, base, "task"))
        gitops.git(repo, "checkout", "-q", "--detach", base)
        (repo / "My Report.md").write_text("from newer work\n", encoding="utf-8")
        gitops.commit_all(repo, "newer")
        result = gitops.merge_into(repo, "task", "merge the task")
        self.assertEqual(result.conflicts, ["My Report.md"])


class StreamResyncTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
# The page's live connection, under the test's control: it can drop and come back, as over a phone's Wi-Fi.
FAKE = '''(() => {
  const all = [];
  class Stream extends EventTarget {
    constructor(url) { super(); this.url = String(url); this.readyState = 0; all.push(this); setTimeout(() => this.up(), 20); }
    up() { this.readyState = 1; const e = new Event('open'); if (this.onopen) this.onopen(e); this.dispatchEvent(e); }
    down() { this.readyState = 0; const e = new Event('error'); if (this.onerror) this.onerror(e); this.dispatchEvent(e); }
    send(name, data) { this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) })); }
    close() { this.readyState = 2; }
  }
  window.EventSource = Stream;
  window.__streams = all;
})();'''
base, chrome, cid = sys.argv[1], sys.argv[2], sys.argv[3]
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome)
    page = b.new_page(viewport={"width": 1280, "height": 800})
    page.add_init_script(FAKE)
    page.goto(base + "#/chat/" + cid)
    page.wait_for_selector(".composer textarea")
    ch = "window.__streams.filter((s) => s.url.includes('/api/chats/%s/events')).pop()" % cid
    page.wait_for_function(ch + ".readyState === 1")
    # 1. The answer is written while the page hears nothing (the Wi-Fi dropped for a moment), then it reconnects
    page.fill(".composer textarea", "hello")
    page.keyboard.press("Enter")
    page.wait_for_selector(".send-btn.stop")
    for _ in range(150):  # Crew has finished and kept the answer
        if page.evaluate("fetch('/api/chats/%s').then((r) => r.json()).then((c) => !c.busy && c.messages.length === 2)"
                         % cid):
            break
        page.wait_for_timeout(200)
    page.evaluate(ch + ".down()")
    page.wait_for_timeout(300)
    page.evaluate(ch + ".up()")
    page.wait_for_timeout(2500)
    out["blip_still_waiting"] = page.locator(".send-btn.stop").count() > 0
    out["blip_answer_shown"] = "That is all." in page.inner_text(".thread")
    # Crew marks the chat free a moment before it sends "done": that "done" may come after the page caught up
    last = page.evaluate("fetch('/api/chats/%s').then((r) => r.json()).then((c) => c.messages[c.messages.length - 1])" % cid)
    answers = page.locator(".turn-ai").count()
    page.evaluate(ch + ".send('done', {id: %d, text: %s, meta: {}})" % (last["id"], json.dumps(last["text"])))
    page.wait_for_timeout(300)
    out["blip_answer_shown_once"] = page.locator(".turn-ai").count() == answers
    # 2. Crew restarts in the middle of an answer and is back at once: that answer is gone, nothing more will come
    page.evaluate(ch + ".send('start', {engine: 'claude'})")
    page.evaluate(ch + ".send('delta', {text: 'Working on the second answer'})")
    page.wait_for_selector(".send-btn.stop")
    page.evaluate(ch + ".down()")
    page.wait_for_timeout(300)
    page.evaluate(ch + ".up()")
    page.wait_for_timeout(2500)
    out["restart_still_waiting"] = page.locator(".send-btn.stop").count() > 0
    out["restart_told"] = "interrupted" in page.inner_text(".thread")
    print(json.dumps(out))
    b.close()
"""

    def test_f20_after_the_connection_comes_back_the_chat_shows_where_things_are(self):
        """The chat page asked Crew where the answer stood only after its connection had failed twice in a row. Crew
        restarting (an update) or a phone's Wi-Fi dropping for a moment reconnects at the first try: an answer that
        finished meanwhile never appeared, and one that was lost left the page waiting for ever."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        os.environ.update(ENV)
        s = AppServer()
        try:
            cid = s.api("POST", "/api/chats", {})["id"]
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium(), cid],
                                 capture_output=True, text=True, timeout=180)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         {"blip_still_waiting": False, "blip_answer_shown": True, "blip_answer_shown_once": True,
                          "restart_still_waiting": False, "restart_told": True})


class RightToLeftTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
base, chrome, cid = sys.argv[1], sys.argv[2], sys.argv[3]
ANSWER = ("کریو ایک ٹیم ہے جو آپ کے لیے کام کرتی ہے۔\n\n- پہلا نکتہ: یہ تیز ہے!\n- دوسرا نکتہ: Crew 2.3 میں نیا کیا ہے؟\n\n"
          "| نام | Value |\n|---|---|\n| رفتار | Fast |\n\nAn English line at the end.")
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome)
    page = b.new_page(viewport={"width": 1280, "height": 650})
    page.goto(base + "#/chat/" + cid)
    page.wait_for_selector(".turn-user .bubble")
    page.evaluate("(md) => import('/js/ui.js').then((ui) => { const d = document.createElement('div'); d.className = 'md';"
                  " d.id = 'answer'; d.innerHTML = ui.markdown(md); document.querySelector('.thread').append(d); })", ANSWER)
    page.fill(".composer textarea", "اگلا سوال")
    rtl = lambda sel: page.evaluate("(s) => [...document.querySelectorAll(s)].map((e) => e.matches(':dir(rtl)'))", sel)
    out = {"titles": rtl(".title-btn .ellipsis") + rtl("#recents a .t"),
           "message": rtl(".turn-user .bubble"), "paragraphs": rtl("#answer p"), "items": rtl("#answer li"),
           "cells": rtl("#answer td"), "typing": rtl(".composer textarea"),
           "bullet_on_the_right": page.evaluate("(() => { const li = document.querySelector('#answer li');"
               " const r = li.getBoundingClientRect(), ul = li.parentElement.getBoundingClientRect();"
               " return ul.right - r.right > 10 && r.left - ul.left < 2; })()")}
    # A project's conversation: the owner writes to the team in Urdu, and an agent answers in Urdu
    state = {"title": "Bakery site", "running": True, "starting": False, "raw_phase": "build", "phase": "Building",
             "agents": [], "tasks": [], "accounts": [], "messages": [
                 {"id": 1, "t": 1, "who": "you", "kind": "chat", "text": "Use our brand colours: سبز اور سفید",
                  "original": "ہمارے برانڈ کے رنگ استعمال کریں: سبز اور سفید"},
                 {"id": 2, "t": 2, "who": "lead", "kind": "decision", "text": "ٹھیک ہے! Header سبز ہوگا۔"}]}
    page.route("**/api/runs/t1?after=*", lambda route: route.fulfill(status=200, content_type="application/json",
                                                                      body=json.dumps(state)))
    page.goto(base + "#/projects/t1")
    page.wait_for_selector(".tmsg .body")
    out["team"] = rtl(".tmsg .body")
    out["own_words"] = rtl(".own-words div")
    out["team_box"] = page.evaluate("document.querySelector('.say-dock textarea').getAttribute('dir')")
    print(json.dumps(out))
    b.close()
"""

    def test_f22_urdu_reads_right_to_left_with_english_words_in_place(self):
        """Urdu in a message or an answer was laid out as English is: left to right. A sentence with an English
        word or a number in it came out in the wrong order ("دوسرا نکتہ: Crew 2.3 میں نیا کیا ہے؟" read back to
        front), its "!" and "?" stood at the start, and list bullets sat on the wrong side. Each paragraph, list item,
        table cell and message now takes its direction from its own text; English stays left to right."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        os.environ.update(ENV)
        s = AppServer()
        try:
            cid = s.api("POST", "/api/chats", {})["id"]
            s.api("POST", f"/api/chats/{cid}/send", {"text": "کریو کیا ہے؟ مجھے Crew 2.3 کے بارے میں بتائیں۔"})
            until(lambda: not s.api("GET", f"/api/chats/{cid}")["busy"], timeout=30)
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium(), cid],
                                 capture_output=True, text=True, timeout=120)
        finally:
            s.stop()
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         {"titles": [True, True],
                          "message": [True], "paragraphs": [True, False], "items": [True, True], "cells": [True, False],
                          "typing": [True], "bullet_on_the_right": True,
                          "team": [False, True], "own_words": [True], "team_box": "auto"})


class UpdateReconnectTests(unittest.TestCase):
    PROBE = r"""
import json, sys
from playwright.sync_api import sync_playwright
base, chrome, mode, other = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
out = {}
gone = lambda route: route.abort()  # Crew is away at its own address, installing
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome)
    page = b.new_page(viewport={"width": 1280, "height": 800})
    if mode == "app":
        page.route("**/api/update", lambda route: route.fulfill(status=200, content_type="application/json", body="{}")
                   if route.request.method == "POST" else route.continue_())
        page.goto(base + "#/new")
        page.wait_for_selector(".composer textarea")
        page.evaluate("import('/app.js').then((m) => m.installUpdate({latest: '9.9.9'}))")
        page.route(base + "api/ping", gone)
        page.wait_for_timeout(9000)
        out["went_to_another_program"] = page.url.startswith("http://127.0.0.1:%d" % other)
        # Crew is back at its address on its earlier version (the update could not be installed): a new process
        page.evaluate("window.__before_restart = true")
        page.unroute(base + "api/ping", gone)
        page.route(base + "api/ping", lambda route: route.fulfill(status=200, content_type="application/json",
                   body=json.dumps({"crew": True, "version": "2.3.0", "pid": 1, "port": 0})))
        page.wait_for_timeout(6000)
        out["reopened"] = page.url.startswith(base) and page.evaluate("window.__before_restart === undefined")
    else:  # the page shown while Crew is not running: it looks for Crew on its other ports
        page.route(base + "api/ping", gone)
        page.goto(base + "offline.html")
        page.wait_for_timeout(4500)
        out["went_to"] = page.url.split("/")[2]
    print(json.dumps(out))
    b.close()
"""

    @staticmethod
    def free_ports() -> list[int]:
        found = []
        for port in range(8765, 8775):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            found.append(port)
        return found

    def test_f21_after_an_update_only_crew_is_taken_for_crew(self):
        """After an update the page looks for Crew on its other ports, where it cannot read the answer: whatever
        answered there was taken for Crew (another program, or a web page an agent was testing), and the owner was
        sent to it. A Crew back on its earlier version (the update could not be installed) was not recognised: the
        owner watched "Updating Crew…" for six minutes."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        ports = self.free_ports()
        if len(ports) < 2:
            self.skipTest("Crew's own ports are taken on this machine")
        other_program, crew_again = ports[0], ports[1]
        site = Path(tempfile.mkdtemp(prefix="crew-other-"))
        (site / "index.html").write_text("<h1>Another program</h1>", encoding="utf-8")
        web = subprocess.Popen([sys.executable, "-m", "http.server", str(other_program), "--bind", "127.0.0.1"],
                               cwd=site, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(web.wait)
        self.addCleanup(web.kill)
        os.environ.update(ENV)
        s = AppServer()
        self.addCleanup(s.stop)

        def answering() -> bool:
            try:
                socket.create_connection(("127.0.0.1", other_program), timeout=1).close()
                return True
            except OSError:
                return False

        until(answering, timeout=10)

        def probe(mode: str) -> dict:
            out = subprocess.run([sys.executable, "-c", self.PROBE, f"http://127.0.0.1:{s.port}/", _chromium(), mode,
                                  str(other_program)], capture_output=True, text=True, timeout=180)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            return json.loads(out.stdout.strip().splitlines()[-1])

        self.assertEqual(probe("app"), {"went_to_another_program": False, "reopened": True})
        self.assertEqual(probe("offline"), {"went_to": f"127.0.0.1:{s.port}"})  # still looking
        again = server.CrewServer(("127.0.0.1", crew_again), server.Handler)  # Crew, back on another of its ports
        threading.Thread(target=again.serve_forever, daemon=True).start()
        self.addCleanup(again.server_close)
        self.addCleanup(again.shutdown)
        self.assertEqual(probe("offline"), {"went_to": f"127.0.0.1:{crew_again}"})


if __name__ == "__main__":
    unittest.main()
