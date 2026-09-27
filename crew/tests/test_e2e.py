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
{team_extra}
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

{accounts}
"""


def make_run(scenario: dict, accounts: list[tuple[str, str]], stall: float = 0.5, team_extra: str = ""):
    home = Path(tempfile.mkdtemp(prefix="crew-e2e-home-"))
    os.environ["CREW_HOME"] = str(home)
    os.environ["CREW_FAKE_STATE"] = tempfile.mkdtemp(prefix="crew-e2e-state-")
    os.environ["CREW_FAKE_SCENARIO"] = json.dumps(scenario)
    os.environ["CREW_CLAUDE_BIN"] = str(FAKES / "fake_claude")
    os.environ["CREW_CODEX_BIN"] = str(FAKES / "fake_codex")
    blocks = "\n".join(f'[[account]]\nname = "{n}"\nvendor = "{v}"\n' for n, v in accounts)
    (home / "crew.toml").write_text(TOML.format(stall=stall, accounts=blocks, team_extra=team_extra))
    cfg = config.load(str(home / "crew.toml"))
    os.environ["CREW_FAKE_SEATS"] = ",".join(f"{s.name}:{s.vendor}" for s in cfg.seats)
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

    def codex_calls(self) -> list[dict]:
        path = Path(os.environ["CREW_FAKE_STATE"]) / "codex-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def oneoffs(self, store: Store) -> list[dict]:
        return [{"seat": e["seat"], "task": e["task_id"], **e["data"]} for e in store.events("oneoff")
                if e["data"].get("state") == "start"]

    def test_happy_path_three_vendors(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3}, [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        self.assertEqual([(s.vendor, s.role) for s in cfg.seats],
                         [("claude", "lead"), ("claude", "member"), ("codex", "member"), ("codex", "member")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        st = orch.store
        reviews = st.events("review")
        self.assertGreaterEqual(len(reviews), 4)
        by = {e["data"].get("by") or "" for e in reviews}
        self.assertTrue(any(b.startswith("reviewer-") for b in by), by)
        chat = dump_chat(st)
        self.assertIn("Plan review: APPROVE", chat)
        self.assertIn("Final review: APPROVE", chat)
        # The three tiers: GPT-6 Sol builds the workhorse tasks, Opus 5.5 the manager tasks and every review,
        # GPT-6 Astra is the CEO (answering in JSON, recorded by the orchestrator).
        vendor = {s["name"]: s["vendor"] for s in st.seats()}
        tasks = st.tasks()
        self.assertEqual({t["title"]: t["tier"] for t in tasks},
                         {"Foundation": "manager", "Feature 1": "workhorse", "Feature 2": "manager",
                          "Feature 3": "workhorse"})
        for t in tasks:
            self.assertEqual(vendor[t["owner"]], "codex" if t["tier"] == "workhorse" else "claude", t)
        runs = self.oneoffs(st)
        reviewers = [r for r in runs if r["role"] == "reviewer"]
        self.assertTrue(reviewers and all(r["vendor"] == "claude" and r["model"] == "claude-opus-5-5"
                                          for r in reviewers), reviewers)
        ceo = [r for r in runs if r["role"] == "ceo"]
        self.assertEqual({(r["model"], r["vendor"], r["effort"]) for r in ceo}, {("gpt-6-astra", "codex", "max")})
        self.assertEqual(sorted(r["seat"] for r in ceo), ["ceo-final", "ceo-plan"])
        calls = self.codex_calls()
        self.assertTrue(all(c["model"] == "gpt-6-astra" and c["schema"] for c in calls if c["role"] == "ceo"), calls)
        self.assertTrue(any(c["model"] == "gpt-6-sol" and c["role"] == "member" for c in calls), calls)
        from crewlib import tiers
        share = {t["tier"]: t["tokens"] for t in tiers.shares(st)["tiers"]}
        self.assertTrue(all(share.values()), share)  # all three tiers did work
        self.assertIn("How the work was shared", (orch.run_dir / "REPORT.md").read_text())
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
        """A small, routine job: one GPT-6 Sol seat builds it, Opus 5.5 reviews it and the lead verifies and reports."""
        cfg, run_dir, repo, rid = make_run({"tasks": 3, "size": "small", "parts": 1},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        st = orch.store
        self.assertEqual(st.get("mode"), "solo")
        self.assertEqual(len(st.tasks()), 1)
        task = st.tasks()[0]
        self.assertEqual((task["tier"], task["owner"], task["effort"]), ("workhorse", "curie", "medium"))
        turns = {s["name"]: s["turns"] for s in st.seats()}
        self.assertEqual((turns["boole"], turns["dijkstra"]), (0, 0))  # benched: nobody else was needed
        self.assertGreater(turns["curie"], 0)
        self.assertGreater(turns["ada"], 0)  # the lead verified the result and wrote the report
        self.assertTrue(all(r["vendor"] == "claude" for r in self.oneoffs(st) if r["role"] == "reviewer"))
        self.assertTrue(st.events("review"))
        self.assertIn("Final review: APPROVE", dump_chat(st))
        self.assertNotIn("ceo-effort", {r["seat"] for r in self.oneoffs(st)})  # the CEO is kept for checking

    def test_solo_manager_job_is_built_by_the_lead(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "size": "small", "parts": 1, "builder_tier": "manager",
                                            "solo_effort": "xhigh"},
                                           [("claude-1", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        task = st.tasks()[0]
        self.assertEqual((task["tier"], task["owner"], task["effort"]), ("manager", "ada", "xhigh"))
        self.assertEqual({s["name"]: s["turns"] for s in st.seats() if s["vendor"] == "codex"},
                         {"boole": 0, "curie": 0})

    def test_a_review_at_a_usage_limit_moves_on_or_waits(self):
        """The reviewer of a Sol task runs out of usage on each Claude subscription in turn: the check moves to the
        other subscription, then waits for the managers' usage to return. The work is never sent back for it."""
        cfg, run_dir, repo, rid = make_run({"tasks": 1, "review_limit_task": [2], "limit_seconds": 20},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid, timeout=300)
        self.assert_finished(orch, repo, 1)
        st = orch.store
        task = st.task(2)
        self.assertEqual((task["tier"], task["review_rounds"]), ("workhorse", 1))  # one real review, no bounce
        chat = dump_chat(st)
        self.assertIn("waits for its check", chat)
        self.assertNotIn("needs changes", chat)
        self.assertEqual(len([r for r in self.oneoffs(st) if r["seat"] == "reviewer-2"]), 3)  # both limits, then the check

    def test_head_to_head_keeps_the_better_version(self):
        """Both features are built twice (GPT-6 Sol and Opus 5.5); the judge compares each pair blind and the better
        version is merged under the original task's number. The scorecard records every result."""
        cfg, run_dir, repo, rid = make_run({"tasks": 2}, [("claude-1", "claude"), ("claude-2", "claude"),
                                                          ("codex-1", "codex")], team_extra='head_to_head = "all"')
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        vendor = {s["name"]: s["vendor"] for s in st.seats()}
        contests = st.events("contest")
        self.assertEqual(len(contests), 2, dump_chat(st))
        self.assertTrue(all(e["data"]["winner"] == "gpt-6-sol" for e in contests))  # the judge preferred Sol's
        merged = {t["title"]: t for t in st.tasks() if t["status"] == "merged"}
        self.assertEqual(sorted(merged), ["Feature 1", "Feature 2", "Foundation"])
        self.assertEqual(sum(1 for t in st.tasks() if t["status"] == "cancelled"), 2)  # the two losing versions
        self.assertEqual(vendor[merged["Feature 2"]["owner"]], "codex")  # a manager task, won by Sol's version
        judges = [r for r in self.oneoffs(st) if r["seat"].startswith("judge-")]
        self.assertTrue(judges and all(r["model"] == "claude-opus-5-5" for r in judges))
        from crewlib import scorecard
        models = scorecard.stats()["models"]
        self.assertEqual((models["gpt-6-sol"]["wins"], models["claude-opus-5-5"]["losses"]), (2, 2))
        self.assertIn("Head-to-head", (orch.run_dir / "REPORT.md").read_text())

    def test_solo_head_to_head(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "size": "small", "parts": 1, "contest_winner": "claude"},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")],
                                           team_extra='head_to_head = "some"')
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        self.assertEqual(st.get("mode"), "solo")
        self.assertEqual((st.get("solo_builder"), st.get("solo_rival")), ("curie", "boole"))
        contest = st.events("contest")
        self.assertEqual([e["data"]["winner"] for e in contest], ["claude-opus-5-5"])
        kept = st.task(1)
        self.assertEqual((kept["status"], kept["owner"]), ("merged", "boole"))  # the rival's version was kept
        turns = {s["name"]: s["turns"] for s in st.seats()}
        self.assertEqual(turns["dijkstra"], 0)

    def test_workhorse_task_moves_up_after_two_failed_checks(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 1, "reject_twice": [2]},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 1)
        st = orch.store
        vendor = {s["name"]: s["vendor"] for s in st.seats()}
        task = st.task(2)
        self.assertEqual((task["tier"], vendor[task["owner"]], task["review_rounds"]), ("manager", "claude", 1))
        self.assertTrue(st.events("moved_up"))
        self.assertIn("moves up to Opus 5.5", dump_chat(st))
        from crewlib import scorecard
        models = scorecard.stats()["models"]
        self.assertEqual(models["gpt-6-sol"]["moved_up"], 1)
        self.assertEqual(models["gpt-6-sol"]["by_kind"]["build S"]["passed"], 0)
        self.assertEqual(models["claude-opus-5-5"]["by_kind"]["build S"]["passed"], 1)  # the moved task, first time

    def test_ceo_falls_back_to_its_claude_backup(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "codex_ceo_fails": True},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        ceo = [(r["seat"], r["model"]) for r in self.oneoffs(orch.store) if r["role"] == "ceo"]
        self.assertIn(("ceo-plan", "gpt-6-astra"), ceo)
        self.assertIn(("ceo-plan", "claude-fable-5-1"), ceo)
        self.assertIn("Plan review: APPROVE", dump_chat(orch.store))

    def test_stop_and_resume(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 3}, [("claude-1", "claude"), ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid,
                        stop_after=lambda st: any(t["status"] == "merged" for t in st.tasks()))
        self.assertEqual(orch.store.get("phase"), "stopped")
        orch2 = run_orch(cfg, run_dir, repo, rid, resume=True)
        self.assert_finished(orch2, repo, 3)


if __name__ == "__main__":
    unittest.main()
