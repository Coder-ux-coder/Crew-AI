"""The model scorecard: the corrected first-check record, the figures per model and kind of work, the routing
rules drawn from them, head-to-head results, and the lead's task tool applying the rule."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crewlib import lessons, scorecard, tools  # noqa: E402
from crewlib.store import Store  # noqa: E402

SOL, OPUS = "gpt-6-sol", "claude-opus-5-5"


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("CREW_HOME")
        os.environ["CREW_HOME"] = tempfile.mkdtemp(prefix="crew-score-")

    def tearDown(self):
        os.environ["CREW_HOME"] = self.saved or ""

    def record(self, model: str, kind: str, size: str, passed: int, total: int, **kw):
        tier = "workhorse" if model == SOL else "manager"
        for i in range(total):
            lessons.record_effort_outcome(kind, size, "medium", 1 if i < passed else 2, 10 + i, 20000 + 1000 * i,
                                          project="p", tier=tier, model=model, **kw)


class FirstCheckFix(Base):
    def test_old_records_are_corrected_once_and_their_lessons_dropped(self):
        path = Path(os.environ["CREW_HOME"]) / "memory.db"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, last_seen REAL, category TEXT,
                text TEXT, evidence TEXT, source TEXT, project TEXT, weight INTEGER DEFAULT 1, norm TEXT);
            CREATE TABLE memo (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE effort_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project TEXT, kind TEXT,
                size TEXT, effort TEXT, rounds INTEGER, first_pass INTEGER, minutes REAL, tokens INTEGER);
            INSERT INTO effort_outcomes(ts,project,kind,size,effort,rounds,first_pass,minutes,tokens)
                VALUES (1e10,'p','build','S','high',1,0,5,100), (1e10,'p','build','S','high',3,0,9,300);
            INSERT INTO lessons(ts,category,text,source,weight) VALUES
                (1,'ceo','build tasks of size S at high effort were approved first time only 0%','crew-effort-record',3),
                (1,'ceo','A lesson the CEO wrote itself.','agent:ceo',1);
        """)
        db.commit()
        db.close()
        stats = lessons.effort_stats()  # opening the memory runs the one-time correction
        self.assertEqual(stats[0]["first_pass"], 0.5)  # approved at the first review: rounds 1 now counts
        texts = [x["text"] for x in lessons.top(10, categories=("ceo",))]
        self.assertEqual(texts, ["A lesson the CEO wrote itself."])
        lessons.record_effort_outcome("build", "S", "high", 3, 5, 100)  # a later failure stays a failure
        self.assertAlmostEqual(lessons.effort_stats()[0]["first_pass"], 1 / 3)

    def test_first_pass_means_approved_at_the_first_review(self):
        lessons.record_effort_outcome("fix", "M", "high", 1, 5, 100, model=OPUS)
        lessons.record_effort_outcome("fix", "M", "high", 2, 5, 100, model=OPUS)
        cell = scorecard.stats()["models"][OPUS]["by_kind"]["fix M"]
        self.assertEqual((cell["n"], cell["passed"]), (2, 1))


