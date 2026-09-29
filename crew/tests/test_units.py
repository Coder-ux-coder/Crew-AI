"""Unit tests: store, leases, tools (authority, budgets), MCP protocol, scheduler, config, lessons, git."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CREW_HOME"] = tempfile.mkdtemp(prefix="crew-home-")

from crewlib import config, gitops, lessons, scheduler, tools  # noqa: E402
from crewlib.store import Store, StoreError, globs_overlap  # noqa: E402
from crewlib.util import Redactor, load_env_file  # noqa: E402


def new_store() -> Store:
    d = Path(tempfile.mkdtemp(prefix="crew-store-"))
    st = Store(d / "team.db")
    for name, role in (("ada", "lead"), ("boole", "member"), ("curie", "member")):
        st.upsert_seat(name, role=role, vendor="claude", account="claude-1", status="idle")
    st.upsert_account("claude-1", vendor="claude")
    st.set("settings", {"chat_budget": 3, "max_escalations": 2})
    st.set("phase", "build")
    return st


class GlobTests(unittest.TestCase):
    def test_overlaps(self):
        self.assertTrue(globs_overlap("src/api/**", "src/api/users.py"))
        self.assertTrue(globs_overlap("src/api", "src/api/users.py"))
        self.assertTrue(globs_overlap("**/*.md", "docs/x.py"))  # unknown -> conservative
        self.assertTrue(globs_overlap("src/*.py", "src/a.py"))
        self.assertFalse(globs_overlap("src/api/**", "src/web/**"))
        self.assertFalse(globs_overlap("src/a.py", "src/b.py"))
        self.assertFalse(globs_overlap("tests/test_a.py", "src/a.py"))


class StoreTests(unittest.TestCase):
    def test_dependencies_and_leases(self):
        st = new_store()
        a = st.create_task("core", "build core", "works", ["app/core.py"], [], "S", "foundation")
        b = st.create_task("feat", "build feat", "works", ["app/feat.py"], [a], "M")
        c = st.create_task("feat2", "touch core too", "works", ["app/**"], [], "M")
        with self.assertRaises(StoreError):
            st.create_task("bad", "x", "y", ["x.py"], [99], "S")
        with self.assertRaises(StoreError):
            st.create_task("noscope", "x", "y", [], [], "S", "build")
        st.create_task("research", "look", "", [], [], "S", "research")  # no scope needed
        ready = [t["id"] for t in st.ready_tasks()]
        self.assertIn(a, ready)
        self.assertNotIn(b, ready)  # waits on a
        st.start_task(a, "ada", "br-a")
        with self.assertRaises(StoreError):
            st.start_task(c, "boole", "br-c")  # app/** overlaps app/core.py held by a
        with self.assertRaises(StoreError):
            st.start_task(a, "boole", "br-a")  # already taken
        st.update_task(a, status="merged")
        self.assertIn(b, [t["id"] for t in st.ready_tasks()])

    def test_chat_unread_and_cursor(self):
        st = new_store()
        st.post("ada", "update", "hello")
        st.post("boole", "update", "hi")
        self.assertEqual([m["text"] for m in st.unread("curie")], ["hello", "hi"])
        self.assertEqual([m["text"] for m in st.unread("ada")], ["hi"])  # own messages excluded
        st.mark_read("curie", st.last_message_id())
        self.assertEqual(st.unread("curie"), [])


class ToolTests(unittest.TestCase):
    def ctx(self, st, seat, role):
        return tools.Ctx(store=st, seat=seat, role=role)

    def test_authority(self):
        st = new_store()
        text, err = tools.call(self.ctx(st, "boole", "member"), "team_task_create",
                               {"title": "t", "spec": "s", "acceptance": "a", "scope": ["x.py"]})
        self.assertTrue(err)
        text, err = tools.call(self.ctx(st, "ada", "lead"), "team_task_create",
                               {"title": "t", "spec": "s", "acceptance": "a", "scope": ["x.py"], "suggested_owner": "boole"})
        self.assertFalse(err, text)
        tid = st.tasks()[0]["id"]
        st.start_task(tid, "boole", "b")
        text, err = tools.call(self.ctx(st, "curie", "member"), "team_task_submit",
                               {"task_id": tid, "summary": "did the thing properly", "evidence": "pytest: 3 passed"})
        self.assertTrue(err)  # not the owner
        text, err = tools.call(self.ctx(st, "boole", "member"), "team_task_submit",
                               {"task_id": tid, "summary": "did the thing properly", "evidence": ""})
        self.assertTrue(err)  # evidence required
        text, err = tools.call(self.ctx(st, "boole", "member"), "team_task_submit",
                               {"task_id": tid, "summary": "did the thing properly", "evidence": "pytest: 3 passed"})
        self.assertFalse(err, text)
        self.assertEqual(st.task(tid)["status"], "review")
        rev = tools.Ctx(store=st, seat="reviewer-1", role="reviewer", task_id=tid + 1)
        text, err = tools.call(rev, "team_review_submit", {"task_id": tid, "verdict": "approve", "notes": "ok"})
        self.assertTrue(err)  # wrong task for this reviewer
        rev.task_id = tid
        text, err = tools.call(rev, "team_review_submit", {"task_id": tid, "verdict": "approve", "notes": "ok"})
        self.assertFalse(err, text)
        self.assertEqual(st.get(f"review:{tid}")["verdict"], "approve")

    def test_chat_budget_and_concern(self):
        st = new_store()
        c = self.ctx(st, "boole", "member")
        for i in range(3):
            self.assertFalse(tools.call(c, "team_chat_post", {"text": f"update {i}"})[1])
        text, err = tools.call(c, "team_chat_post", {"text": "one more opinion"})
        self.assertTrue(err)
        self.assertIn("budget", text)
        self.assertFalse(tools.call(c, "team_chat_post", {"text": "stuck on X", "kind": "blocker"})[1])
        st.set("phase", "plan")
        st.upsert_seat("curie", chat_used=0)
        c2 = self.ctx(st, "curie", "member")
        self.assertFalse(tools.call(c2, "team_chat_post", {"text": "concern: scope", "kind": "concern"})[1])
        self.assertTrue(tools.call(c2, "team_chat_post", {"text": "another concern", "kind": "concern"})[1])

    def test_digest_urgent_inline(self):
        st = new_store()
        tools.call(self.ctx(st, "ada", "lead"), "team_decide", {"text": "Use SQLite for storage."})
        text, _ = tools.call(self.ctx(st, "boole", "member"), "team_status", {})
        self.assertIn("Use SQLite", text)
        self.assertIn("unread", text)

    def test_project_done_requires_closed_board(self):
        st = new_store()
        lead = self.ctx(st, "ada", "lead")
        tools.call(lead, "team_task_create", {"title": "t", "spec": "s", "acceptance": "a", "scope": ["x.py"]})
        text, err = tools.call(lead, "team_project_done", {"report": "x" * 60})
        self.assertTrue(err)


class McpTests(unittest.TestCase):
    def test_protocol_roundtrip(self):
        st = new_store()
        env = {**os.environ, "CREW_DB": st.path, "CREW_SEAT": "ada", "CREW_ROLE": "lead",
               "PYTHONPATH": str(ROOT)}
        p = subprocess.Popen([sys.executable, "-m", "crewlib.mcp_server"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=env)

        def rpc(msg):
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline()) if "id" in msg else None

        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = [t["name"] for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("team_task_create", names)
        self.assertNotIn("team_review_submit", names)
        res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "team_chat_post", "arguments": {"text": "hello team"}}})
        self.assertFalse(res["result"]["isError"])
        self.assertEqual(st.recent_messages(1)[0]["text"], "hello team")
        p.stdin.close()
        p.wait(5)


class SchedulerTests(unittest.TestCase):
    def test_modes(self):
        t = time.time()
        self.assertEqual(scheduler.mode_of({"util_5h": None}, t), "normal")
        self.assertEqual(scheduler.mode_of({"parked_until": t + 100}, t), "parked")
        self.assertEqual(scheduler.mode_of({"util_5h": 0.95, "reset_5h": t + 3 * 3600}, t), "conserve")
        self.assertEqual(scheduler.mode_of({"util_5h": 0.4, "reset_5h": t + 20 * 60}, t), "spend")
        self.assertEqual(scheduler.mode_of({"util_5h": 0.1, "reset_5h": t + 2 * 3600}, t), "spend")
        self.assertEqual(scheduler.mode_of({"util_5h": 0.5, "reset_5h": t + 2.5 * 3600}, t), "normal")
        self.assertEqual(scheduler.mode_of({"util_5h": 0.2, "util_7d": 0.95}, t), "conserve")

    def test_apply_rate_from_claude_event(self):
        st = new_store()
        info = {"status": "allowed_warning", "resetsAt": 1, "rateLimitType": "five_hour", "utilization": 0.8,
                "unifiedWindows": {"five_hour": {"utilization": 0.81, "resetsAt": 2000},
                                   "seven_day": {"utilization": 0.3, "resetsAt": 9000}}}
        scheduler.apply_rate(st, "claude-1", info)
        acc = st.account("claude-1")
        self.assertAlmostEqual(acc["util_5h"], 0.81)
        self.assertAlmostEqual(acc["util_7d"], 0.3)
        scheduler.apply_rate(st, "claude-1", {"status": "rejected", "resetsAt": int(time.time()) + 600})
        self.assertEqual(scheduler.refresh_modes(st)["claude-1"], "parked")

    def test_choose_task_respects_suggested_owner_and_conserve(self):
        tasks = [
            {"id": 1, "size": "L", "kind": "build", "suggested_owner": "boole"},
            {"id": 2, "size": "S", "kind": "build", "suggested_owner": None},
            {"id": 3, "size": "M", "kind": "build", "suggested_owner": "curie"},
        ]
        seat = {"name": "curie", "vendor": "claude", "account": "a"}
        grace = {1: time.time() + 100}
        pick = scheduler.choose_task(seat, tasks, {"name": "a"}, "normal", {}, "m", {"curie"}, grace)
        self.assertEqual(pick["id"], 3)
        pick = scheduler.choose_task(seat, tasks, {"name": "a"}, "conserve", {}, "m", {"curie"}, grace)
        self.assertEqual(pick["id"], 2)  # conserve: small tasks only


class ConfigTests(unittest.TestCase):
    def test_defaults_and_policy(self):
        cfg = config.load(None, seats=3)
        self.assertEqual(len(cfg.seats), 3)
        self.assertEqual(cfg.lead.role, "lead")
        self.assertEqual(cfg.models.work, "claude-opus-5-5")
        with self.assertRaises(config.ConfigError):
            cfg.models.check("claude-haiku-4-5")
        with self.assertRaises(config.ConfigError):
            cfg.models.check("claude-sonnet-5")
        self.assertEqual(cfg.models.check("claude-fable-5-1"), "claude-fable-5-1")

    def test_example_settings_parse(self):
        cfg = config.load(str(ROOT / "crew.toml.example"))
        self.assertEqual([a.name for a in cfg.accounts], ["claude-1", "claude-2", "claude-3", "codex-1"])
        self.assertEqual(cfg.seats[0].role, "lead")
        self.assertEqual([s.tier for s in cfg.seats], ["manager"] * 3 + ["workhorse"] * 2)
        self.assertEqual({s.vendor for s in cfg.seats}, {"claude"})  # the ChatGPT subscription runs the CEO
        self.assertEqual(cfg.models.workhorse, "claude-sonnet-5-5")


class LessonTests(unittest.TestCase):
    def test_add_reinforce_search(self):
        self.assertEqual(lessons.add("speed", "Run the fast test subset while developing and the full suite before submit."), "added")
        self.assertEqual(lessons.add("speed", "While developing run the fast test subset; run the full suite before you submit."), "reinforced")
        found = lessons.search("fast test subset")
        self.assertTrue(found and found[0]["weight"] >= 2)
        self.assertTrue(lessons.top(5))  # seeds loaded


class UtilTests(unittest.TestCase):
    def test_secrets(self):
        d = Path(tempfile.mkdtemp())
        (d / "s.env").write_text("# c\nexport API_KEY='abcdef123456'\nEMPTY=\nOTHER=xyz\n")
        env = load_env_file(d / "s.env")
        self.assertEqual(env, {"API_KEY": "abcdef123456", "OTHER": "xyz"})
        self.assertEqual(Redactor(env)("key is abcdef123456!"), "key is •••!")


class GitTests(unittest.TestCase):
    def test_worktrees_merge_conflict_and_revert(self):
        repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-repo-")))
        (repo / "a.txt").write_text("base\n")
        gitops.commit_all(repo, "base")
        main = repo.parent / (repo.name + "-main")
        gitops.add_worktree(repo, main, "crew/x/main", gitops.head(repo))
        w1, w2 = repo.parent / (repo.name + "-w1"), repo.parent / (repo.name + "-w2")
        for w in (w1, w2):
            gitops.git(repo, "worktree", "add", "-f", "--detach", str(w), "crew/x/main")
        gitops.checkout_task(w1, "crew/x/task-1", "crew/x/main")
        gitops.checkout_task(w2, "crew/x/task-2", "crew/x/main")
        (w1 / "a.txt").write_text("one\n")
        (w2 / "a.txt").write_text("two\n")
        self.assertTrue(gitops.commit_all(w1, "t1"))
        self.assertTrue(gitops.commit_all(w2, "t2"))
        self.assertTrue(gitops.merge_into(main, "crew/x/task-1", "m1").ok)
        res = gitops.merge_into(main, "crew/x/task-2", "m2")
        self.assertFalse(res.ok)
        self.assertEqual(res.conflicts, ["a.txt"])
        self.assertFalse(gitops.is_dirty(main))  # merge aborted cleanly
        gitops.revert_last_merge(main, "test")
        self.assertEqual((main / "a.txt").read_text(), "base\n")
        chk = gitops.run_checks(main, ["false"], main.parent / "chk.log", 30)
        self.assertFalse(chk.ok)

    def test_early_work_is_carried_onto_the_task_branch(self):
        repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-repo-")))
        (repo / "a.txt").write_text("base\n")
        gitops.commit_all(repo, "base")
        gitops.git(repo, "branch", "crew/y/main")
        wt = repo.parent / (repo.name + "-early")
        gitops.git(repo, "worktree", "add", "-f", "--detach", str(wt), "crew/y/main")
        (wt / "committed.txt").write_text("done early\n")
        gitops.commit_all(wt, "early commit on a detached head")
        (wt / "uncommitted.txt").write_text("still editing\n")
        gitops.checkout_task(wt, "crew/y/task-1", "crew/y/main")
        self.assertEqual(gitops.current_branch(wt), "crew/y/task-1")
        self.assertTrue((wt / "committed.txt").is_file())
        self.assertTrue((wt / "uncommitted.txt").is_file())
        self.assertFalse(gitops.is_dirty(wt))
        files = gitops.changed_files(repo, "crew/y/main", "crew/y/task-1")
        self.assertEqual(sorted(files), ["committed.txt", "uncommitted.txt"])


if __name__ == "__main__":
    unittest.main()
