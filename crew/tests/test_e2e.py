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
ledger_minutes = {ledger}
chat_budget = 8
review = "cross"
ceo_reviews = true
deliver = "merge"
web_port = 0

[models]
work = "claude-opus-5-5"

{accounts}
"""


def make_run(scenario: dict, accounts: list[tuple[str, str]], stall: float = 0.5, team_extra: str = "",
             ledger: float = 1.5):
    home = Path(tempfile.mkdtemp(prefix="crew-e2e-home-"))
    os.environ["CREW_HOME"] = str(home)
    os.environ["CREW_FAKE_STATE"] = tempfile.mkdtemp(prefix="crew-e2e-state-")
    os.environ["CREW_FAKE_SCENARIO"] = json.dumps(scenario)
    os.environ["CREW_CLAUDE_BIN"] = str(FAKES / "fake_claude")
    os.environ["CREW_CODEX_BIN"] = str(FAKES / "fake_codex")
    blocks = "\n".join(f'[[account]]\nname = "{n}"\nvendor = "{v}"\n' for n, v in accounts)
    (home / "crew.toml").write_text(TOML.format(stall=stall, accounts=blocks, team_extra=team_extra, ledger=ledger))
    cfg = config.load(str(home / "crew.toml"))
    os.environ["CREW_FAKE_SEATS"] = ",".join(f"{s.name}:{s.tier}" for s in cfg.seats)
    repo = gitops.ensure_repo(Path(tempfile.mkdtemp(prefix="crew-e2e-proj-")))
    (repo / "shared.txt").write_text("original\n")
    gitops.commit_all(repo, "initial project")
    run_id = "t" + str(int(time.time() * 1000))
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    return cfg, run_dir, repo, run_id


def run_orch(cfg, run_dir, repo, run_id, resume=False, timeout=240, stop_after=None, during=None) -> Orchestrator:
    """Run a project to the end. during(store) is called every half second while it runs (the owner's hand)."""
    orch = Orchestrator(cfg, run_dir, repo, "Build a small feature pack with tests", run_id, resume=resume)
    os.environ["CREW_FAKE_INTEGRATION"] = orch.integration
    t = threading.Thread(target=orch.run, daemon=True)
    t.start()
    start = time.time()
    while t.is_alive() and time.time() - start < timeout:
        if stop_after and stop_after(orch.store):
            orch.store.set("stop_requested", time.time())
            stop_after = None
        if during:
            during(orch.store)
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
        self.assertEqual([(s.vendor, s.role, s.tier) for s in cfg.seats],
                         [("claude", "lead", "manager"), ("claude", "member", "manager"),
                          ("claude", "member", "workhorse"), ("claude", "member", "workhorse")])
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
        # The three tiers: Sonnet 5.5 builds the workhorse tasks, Opus 5.5 the manager tasks and every review,
        # GPT-6 Astra is the CEO (answering in JSON, recorded by the orchestrator).
        seats = {s["name"]: s for s in st.seats()}
        tasks = st.tasks()
        self.assertEqual({t["title"]: t["tier"] for t in tasks},
                         {"Foundation": "manager", "Feature 1": "workhorse", "Feature 2": "manager",
                          "Feature 3": "workhorse"})
        for t in tasks:
            owner = seats[t["owner"]]
            self.assertEqual((owner["tier"], owner["vendor"], owner["model"]),
                             (t["tier"], "claude",
                              "claude-sonnet-5-5" if t["tier"] == "workhorse" else "claude-opus-5-5"), t)
        runs = self.oneoffs(st)
        reviewers = [r for r in runs if r["role"] == "reviewer"]
        self.assertTrue(reviewers and all(r["vendor"] == "claude" and r["model"] == "claude-opus-5-5"
                                          for r in reviewers), reviewers)
        ceo = [r for r in runs if r["role"] == "ceo"]
        self.assertEqual({(r["model"], r["vendor"], r["effort"]) for r in ceo}, {("gpt-6-astra", "codex", "max")})
        self.assertEqual(sorted(r["seat"] for r in ceo), ["ceo-final", "ceo-plan"])
        calls = self.codex_calls()
        self.assertTrue(all(c["model"] == "gpt-6-astra" and c["schema"] for c in calls if c["role"] == "ceo"), calls)
        self.assertEqual([c for c in calls if c["role"] != "ceo"], [])  # ChatGPT only runs the CEO now
        from crewlib import tiers
        share = {t["tier"]: t["tokens"] for t in tiers.shares(st)["tiers"]}
        self.assertTrue(all(share.values()), share)  # all three tiers did work
        self.assertIn("How the work was shared", (orch.run_dir / "REPORT.md").read_text())
        accounts = {a["name"]: a for a in orch.store.accounts()}
        self.assertIsNotNone(accounts["claude-1"]["util_5h"])  # usage read from rate events
        self.assertGreater(accounts["codex-1"]["tokens"], 0)  # the CEO's work counts against the ChatGPT plan
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
        """A small, routine job: one Sonnet 5.5 seat builds it, Opus 5.5 reviews it and the lead verifies and reports."""
        cfg, run_dir, repo, rid = make_run({"tasks": 3, "size": "small", "parts": 1},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 3)
        st = orch.store
        self.assertEqual(st.get("mode"), "solo")
        self.assertEqual(len(st.tasks()), 1)
        task = st.tasks()[0]
        tier = {s["name"]: s["tier"] for s in st.seats()}
        self.assertEqual((task["tier"], tier[task["owner"]], task["effort"]), ("workhorse", "workhorse", "medium"))
        turns = {s["name"]: s["turns"] for s in st.seats()}
        other = ({"curie", "dijkstra"} - {task["owner"]}).pop()  # the workhorse on the subscription used least builds
        self.assertEqual((turns["boole"], turns[other]), (0, 0))  # benched: nobody else was needed
        self.assertGreater(turns[task["owner"]], 0)
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
        self.assertEqual({s["name"]: s["turns"] for s in st.seats() if s["tier"] == "workhorse"},
                         {"boole": 0, "curie": 0})

    def test_a_review_at_a_usage_limit_moves_on_or_waits(self):
        """The reviewer of a workhorse task runs out of usage on each Claude subscription in turn: the check moves to the
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
        """Both features are built twice (Sonnet 5.5 and Opus 5.5); the judge compares each pair blind and the better
        version is merged under the original task's number. The scorecard records every result."""
        cfg, run_dir, repo, rid = make_run({"tasks": 2}, [("claude-1", "claude"), ("claude-2", "claude"),
                                                          ("codex-1", "codex")], team_extra='head_to_head = "all"')
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        tier = {s["name"]: s["tier"] for s in st.seats()}
        contests = st.events("contest")
        self.assertEqual(len(contests), 2, dump_chat(st))
        self.assertTrue(all(e["data"]["winner"] == "claude-sonnet-5-5" for e in contests))  # the judge preferred Sonnet's
        merged = {t["title"]: t for t in st.tasks() if t["status"] == "merged"}
        self.assertEqual(sorted(merged), ["Feature 1", "Feature 2", "Foundation"])
        self.assertEqual(sum(1 for t in st.tasks() if t["status"] == "cancelled"), 2)  # the two losing versions
        self.assertEqual(tier[merged["Feature 2"]["owner"]], "workhorse")  # a manager task, won by Sonnet's version
        judges = [r for r in self.oneoffs(st) if r["seat"].startswith("judge-")]
        self.assertTrue(judges and all(r["model"] == "claude-opus-5-5" for r in judges))
        from crewlib import scorecard
        models = scorecard.stats()["models"]
        self.assertEqual((models["claude-sonnet-5-5"]["wins"], models["claude-opus-5-5"]["losses"]), (2, 2))
        self.assertIn("Head-to-head", (orch.run_dir / "REPORT.md").read_text())

    def test_head_to_head_combines_the_best_of_both(self):
        """Collaborative head-to-head (the default style): the judge names what the other version does better, and
        the kept version takes it in before the usual check — one joint result."""
        borrow = "Handle an empty input the way the other version does (app/feat1.py)."
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "contest_borrow": borrow},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")],
                                           team_extra='head_to_head = "all"')
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        self.assertEqual(len(st.events("contest")), 2, dump_chat(st))
        kept = [t for t in st.tasks() if t["status"] == "merged" and t["title"].startswith("Feature")]
        self.assertEqual(len(kept), 2)
        for t in kept:
            self.assertIn("Joint submission", t["notes"])
            self.assertTrue(st.get(f"joint:{t['id']}"))
        self.assertTrue(any("combining the best of both" in m["text"] for m in st.messages_after(0, 5000)))

    def test_solo_head_to_head(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "size": "small", "parts": 1, "contest_winner": "manager"},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")],
                                           team_extra='head_to_head = "some"')
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        self.assertEqual(st.get("mode"), "solo")
        builder = st.get("solo_builder")
        self.assertEqual((builder in ("curie", "dijkstra"), st.get("solo_rival")), (True, "boole"))
        contest = st.events("contest")
        self.assertEqual([e["data"]["winner"] for e in contest], ["claude-opus-5-5"])
        kept = st.task(1)
        self.assertEqual((kept["status"], kept["owner"]), ("merged", "boole"))  # the rival's version was kept
        turns = {s["name"]: s["turns"] for s in st.seats()}
        self.assertEqual(turns[({"curie", "dijkstra"} - {builder}).pop()], 0)

    def test_talk_to_one_agent_and_the_ceo(self):
        """The owner writes privately to one agent and to the CEO. The prompt writer writes each message up first
        (the owner's words travel with it); the agent answers the owner alone and passes the instruction on to the
        lead; the CEO answers the question. Nobody else sees the private messages."""
        cfg, run_dir, repo, rid = make_run({"tasks": 2}, [("claude-1", "claude"), ("claude-2", "claude")])
        asked: dict[str, int] = {}

        def owner(st: Store) -> None:
            if not asked and st.get("phase") == "build":
                asked["boole"] = st.owner_message("How is it going? Please tell the team: every module needs a "
                                                  "docstring.", "boole")
                asked["ceo"] = st.owner_message("Is the plan sound?", "ceo")

        orch = run_orch(cfg, run_dir, repo, rid, during=owner)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        msgs = st.messages_after(0, 5000)
        by_id = {m["id"]: m for m in msgs}
        self.assertEqual(by_id[asked["boole"]]["kind"], "drafted")
        final = next(m for m in msgs if m.get("ref") == asked["boole"])
        self.assertEqual((final["kind"], final["recipient"]), ("direct", "boole"))
        self.assertTrue(final["text"].startswith("Clarified: How is it going?"))
        self.assertEqual(final["original"], by_id[asked["boole"]]["text"])
        replies = [m for m in msgs if m["sender"] == "boole" and m.get("recipient") == "you"]
        self.assertTrue(replies, dump_chat(st))
        relay = [m for m in msgs if m["sender"] == "boole" and not m.get("recipient")
                 and m["text"].startswith("From the owner (told to me directly)")]
        self.assertTrue(relay and "@ada" in relay[0]["text"], dump_chat(st))
        ceo = [m for m in msgs if m["sender"] == "ceo" and m.get("recipient") == "you"]
        self.assertTrue(ceo and "CEO here" in ceo[0]["text"], dump_chat(st))
        writers = [r for r in self.oneoffs(st) if r["seat"].startswith("writer-")]
        self.assertEqual(len(writers), 2)
        questions = [r for r in self.oneoffs(st) if r["seat"].startswith("ceo-question-")]
        self.assertTrue(questions and questions[0]["effort"] == "high")

    def test_an_unanswered_question_is_answered_from_the_turn(self):
        """An agent that does not use team_reply_owner still answers: its own words at the end of the turn."""
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "no_reply_tool": True},
                                           [("claude-1", "claude"), ("claude-2", "claude")],
                                           team_extra="prompt_writer = false")
        asked: dict[str, int] = {}

        def owner(st: Store) -> None:
            if not asked and st.get("phase") == "build":
                asked["boole"] = st.owner_message("Are you blocked on anything?", "boole")

        orch = run_orch(cfg, run_dir, repo, rid, during=owner)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        self.assertEqual(st.messages_after(asked["boole"] - 1)[0]["kind"], "direct")  # no prompt writer
        replies = [m for m in st.messages_after(asked["boole"], 5000)
                   if m["sender"] == "boole" and m.get("recipient") == "you"]
        self.assertTrue(replies, dump_chat(st))
        self.assertEqual([m["id"] for m in st.owner_asks("boole")], [])

    def test_a_leaked_secret_goes_back_to_its_author(self):
        """Crew's own scan finds an API key in a change, sends the work back before any reviewer sees it, and the
        corrected version is merged."""
        cfg, run_dir, repo, rid = make_run({"tasks": 2, "leak_task": [2]}, [("claude-1", "claude"),
                                                                            ("claude-2", "claude")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 2)
        st = orch.store
        scanned = [e for e in st.events("review") if e["task_id"] == 2 and e["data"].get("by") == "scan"]
        self.assertEqual([e["data"]["verdict"] for e in scanned], ["changes"], dump_chat(st))
        self.assertTrue(any(e["data"].get("secrets") for e in st.events("scan")))
        self.assertNotIn("sk-ant-", (repo / "app" / "feat1.py").read_text())
        self.assertNotIn("Qw3rTy7UiOp9", dump_chat(st))  # the key is never repeated anywhere

    def test_workhorse_task_moves_up_after_two_failed_checks(self):
        cfg, run_dir, repo, rid = make_run({"tasks": 1, "reject_twice": [2]},
                                           [("claude-1", "claude"), ("claude-2", "claude"), ("codex-1", "codex")])
        orch = run_orch(cfg, run_dir, repo, rid)
        self.assert_finished(orch, repo, 1)
        st = orch.store
        tier = {s["name"]: s["tier"] for s in st.seats()}
        task = st.task(2)
        self.assertEqual((task["tier"], tier[task["owner"]], task["review_rounds"]), ("manager", "manager", 1))
        self.assertTrue(st.events("moved_up"))
        self.assertIn("moves up to Opus 5.5", dump_chat(st))
        from crewlib import scorecard
        models = scorecard.stats()["models"]
        self.assertEqual(models["claude-sonnet-5-5"]["moved_up"], 1)
        self.assertEqual(models["claude-sonnet-5-5"]["by_kind"]["build S"]["passed"], 0)
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