class Figures(Base):
    def test_per_model_and_kind_with_labels(self):
        self.record(SOL, "docs", "S", 5, 5)
        self.record(SOL, "fix", "M", 1, 5)
        self.record(OPUS, "fix", "M", 4, 4)
        self.record(OPUS, "docs", "S", 4, 4)
        self.record(SOL, "build", "S", 2, 2)
        models = scorecard.stats()["models"]
        sol = models[SOL]
        self.assertEqual((sol["tier"], sol["n"], sol["passed"]), ("workhorse", 12, 8))
        self.assertEqual(sol["by_kind"]["docs S"]["label"], "strong")
        self.assertEqual(sol["by_kind"]["fix M"]["label"], "weak")
        self.assertEqual(sol["by_kind"]["build S"]["label"], "thin")  # two pieces are not evidence yet
        self.assertEqual(sol["strong"], ["docs S"])
        self.assertEqual(sol["weak"], ["fix M"])
        self.assertIn("S", sol["by_size"])

    def test_rules_move_weak_work_up_and_suggest_strong_work_down(self):
        self.record(SOL, "fix", "M", 1, 5)
        self.record(OPUS, "fix", "M", 4, 4)
        self.record(SOL, "docs", "S", 5, 5)
        self.record(OPUS, "docs", "S", 4, 4)
        rules = scorecard.rules(SOL, OPUS)
        self.assertEqual([(r["kind"], r["size"]) for r in rules["up"]], [("fix", "M")])
        self.assertIn("1 of 5", rules["up"][0]["why"])
        self.assertEqual([(r["kind"], r["size"]) for r in rules["down"]], [("docs", "S")])
        self.assertEqual(scorecard.route_tier("fix", "M", "workhorse", SOL, OPUS)[0], "manager")
        self.assertEqual(scorecard.route_tier("docs", "S", "workhorse", SOL, OPUS), ("workhorse", ""))
        self.assertEqual(scorecard.route_tier("fix", "M", "manager", SOL, OPUS), ("manager", ""))
        text = scorecard.render(SOL, OPUS)
        self.assertIn("GPT-6 Sol (workhorse)", text)
        self.assertIn("weak at fix M 20% (5)", text)
        self.assertIn("Automatic rule", text)
        self.assertIn("your call", text)

    def test_head_to_head_results_count(self):
        for _ in range(2):
            lessons.record_contest("p", "Header", "build", "S", SOL, OPUS, "workhorse", "manager", True, True, "simpler")
        lessons.record_contest("p", "Parser", "build", "S", OPUS, SOL, "manager", "workhorse", True, False, "handles errors")
        models = scorecard.stats()["models"]
        self.assertEqual((models[SOL]["wins"], models[SOL]["losses"]), (2, 1))
        self.assertEqual(scorecard.rules(SOL, OPUS)["down"][0]["why"], "GPT-6 Sol won 2 of 3 head-to-heads")
        summary = scorecard.summary(SOL, OPUS)
        self.assertEqual(len(summary["contests"]), 3)
        self.assertEqual(summary["contests"][0]["winner"], "Opus 5.5")  # newest first

    def test_summary_table_marks_the_better_model(self):
        self.record(SOL, "docs", "S", 4, 4)
        self.record(OPUS, "docs", "S", 3, 4)
        summary = scorecard.summary(SOL, OPUS)
        self.assertEqual([m["model"] for m in summary["models"]], [SOL, OPUS])  # workhorse first
        row = summary["matrix"][0]
        self.assertEqual((row["kind"], row["size"], row["best"]), ("docs", "S", SOL))

    def test_nothing_recorded_yet(self):
        self.assertEqual(scorecard.render(SOL, OPUS), "")
        self.assertEqual(scorecard.summary(SOL, OPUS)["models"], [])


class TaskTool(Base):
    def test_a_workhorse_task_of_a_weak_kind_goes_to_the_manager(self):
        self.record(SOL, "fix", "M", 1, 5)
        st = Store(Path(tempfile.mkdtemp(prefix="crew-score-run-")) / "team.db")
        for name, role, vendor, model in (("ada", "lead", "claude", OPUS), ("curie", "member", "codex", SOL)):
            st.upsert_seat(name, role=role, vendor=vendor, model=model, account=name, status="idle")
        st.set("phase", "plan")
        lead = tools.Ctx(store=st, seat="ada", role="lead")
        text, err = tools.call(lead, "team_task_create", {"title": "Fix the parser", "spec": "Handle blank lines.",
                                                          "acceptance": "blank lines parse", "scope": ["parser.py"],
                                                          "size": "M", "kind": "fix", "tier": "workhorse",
                                                          "suggested_owner": "curie"})
        self.assertFalse(err, text)
        task = st.tasks()[-1]
        self.assertEqual((task["tier"], task["suggested_owner"]), ("manager", None))
        self.assertIn("passed the first check 1 of 5 times", text)
        text, err = tools.call(lead, "team_task_create", {"title": "Docs", "spec": "Write the README section.",
                                                          "acceptance": "section exists", "scope": ["README.md"],
                                                          "size": "S", "kind": "docs", "tier": "workhorse"})
        self.assertFalse(err, text)
        self.assertEqual(st.tasks()[-1]["tier"], "workhorse")


if __name__ == "__main__":
    unittest.main()
