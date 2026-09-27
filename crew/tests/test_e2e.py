"""End-to-end runs of the real orchestrator against scripted fake agents, with fault injection.

Each scenario runs a full project: refine → plan (+CEO review) → parallel build → fresh-eyes review →
merge with checks → final review → delivery → lessons. Faults: hangs, crashes, usage-limit hits,
rejected reviews, merge conflicts, stop-and-resume.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"
sys.path.insert(0, str(ROOT))

from crewlib import config, gitops  # noqa: E402
from crewlib.orchestrator import Orchestrator  # noqa: E402
from crewlib.store import Store  # noqa: E402

TOML = """
[team]
max_hours = 0.1
stall_minutes = {stall}
ledger_minutes = 1.5
chat_budget = 8
review = "cross"
ceo_reviews = true
deliver = "merge"
web_port = 0

[models]
work = "claude-opus-5-5"
ceo = "claude-fable-5-1"

{accounts}
"""


def make_run(scenario: dict, accounts: list[tuple[str, str]], stall: float = 0.5):
    home = Path(tempfile.mkdtemp(prefix="crew-e2e-home-"))
    os.environ["CREW_HOME"] = str(home)
    os.environ["CREW_FAKE_STATE"] = tempfile.mkdtemp(prefix="crew-e2e-state-")
    os.environ["CREW_FAKE_SCENARIO"] = json.dumps(scenario)
    os.environ["CREW_CLAUDE_BIN"] = str(FAKES / "fake_claude")
    os.environ["CREW_CODEX_BIN"] = str(FAKES / "fake_codex")
    blocks = "\n".join(f'[[account]]\nname = "{n}"\nvendor = "{v}"\n' for n, v in accounts)
    (home / "crew.toml").write_text(TOML.format(stall=stall, accounts=blocks))
    cfg = config.load(str(home / "crew.toml"))
    os.environ["CREW_FAKE_SEATS"] = ",".join(s.name for s in cfg.seats)
    repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-e2e-proj-")))
    (repo / "shared.txt").write_text("original\n")
    gitops.commit_all(repo, "initial project")
    run_id = "t" + str(int(time.time() * 1000))
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    return cfg, run_dir, repo, run_id


def run_orch(cfg, run_dir, repo, run_id, resume=False, timeout=240, stop_after=None) -> Orchestrator:
    orch = Orchestrator(cfg, run_dir, repo, "Build a small feature pack with tests", run_id, resume=resume)
    os.environ["CREW_FAKE_INTEGRATION"] = orch.integration
    t = threading.Thread(target=orch.run, daemon=True)
    t.start()
    start = time.time()
    while t.is_alive() and time.time() - start < timeout:
        if stop_after and stop_after(orch.store):
            orch.store.set("stop_requested", time.time())
            stop_after = None
        time.sleep(0.5)
    if t.is_alive():
        orch.store.set("stop_requested", time.time())
        t.join(30)
        raise AssertionError("run did not finish in time; chat:\n" + dump_chat(orch.store))
    return orch


def dump_chat(store: Store) -> str:
    return "\n".join(f"{m['sender']}[{m['kind']}]: {m['text'][:200]}" for m in store.messages_after(0, 1000))


class E2E(unittest.TestCase):
    maxDiff = None

    def assert_finished(self, orch: Orchestrator, repo: Path, features: int):
        st = orch.store
        self.assertEqual(st.get("phase"), "done", dump_chat(st))
        statuses = {t["title"]: t["status"] for t in st.tasks()}
        self.assertTrue(all(s in ("merged", "cancelled") for s in statuses.values()), statuses)
        for i in range(1, features + 1):
            self.assertTrue((repo / "app" / f"feat{i}.py").is_file(), f"feat{i} not delivered")
        self.assertTrue((orch.run_dir / "REPORT.md").is_file())
        self.assertFalse(gitops.is_dirty(repo))

    def test_happy_path_three_vendors(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3}, [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        reviews = orch.store.events("review")
        self.assertGreaterEqual(len(reviews), 4)
        by = {e["data"].get("by") or "" for e in reviews}
        self.assertTrue(any(b.startswith("reviewer-") for b in by), by)
        chat = dump_chat(orch.store)
        self.assertIn("Plan review: APPROVE", chat)
        self.assertIn("Final review: APPROVE", chat)
        accounts = {a["name"]: a for a in orch.store.accounts()}
        self.assertIsNotNone(accounts["claude-1"]["util_5h"])  # usage read from rate events
        self.assertIsNotNone(accounts["codex-1"]["util_5h"])  # usage read from Codex session files
        # effort: the CEO set one for every task at plan review, and builders ran at it
        efforts = {t["id"]: t["effort"] for t in orch.store.tasks()}
        self.assertTrue(all(efforts.values()), efforts)
        self.assertIn("xhigh", efforts.values())
        self.assertIn("Effort per task:", chat)
        from crewlib import lessons
        self.assertTrue(lessons.effort_stats())  # the CEO's effort record grows with every merged task

    def test_rejections_concerns_and_plan_revision(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "reject_task": [2], "plan_changes": True, "concern": True},
                                           [("claude-1", "claude"), ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        self.assertEqual(orch.store.task(2)["review_rounds"], 2)
        self.assertIn("Plan review: CHANGES", dump_chat(orch.store))

    def test_usage_limit_failover_keeps_conversation(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "limit_on_task": [1]},
                                           [("claude-1", "claude"), ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        fo = orch.store.events("failover")
        self.assertTrue(fo, dump_chat(orch.store))
        self.assertTrue(fo[0]["data"]["kept_context"])
        self.assertEqual(fo[0]["data"]["frm"], "claude-1")

    def test_hang_and_crash_recovery(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3, "hang_on_task": [2], "crash_on_task": [3]},
                                           [("claude-1", "claude"), ("claude-2", "claude")], stall=0.05)
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        self.assertTrue(orch.store.events("restart"))

    def test_merge_conflict_is_returned_and_resolved(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "outside_scope_task": [2, 3]},
                                           [("claude-1", "claude"), ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        self.assertIn("conflicts with newer work", dump_chat(orch.store))

    def test_auto_solo_mode_for_small_jobs(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3, "size": "small", "parts": 1},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        st = orch.store
        self.assertEqual(st.get("mode"), "solo")
        self.assertEqual(len(st.tasks()), 1)
        self.assertEqual({s["name"]: s["turns"] for s in st.seats() if s["role"] != "lead"},
                         {"boole": 0, "curie": 0})  # the others only checked the work
        self.assertTrue(st.events("review"))
        self.assertIn("Final review: APPROVE", dump_chat(st))

    def test_stop_and_resume(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3}, [("claude-1", "claude"), ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid,
                        stop_after=lambda st: any(t["status"] == "merged" for t in st.tasks()))
        self.assertEqual(orch.store.get("phase"), "stopped")
        orch2 = run_orch(cfg, run_dir, repo, rid, resume=True)
        self.assert_finished(orch2, repo, 3)


if __name__ == "__main__":
    unittest.main()
