"""The three-tier team: GPT-6 Sol is the workhorse, Opus 5.5 the manager, GPT-6 Astra the CEO.

Seats per tier, the rules that route each task to its tier, the effort names each model accepts, the lead's and
the CEO's tier tools, and the token shares measured against the owner's targets.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore", ResourceWarning)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CREW_HOME", tempfile.mkdtemp(prefix="crew-home-"))

from crewlib import config, scheduler, tiers, tools  # noqa: E402
from crewlib.agents import codex_effort  # noqa: E402
from crewlib.store import Store  # noqa: E402


def team_store() -> Store:
    """A lead and a manager on Claude, two workhorse seats on one ChatGPT subscription."""
    st = Store(Path(tempfile.mkdtemp(prefix="crew-tiers-")) / "team.db")
    for name, role, vendor, model, account in (("ada", "lead", "claude", "claude-opus-5-5", "claude-1"),
                                               ("boole", "member", "claude", "claude-opus-5-5", "claude-2"),
                                               ("curie", "member", "codex", "gpt-6-sol", "codex-1"),
                                               ("dijkstra", "member", "codex", "gpt-6-sol", "codex-1")):
        st.upsert_seat(name, role=role, vendor=vendor, model=model, account=account, status="idle")
    st.set("settings", {"chat_budget": 3, "max_escalations": 2})
    st.set("phase", "plan")
    return st


def write_toml(text: str) -> str:
    path = Path(tempfile.mkdtemp(prefix="crew-cfg-")) / "crew.toml"
    path.write_text(text)
    return str(path)


ACCOUNTS = ('[[account]]\nname = "claude-1"\nvendor = "claude"\n\n[[account]]\nname = "claude-2"\nvendor = "claude"\n\n'
            '[[account]]\nname = "codex-1"\nvendor = "codex"\n')


class DefaultsAndSeats(unittest.TestCase):
    def test_the_owners_structure_is_the_default(self):
        m = config.ModelPolicy()
        self.assertEqual((m.codex, m.work, m.ceo, m.ceo_backup),
                         ("gpt-6-sol", "claude-opus-5-5", "gpt-6-astra", "claude-fable-5-1"))
        self.assertEqual(tiers.TARGETS, {"workhorse": (25, 35), "manager": (60, 70), "ceo": (0, 5)})

    def test_manager_seats_per_claude_account_and_workhorse_seats_per_chatgpt_account(self):
        cfg = config.load(write_toml(ACCOUNTS))
        self.assertEqual([(s.name, s.vendor, s.account, s.role) for s in cfg.seats],
                         [("ada", "claude", "claude-1", "lead"), ("boole", "claude", "claude-2", "member"),
                          ("curie", "codex", "codex-1", "member"), ("dijkstra", "codex", "codex-1", "member")])
        cfg = config.load(write_toml("[team]\nworkhorse_seats = 3\n\n" + ACCOUNTS))
        self.assertEqual(sum(1 for s in cfg.seats if s.vendor == "codex"), 3)
        cfg = config.load(write_toml(ACCOUNTS), seats=2)  # an explicit count still shares accounts round-robin
        self.assertEqual([s.account for s in cfg.seats], ["claude-1", "claude-2"])

    def test_each_tier_must_run_on_the_right_kind_of_model(self):
        for bad in ('[models]\nwork = "gpt-6-sol"\n', '[models]\ncodex = "claude-opus-5-5"\n',
                    '[models]\nceo_backup = "gpt-6-astra"\n', '[models]\nceo = "gpt-6-luna"\n',
                    '[team]\nworkhorse_seats = 0\n'):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(write_toml(bad + "\n" + ACCOUNTS))
        cfg = config.load(write_toml('[models]\neffort_ceo = "ultra"\n\n' + ACCOUNTS))
        self.assertEqual(cfg.models.effort_ceo, "ultra")  # GPT-6's deepest level is allowed for the CEO

    def test_models_are_named_and_placed(self):
        self.assertEqual(tiers.vendor_of("gpt-6-astra"), "codex")
        self.assertEqual(tiers.vendor_of("claude-fable-5-1"), "claude")
        self.assertEqual(tiers.model_label("gpt-6-sol"), "GPT-6 Sol")
        self.assertEqual(tiers.model_label(""), "ChatGPT")
        self.assertEqual(tiers.default_tier("foundation", "S"), "manager")
        self.assertEqual(tiers.default_tier("docs", "M"), "workhorse")
        self.assertEqual(tiers.default_tier("build", "S"), "workhorse")
        self.assertEqual(tiers.default_tier("build", "M"), "manager")


class EffortNames(unittest.TestCase):
    def test_gpt6_levels_are_passed_as_openai_names_them(self):
        for level in ("low", "medium", "high", "xhigh", "max", "ultra"):
            self.assertEqual(codex_effort(level, "gpt-6-sol"), level)
            self.assertEqual(codex_effort(level, "gpt-6-astra"), level)
        self.assertEqual(codex_effort("minimal", "gpt-6-sol"), "low")  # GPT-6 has no "minimal"
        self.assertIsNone(codex_effort("auto", "gpt-6-sol"))
        self.assertEqual(codex_effort("max", ""), "max")  # Codex's own default is a GPT-6 model

    def test_older_models_get_their_nearest_level(self):
        self.assertEqual(codex_effort("max", "gpt-5.5"), "xhigh")
        self.assertEqual(codex_effort("ultra", "gpt-5.5"), "xhigh")
        self.assertEqual(codex_effort("minimal", "gpt-5.5"), "minimal")


class Routing(unittest.TestCase):
    sol = {"name": "curie", "vendor": "codex", "account": "codex-1"}
    opus = {"name": "boole", "vendor": "claude", "account": "claude-2"}

    def test_each_tier_takes_its_own_work(self):
        routine = {"id": 1, "tier": "workhorse", "kind": "build", "size": "S"}
        hard = {"id": 2, "tier": "manager", "kind": "build", "size": "M"}
        foundation = {"id": 3, "tier": "workhorse", "kind": "foundation", "size": "S"}
        allows = scheduler.tier_allows
        self.assertTrue(allows(self.sol, routine, True, False))
        self.assertFalse(allows(self.sol, hard, True, False))  # never: work that needs judgement
        self.assertFalse(allows(self.sol, foundation, True, False))
        self.assertTrue(allows(self.opus, hard, True, False))
        self.assertFalse(allows(self.opus, routine, True, False))  # routine work waits for the workhorse
        self.assertTrue(allows(self.opus, routine, False, False))  # ...unless no workhorse can run
        self.assertTrue(allows(self.opus, routine, True, True))  # ...or it waited long and Opus is under target
        self.assertTrue(allows(self.opus, foundation, True, False))

    def test_choose_task_uses_the_tier(self):
        ready = [{"id": 1, "tier": "manager", "kind": "build", "size": "M", "suggested_owner": None},
                 {"id": 2, "tier": "workhorse", "kind": "build", "size": "S", "suggested_owner": None}]
        pick = scheduler.choose_task(self.sol, ready, {"name": "codex-1"}, "normal", {}, "gpt-6-sol", {"curie"}, {},
                                     workhorse_usable=True)
        self.assertEqual(pick["id"], 2)
        pick = scheduler.choose_task(self.opus, ready, {"name": "claude-2"}, "normal", {}, "claude-opus-5-5",
                                     {"boole"}, {}, workhorse_usable=True)
        self.assertEqual(pick["id"], 1)


class TierTools(unittest.TestCase):
    def test_the_lead_sets_tiers_and_owners_must_match(self):
        st = team_store()
        lead = tools.Ctx(store=st, seat="ada", role="lead")
        text, err = tools.call(lead, "team_task_create", {"title": "Font sizes", "spec": "Set h1 to 32px.",
                                                          "acceptance": "h1 is 32px", "scope": ["style.css"],
                                                          "size": "S", "tier": "workhorse"})
        self.assertFalse(err, text)
        self.assertEqual(st.tasks()[-1]["tier"], "workhorse")
        text, err = tools.call(lead, "team_task_create", {"title": "Login", "spec": "Add sign-in.", "acceptance": "ok",
                                                          "scope": ["auth.py"], "tier": "manager",
                                                          "suggested_owner": "curie"})
        self.assertTrue(err)
        self.assertIn("workhorse seat", text)
        text, err = tools.call(lead, "team_task_create", {"title": "Copy", "spec": "Fix the typo.", "acceptance": "ok",
                                                          "scope": ["README.md"], "tier": "workhorse",
                                                          "suggested_owner": "boole"})
        self.assertTrue(err)
        self.assertIn("manager seat", text)
        # without a tier, the suggested owner's tier is used
        text, err = tools.call(lead, "team_task_create", {"title": "Styles", "spec": "Adjust spacing.",
                                                          "acceptance": "ok", "scope": ["site.css"], "size": "M",
                                                          "suggested_owner": "dijkstra"})
        self.assertFalse(err, text)
        self.assertEqual(st.tasks()[-1]["tier"], "workhorse")

    def test_the_ceo_confirms_or_changes_each_tier(self):
        st = team_store()
        tid = st.create_task("Parser", "Parse the file.", "ok", ["p.py"], [], size="S", suggested_owner="curie",
                             tier="workhorse")
        ceo = tools.Ctx(store=st, seat="ceo-plan", role="ceo")
        text, err = tools.call(ceo, "team_verdict", {"kind": "plan", "verdict": "approve", "notes": "",
                                                     "efforts": [{"task_id": tid, "effort": "xhigh",
                                                                  "tier": "manager"}]})
        self.assertFalse(err, text)
        task = st.task(tid)
        self.assertEqual((task["tier"], task["effort"], task["suggested_owner"]), ("manager", "xhigh", None))
        self.assertIn("#1 xhigh (manager)", st.recent_messages(1)[0]["text"])

    def test_the_solo_builder_may_set_the_checks(self):
        st = team_store()
        sol = tools.Ctx(store=st, seat="curie", role="member")
        _, err = tools.call(sol, "team_set_checks", {"commands": ["pytest -q"]})
        self.assertTrue(err)
        st.set("solo_builder", "curie")
        _, err = tools.call(sol, "team_set_checks", {"commands": ["pytest -q"]})
        self.assertFalse(err)
        self.assertEqual(st.get("checks"), ["pytest -q"])


class Shares(unittest.TestCase):
    def test_tokens_are_counted_per_tier_against_the_targets(self):
        st = team_store()
        st.add_seat_usage("ada", tokens=500_000)
        st.add_seat_usage("curie", tokens=300_000)
        st.event("oneoff", seat="reviewer-1", state="start", role="reviewer", vendor="claude", model="claude-opus-5-5")
        st.event("oneoff", seat="reviewer-1", state="done", tokens=150_000)
        st.event("oneoff", seat="ceo-plan", state="start", role="ceo", vendor="codex", model="gpt-6-astra")
        st.event("oneoff", seat="ceo-plan", state="done", tokens=50_000)
        data = tiers.shares(st)
        self.assertEqual(data["total"], 1_000_000)
        by = {t["tier"]: t for t in data["tiers"]}
        self.assertEqual((by["workhorse"]["pct"], by["manager"]["pct"], by["ceo"]["pct"]), (30.0, 65.0, 5.0))
        self.assertTrue(all(t["on_target"] for t in data["tiers"]))
        self.assertEqual({m["label"] for m in data["models"]}, {"GPT-6 Sol", "Opus 5.5", "GPT-6 Astra"})
        self.assertEqual(tiers.share_of(st, "manager"), 65.0)


if __name__ == "__main__":
    unittest.main()
