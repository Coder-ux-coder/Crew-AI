"""Owner-facing autonomy regressions, using isolated records and real git worktrees."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crewapp import chat, runs, settings
from crewlib import agents, config, gitops, tools
from crewlib.context import project_context
from crewlib.orchestrator import Orchestrator
from crewlib.store import Store
from crewlib.util import Redactor


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crew-autonomy-")
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"CREW_HOME": str(self.root), "CREW_AUTO_CODEX": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def store(self, path=None):
        st = Store(path or self.root / "team.db")
        self.addCleanup(st.close)
        return st

    def configured(self):
        settings.save({"accounts": [{"name": "claude-1", "vendor": "claude", "profile": "default"},
                                    {"name": "codex-1", "vendor": "codex", "profile": "default"}]})
        return config.load(str(settings.path()))


class ModelAndSettings(Isolated):
    def test_mixed_workers_and_codex_lead(self):
        cfg = self.configured()
        workers = [s for s in cfg.seats if s.tier == "workhorse"]
        self.assertEqual({(s.vendor, s.model) for s in workers},
                         {("claude", "claude-sonnet-5-5"), ("codex", "gpt-6.1-sol")})
        st = settings.save({"models": {"work": "gpt-6-astra"}, "seats": []})
        self.assertEqual((st["seats"][0]["vendor"], st["seats"][0]["role"]), ("codex", "lead"))

    def test_optional_backup_does_not_reset_owner_settings(self):
        raw = {"models": {"allowed": ["claude-opus-5-5", "gpt-6-astra", "gpt-6-sol", "claude-sonnet-5-5"],
                           "ceo_backup": "claude-fable-5-1"},
               "app": {"theme": "dark", "owner_name": "Owner", "settings_version": 4},
               "account": [{"name": "office", "vendor": "claude"}]}
        settings.path().write_text(settings.dump(raw), encoding="utf-8")
        result = settings.load()
        self.assertEqual((result["app"]["theme"], result["app"]["owner_name"]), ("dark", "Owner"))
        self.assertEqual(result["accounts"][0]["name"], "office")
        self.assertEqual(result["models"]["ceo_backup"], "")
        self.assertIsNone(settings.problem())
        self.assertFalse(list(self.root.glob("*.damaged-*")))

    def test_recover_preserves_new_settings_and_archived_original(self):
        import json
        archived = self.root / "crew.toml.damaged-20260930-200017"
        archived.write_text(settings.dump({"models": {"allowed": ["claude-opus-5-5", "claude-sonnet-5-5"]},
                                            "app": {"owner_name": "Recovered", "theme": "dark"}}), encoding="utf-8")
        original = archived.read_bytes()
        settings.save({"app": {"owner_name": "New choice"}})
        settings.problem_path().write_text(json.dumps({"kept": archived.name, "at": time.time()}))
        result = settings.recover()
        self.assertEqual(result["app"]["owner_name"], "Recovered")
        self.assertEqual(archived.read_bytes(), original)
        self.assertIn("New choice", settings.path().with_suffix(".toml.bak").read_text())
        self.assertIsNone(settings.problem())

    def test_explicit_codex_seat_survives_account_edit_and_save(self):
        cfg = self.configured()
        seats = [{k: getattr(s, k) for k in s.__dataclass_fields__} for s in cfg.seats]
        settings.save({"seats": seats})
        st = settings.save({"accounts": settings.load()["accounts"] + [{"name": "new", "vendor": "claude"}]})
        self.assertEqual(st["seats"], seats)
        before = settings.path().read_bytes()
        with self.assertRaises(ValueError):
            settings.save({"seats": [{**seats[0], "model": "gpt-6.1-sol"}]})
        self.assertEqual(settings.path().read_bytes(), before)


class ContextAndAuthority(Isolated):
    def team(self):
        st = self.store()
        st.set("phase", "build")
        st.set("goal", "Original owner's goal")
        st.set("settings", {"chat_budget": 0})
        st.upsert_seat("ada", role="lead", tier="manager", vendor="claude", model="claude-opus-5-5")
        st.upsert_seat("worker", role="member", tier="workhorse", vendor="codex", model="gpt-6.1-sol")
        return st

    def test_private_history_is_retained_and_separated(self):
        st = self.team()
        old = st.post("you", "direct", "Remember the cobalt palette", recipient="ceo")
        st.post("ceo", "direct", "Cobalt retained", recipient="you")
        st.post("you", "direct", "Private worker question", recipient="worker")
        for i in range(80):
            st.post("ada", "update", f"Public update {i}")
        ceo = project_context(st, "ceo", 12_000)
        self.assertIn("cobalt", ceo)
        self.assertIn("Original owner's goal", ceo)
        self.assertNotIn("Private worker question", ceo)
        self.assertNotIn("cobalt", project_context(st, "worker"))
        self.assertEqual([m["id"] for m in st.history("ceo", "cobalt")], [old, old + 1])
        self.assertLessEqual(len(ceo), 12_000)

    def test_ceo_can_create_revise_and_batch_controls(self):
        st = self.team()
        ceo = tools.Ctx(st, "ceo-owner-3", "ceo")
        text, error = tools.call(ceo, "team_task_create", {"title": "Palette", "spec": "Apply cobalt", "acceptance": "Cobalt appears", "scope": ["ui.py"]})
        self.assertFalse(error, text)
        tid = st.tasks()[0]["id"]
        st.update_task(tid, status="in_progress", owner="worker")
        text, error = tools.call(ceo, "team_task_edit", {"task_id": tid, "spec": "Use indigo", "scope": ["ui.py", "style.css"]})
        self.assertFalse(error, text)
        self.assertEqual(st.controls(pending=True)[0]["action"], "edit_task")
        text, error = tools.call(ceo, "team_control", {"operations": [{"action": "pause_agent", "seat": "worker"}, {"action": "stop"}]})
        self.assertFalse(error, text)
        before = len(st.controls())
        _, error = tools.call(ceo, "team_control", {"operations": [{"action": "stop"}, {"action": "pause_agent", "seat": "missing"}]})
        self.assertTrue(error)
        self.assertEqual(len(st.controls()), before)  # validate the whole batch before inserting it
        _, error = tools.call(tools.Ctx(st, "review", "reviewer"), "team_control", {"operations": [{"action": "stop"}]})
        self.assertTrue(error)

    def test_adaptive_chat_has_no_fixed_count(self):
        st = self.team()
        ctx = tools.Ctx(st, "worker", "member")
        for i in range(16):
            text, error = tools.call(ctx, "team_chat_post", {"kind": "update", "text": f"@ada Concrete finding number {i}: ui.py now has the required control."})
            self.assertFalse(error, text)
        for i in range(6):
            text, error = tools.call(ctx, "team_chat_post", {"kind": "concern", "text": f"@ada Evidence {i}: this handoff needs a correction."})
            self.assertFalse(error, text)
            text, error = tools.call(ctx, "team_escalate", {"question": f"Evidence {i}: resolve the API mismatch using the recorded failing check."})
            self.assertFalse(error, text)

    def test_ceo_can_complete_the_management_workflow(self):
        st = self.team()
        ceo = tools.Ctx(st, "ceo-owner-1", "ceo")
        _, error = tools.call(ceo, "team_task_create", {"title": "Palette", "spec": "Cobalt", "acceptance": "Visible", "scope": ["ui.py"]})
        self.assertFalse(error)
        _, error = tools.call(ceo, "team_set_checks", {"commands": ["test -s ui.py"]})
        self.assertFalse(error)
        _, error = tools.call(ceo, "team_plan_ready", {"summary": "Apply and verify cobalt"})
        self.assertFalse(error)
        tid = st.tasks()[0]["id"]
        _, error = tools.call(ceo, "team_task_note", {"task_id": tid, "note": "Keep the agreed layout"})
        self.assertFalse(error)
        st.update_task(tid, status="merged")
        _, error = tools.call(ceo, "team_project_done", {"report": "The cobalt palette is implemented and the layout is retained. The acceptance checks passed."})
        self.assertFalse(error)


class Coordinator(Isolated):
    def project(self):
        cfg = self.configured()
        repo = self.root / "repo"
        repo.mkdir()
        gitops.ensure_repo(repo)
        run = self.root / "runs" / "demo"
        run.mkdir(parents=True)
        o = Orchestrator(cfg, run, repo, "Build a palette", "demo")
        self.addCleanup(o.store.close)
        o.prepare()
        o.set_phase("build")
        return o

    def test_cancel_checkpoints_real_work_and_retry_keeps_branch(self):
        o = self.project()
        rt = next(s for s in o.seats.values() if s.spec.vendor == "codex")
        tid = o.store.create_task("Palette", "Cobalt palette", "Visible", ["ui.py"], [], tier="workhorse")
        self.assertTrue(o.give_task(rt, o.store.task(tid), "normal"))
        (rt.worktree / "ui.py").write_text("palette = 'cobalt'\n")
        rt.runner = mock.Mock()
        o.store.queue_controls("ceo", [{"action": "cancel_task", "task_id": tid}])
        o.process_controls()
        task = o.store.task(tid)
        self.assertEqual(task["status"], "cancelled")
        self.assertIn("cobalt", gitops.out(o.repo, "show", f"{task['branch']}:ui.py"))
        self.assertIsNone(o.store.seat(rt.name)["current_task"])
        o.store.queue_controls("ceo", [{"action": "retry_task", "task_id": tid}])
        o.process_controls()
        self.assertEqual(o.store.task(tid)["branch"], task["branch"])
        self.assertEqual(o.store.task(tid)["status"], "todo")
        self.assertTrue(all(c["status"] == "done" for c in o.store.controls()))

    def test_stop_bypasses_pending_review_without_overwriting_task(self):
        o = self.project()
        tid = o.store.create_task("Review", "Check", "Pass", ["x"], [])
        o.store.update_task(tid, status="review")
        o.jobs[f"review-{tid}"] = mock.Mock()
        o.store.queue_controls("ceo", [{"action": "cancel_task", "task_id": tid}, {"action": "stop"}])
        o.process_controls()
        self.assertTrue(o.store.get("stop_requested"))
        self.assertEqual(o.store.task(tid)["status"], "review")
        self.assertEqual(o.store.controls(pending=True)[0]["action"], "cancel_task")

    def test_shutdown_waits_for_background_cancellation_before_stop_receipt(self):
        o = self.project()
        ended = threading.Event()

        def background():
            o.cancel_jobs.wait(5)
            time.sleep(.05)
            ended.set()

        o.start_job("review-background", background)
        o.store.queue_controls("ceo", [{"action": "stop"}])
        o.process_controls()
        o.stop_run()
        o.shutdown()
        self.assertTrue(ended.is_set())
        self.assertEqual(o.store.controls()[0]["status"], "done")

    def test_paused_agents_and_model_overrides_survive_restart(self):
        o = self.project()
        rt = next(s for s in o.seats.values() if s.spec.vendor == "codex")
        o.store.queue_controls("ceo", [{"action": "set_model", "seat": rt.name, "model": "gpt-6-sol"},
                                      {"action": "pause_agent", "seat": rt.name}])
        o.process_controls()
        self.assertEqual(o.seat_model(rt.spec), "gpt-6-sol")
        self.assertTrue(o.store.get(f"paused:{rt.name}"))
        with mock.patch.object(o, "start_seat") as start:
            o.resume_seats()
            self.assertNotIn(rt.name, [c.args[0].name for c in start.call_args_list])

    def test_codex_worker_runs_its_model_and_completion_waits_for_owner(self):
        o = self.project()
        rt = next(s for s in o.seats.values() if s.spec.vendor == "codex")
        with mock.patch("crewlib.orchestrator.CodexSeat") as runner:
            o.start_seat(rt, None)
            setup = runner.call_args.args[4]
            self.assertEqual(setup.model, "gpt-6.1-sol")
        tid = o.store.create_task("Done", "Done", "Done", ["x"], [])
        o.store.update_task(tid, status="merged")
        o.store.set("done_requested_at", time.time())
        mid = o.store.owner_message("Change the palette", "ceo")
        o.check_completion()
        self.assertEqual(o.phase(), "build")
        o.store.set(f"ceo_answered:{mid}", True)
        o.jobs[f"owner-ceo-{mid}"] = mock.Mock()
        o.check_completion()
        self.assertEqual(o.phase(), "build")
        o.jobs.clear()
        o.check_completion()
        self.assertEqual(o.phase(), "deliver")

    def test_codex_manager_refines_writes_and_judges_on_codex(self):
        self.configured()
        settings.save({"models": {"work": "gpt-6-astra"}, "seats": []})
        o = self.project()
        self.assertEqual(o.seats[o.lead_name].spec.vendor, "codex")
        result = agents.RunResult(text="Done", structured={"goal": "Cobalt", "message": "Use cobalt"})
        with mock.patch("crewlib.orchestrator.run_once_codex", return_value=result) as codex, \
                mock.patch("crewlib.orchestrator.run_once_claude") as claude:
            o.refine()
            self.assertEqual(codex.call_args.kwargs["setup"].model, "gpt-6-astra")
            o.store.set("prompt_writer", True)
            mid = o.store.owner_message("Cobalt", "ceo")
            o._draft_job(next(m for m in o.store.owner_pending() if m["id"] == mid))
            self.assertEqual(codex.call_args.kwargs["setup"].model, "gpt-6-astra")
            a = o.store.create_task("A", "Build A", "Pass", ["a.py"], [])
            b = o.store.create_task("B", "Build B", "Pass", ["b.py"], [])
            o.run_judge(o.store.task(a), o.store.task(b), "Compare", o.main_wt)
            self.assertEqual(codex.call_args.kwargs["setup"].model, "gpt-6-astra")
            self.assertEqual(codex.call_args.kwargs["account"].vendor, "codex")
            self.assertEqual(codex.call_count, 3)
            claude.assert_not_called()

    def test_codex_manager_can_take_leadership_and_paused_agents_cannot(self):
        self.configured()
        settings.save({"seats": [
            {"name": "primary", "vendor": "claude", "account": "claude-1", "role": "lead", "tier": "manager"},
            {"name": "backup", "vendor": "codex", "account": "codex-1", "role": "member", "tier": "manager", "model": "gpt-6-astra"},
        ]})
        o = self.project()
        old, backup = o.seats["primary"], o.seats["backup"]
        backup.paused = True
        self.assertFalse(o.promote_lead(old, "unavailable"))
        backup.paused = False
        with mock.patch.object(o, "start_seat") as start:
            self.assertTrue(o.promote_lead(old, "unavailable"))
        self.assertEqual(o.lead_name, "backup")
        self.assertEqual(start.call_args.args[0].spec.vendor, "codex")


class ConversationAndProcesses(Isolated):
    def test_switch_products_carries_history_and_busy_refuses_before_changes(self):
        self.configured()
        m = chat.ChatManager()
        self.addCleanup(m.db.db.close)
        cid = m.create(engine="claude")["id"]
        m.db.add_message(cid, "user", "Original cobalt request", {})
        m.db.add_message(cid, "assistant", "I used cobalt in ui.py", {})
        with mock.patch.object(chat.CodexSession, "send") as send:
            result = m.send(cid, "Continue the work", engine="codex", model="gpt-6.1-sol")
        self.assertTrue(result["ok"])
        self.assertIn("Original cobalt request", send.call_args.args[0])
        self.assertIn("I used cobalt", send.call_args.args[0])
        self.assertIsNone(m.get(cid)["session_id"])
        m.sessions[cid].busy = True
        with self.assertRaises(ValueError):
            m.send(cid, "Switch back", engine="claude")
        self.assertEqual(m.get(cid)["engine"], "codex")
        self.assertEqual(len(m.get(cid)["messages"]), 3)

    def test_finished_project_followup_starts_once_and_keeps_history(self):
        rd = self.root / "runs" / "finished"
        rd.mkdir(parents=True)
        st = self.store(rd / "team.db")
        st.set("phase", "done")
        st.set("delivered", {"where": "repo"})
        st.post("you", "direct", "Earlier instructions", recipient="ceo")
        manager = runs.RunManager()
        with mock.patch.object(manager, "_spawn") as spawn:
            self.assertTrue(manager.say("finished", "Use indigo now", "ceo"))
            self.assertTrue(manager.say("finished", "And keep the layout", "ceo"))
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(st.get("phase_before_stop"), "build")
        self.assertIsNone(st.get("delivered"))
        self.assertEqual(len(st.conversation("ceo")), 3)
        for cached in manager._stores.values():
            cached.close()

    def test_cancel_stops_a_real_background_subprocess(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        timer.start()
        started = time.monotonic()
        agents._drive_codex([sys.executable, "-c", "import time; time.sleep(30)"], "prompt", self.root,
                            os.environ.copy(), self.root / "process.log", Redactor({}), timeout=10, cancel_event=event)
        self.assertLess(time.monotonic() - started, 10)
        timer.join()

    def test_codex_connections_do_not_put_header_secrets_in_arguments(self):
        env = {}
        with mock.patch("crewlib.connections.mcp_servers", return_value={
            "notes": {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": "secret-token"}},
            "local": {"type": "stdio", "command": "helper", "args": ["--json"], "env": {"TOKEN": "other"}}}):
            args = agents.codex_connections_args(env)
        self.assertNotIn("secret-token", " ".join(args))
        self.assertIn("secret-token", env.values())
        self.assertIn("env_http_headers", " ".join(args))


if __name__ == "__main__":
    unittest.main()
