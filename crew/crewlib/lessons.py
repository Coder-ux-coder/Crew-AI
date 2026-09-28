"""Experience memory shared by every agent, run and project.

Lessons live in ~/.crew/memory.db (SQLite, safe for many writers). A lesson
repeated in other words reinforces the existing one instead of duplicating it,
so the strongest lessons rise to the top. The top lessons are injected into
every agent's instructions at start; ~/.crew/PLAYBOOK.md is the digest for
humans.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from pathlib import Path

from .util import atomic_write, crew_home, now

CATEGORIES = ("usage", "speed", "quality", "models", "subagents", "tooling", "process", "errors", "project", "ceo")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_STOP = set("a an the to of in on for and or but is are was were be been it this that with as at by from "
            "when then do does did not no if so you your we our they them their its into than can will should".split())
_lock = threading.Lock()
_SEEDS = Path(__file__).with_name("seed_lessons.json")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(crew_home() / "memory.db", timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, last_seen REAL, category TEXT, text TEXT,
        evidence TEXT, source TEXT, project TEXT, weight INTEGER DEFAULT 1, norm TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS memo (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS effort_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project TEXT, kind TEXT, size TEXT, effort TEXT,
        rounds INTEGER, first_pass INTEGER, minutes REAL, tokens INTEGER)""")
    have = {row[1] for row in db.execute("PRAGMA table_info(effort_outcomes)")}
    # tier, model: added with the three-tier team (older rows count as the manager's);
    # outcome, handovers: added with the model scorecard
    for column, decl in (("tier", "TEXT"), ("model", "TEXT"), ("outcome", "TEXT"), ("handovers", "INTEGER")):
        if column not in have:
            try:
                db.execute(f"ALTER TABLE effort_outcomes ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:  # another process added it at the same moment
                pass
    db.execute("""CREATE TABLE IF NOT EXISTS contests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project TEXT, task TEXT, kind TEXT, size TEXT,
        winner_model TEXT, loser_model TEXT, winner_tier TEXT, loser_tier TEXT,
        winner_passed INTEGER, loser_passed INTEGER, reason TEXT)""")
    if db.execute("SELECT 1 FROM memo WHERE key='first_pass_fixed'").fetchone() is None:
        # Before 2.2 a task approved at its first review was stored as not passing first time (the round count
        # includes the approving review). Correct those records once, and drop the effort lessons built on them;
        # they are written again from the corrected record after the next project.
        db.execute("UPDATE effort_outcomes SET first_pass = CASE WHEN rounds <= 1 THEN 1 ELSE 0 END")
        db.execute("DELETE FROM lessons WHERE source='crew-effort-record'")  # DERIVED
        db.execute("INSERT OR IGNORE INTO memo(key,value) VALUES('first_pass_fixed','1')")
    return db


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def add(category: str, text: str, evidence: str = "", source: str = "", project: str = "",
        weight: int = 1) -> str:
    """Store a lesson; returns 'added' or 'reinforced'."""
    category = category if category in CATEGORIES else "process"
    text = " ".join(text.split())
    toks = _tokens(text)
    with _lock:
        db = _db()
        try:
            db.execute("BEGIN IMMEDIATE")
            best, best_score = None, 0.0
            for row in db.execute("SELECT id, norm FROM lessons WHERE category=? ORDER BY id DESC LIMIT 3000",
                                  (category,)):
                score = _similar(toks, set((row["norm"] or "").split()))
                if score > best_score:
                    best, best_score = row["id"], score
            if best is not None and best_score >= 0.6:
                db.execute("UPDATE lessons SET weight=weight+?, last_seen=? WHERE id=?", (weight, now(), best))
                db.execute("COMMIT")
                return "reinforced"
            db.execute(
                "INSERT INTO lessons(ts,last_seen,category,text,evidence,source,project,weight,norm) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), now(), category, text, evidence[:1500], source, project, weight, " ".join(sorted(toks))),
            )
            db.execute("COMMIT")
            return "added"
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()


def _rank(row: sqlite3.Row) -> float:
    age_days = max(0.0, (now() - (row["last_seen"] or now())) / 86400)
    return row["weight"] * math.pow(0.5, age_days / 60)


def top(limit: int = 25, categories: tuple[str, ...] | None = None) -> list[dict]:
    ensure_seeded()
    db = _db()
    try:
        rows = list(db.execute("SELECT * FROM lessons"))
    finally:
        db.close()
    if categories:
        rows = [r for r in rows if r["category"] in categories]
    rows.sort(key=_rank, reverse=True)
    return [dict(r) for r in rows[:limit]]


def search(query: str, limit: int = 10) -> list[dict]:
    ensure_seeded()
    q = _tokens(query)
    db = _db()
    try:
        rows = list(db.execute("SELECT * FROM lessons"))
    finally:
        db.close()
    if q:
        scored = [(len(q & set((r["norm"] or "").split())), _rank(r), r) for r in rows]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        rows = [s[2] for s in scored]
    else:
        rows.sort(key=_rank, reverse=True)
    return [dict(r) for r in rows[:limit]]


def ensure_seeded() -> None:
    """Load the built-in lessons (research + build experience) once per machine, and any new ones a later Crew
    ships. A built-in lesson that is already in the memory is left exactly as it is: installing an update is
    not new evidence, so it must not make the built-in lessons count for more than the team's own."""
    try:
        raw = _SEEDS.read_bytes()
    except OSError:
        return
    version = "sha256:" + hashlib.sha256(raw).hexdigest()  # the content, not the file's date (updates rewrite it)
    db = _db()
    try:
        done = db.execute("SELECT value FROM memo WHERE key='seeded'").fetchone()
    finally:
        db.close()
    if done and done["value"] == version:
        return
    try:
        items = json.loads(raw.decode("utf-8"))
    except ValueError:
        items = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            _seed(str(item.get("category") or "process"), str(item["text"]), str(item.get("evidence") or ""))
    db = _db()
    try:
        db.execute("INSERT INTO memo(key,value) VALUES('seeded',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (version,))
    finally:
        db.close()


def _seed(category: str, text: str, evidence: str = "") -> bool:
    """Add one built-in lesson unless the memory already holds it (or one that says the same). True if added."""
    category = category if category in CATEGORIES else "process"
    text = " ".join(text.split())
    toks = _tokens(text)
    with _lock:
        db = _db()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT norm FROM lessons WHERE category=? ORDER BY id DESC LIMIT 3000",
                              (category,)).fetchall()
            if any(_similar(toks, set((row["norm"] or "").split())) >= 0.6 for row in rows):
                db.execute("COMMIT")
                return False
            db.execute(
                "INSERT INTO lessons(ts,last_seen,category,text,evidence,source,project,weight,norm) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), now(), category, text, evidence[:1500], "crew-seed", "", 1, " ".join(sorted(toks))),
            )
            db.execute("COMMIT")
            return True
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()


def render_for_agents(limit: int = 25) -> str:
    items = [x for x in top(limit + 10) if x["category"] != "ceo"][:limit]  # the CEO's own lessons stay with the CEO
    if not items:
        return ""
    return "\n".join(f"- ({x['category']}) {x['text']}" for x in items)


# ------------------------------------------------------------------ the CEO's effort record

def record_effort_outcome(kind: str, size: str, effort: str, rounds: int, minutes: float, tokens: int,
                          project: str = "", tier: str = "manager", model: str = "", outcome: str = "merged",
                          first_pass: bool | None = None, handovers: int = 0) -> None:
    """One finished piece of work: who built it (model, tier), how hard it thought, and how it went — whether it
    passed its first check, the reviews it took (`rounds` counts the approving one), time, tokens, handovers.
    outcome: merged | moved-up (the workhorse failed twice and the manager took over) | won | lost (head-to-head)."""
    if effort not in EFFORT_ORDER:
        effort = "medium" if tier == "workhorse" else "high"  # "auto": the model chose; recorded as its default
    passed = int(rounds or 0) <= 1 if first_pass is None else bool(first_pass)
    db = _db()
    try:
        db.execute("INSERT INTO effort_outcomes(ts,project,kind,size,effort,rounds,first_pass,minutes,tokens,tier,"
                   "model,outcome,handovers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now(), project, kind or "build", size or "M", effort, int(rounds or 0), int(passed),
                    round(float(minutes or 0), 1), int(tokens or 0), tier or "manager", model or "", outcome,
                    int(handovers or 0)))
    finally:
        db.close()


def record_contest(project: str, task: str, kind: str, size: str, winner_model: str, loser_model: str,
                   winner_tier: str, loser_tier: str, winner_passed: bool, loser_passed: bool, reason: str) -> None:
    """One head-to-head: two models built the same task and the manager, judging blind, kept the better one."""
    db = _db()
    try:
        db.execute("INSERT INTO contests(ts,project,task,kind,size,winner_model,loser_model,winner_tier,loser_tier,"
                   "winner_passed,loser_passed,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now(), project, task, kind, size, winner_model, loser_model, winner_tier, loser_tier,
                    int(bool(winner_passed)), int(bool(loser_passed)), (reason or "")[:600]))
    finally:
        db.close()


def _effort_rank(effort) -> int:
    """Position on the effort scale; a level Crew does not know (an older record, a hand edit) sorts last."""
    return EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)


def effort_stats() -> list[dict]:
    """Per (tier, kind, size, effort): how many tasks, share approved first time, typical minutes and tokens."""
    db = _db()
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT COALESCE(tier, 'manager') AS tier, COALESCE(kind, 'build') AS kind, COALESCE(size, 'M') AS size, "
            "COALESCE(effort, '') AS effort, COUNT(*) AS n, AVG(first_pass) AS first_pass, "
            "AVG(minutes) AS minutes, AVG(tokens) AS tokens, AVG(rounds) AS rounds FROM effort_outcomes "
            "GROUP BY COALESCE(tier, 'manager'), COALESCE(kind, 'build'), COALESCE(size, 'M'), COALESCE(effort, '')")]
    finally:
        db.close()
    rows.sort(key=lambda r: (r["tier"] != "workhorse", str(r["kind"]), str(r["size"]), _effort_rank(r["effort"])))
    return rows


TIER_WORDS = {"workhorse": "workhorse (GPT-6 Sol)", "manager": "manager (Opus 5.5)"}


def render_for_ceo(limit: int = 12) -> str:
    """What the CEO has learned about effort: its own record plus its written lessons."""
    lines = []
    stats = effort_stats()
    if stats:
        lines.append("Your record so far (who built it, task kind, size, effort → tasks, approved first time, "
                     "typical minutes, typical tokens):")
        for r in stats:
            lines.append(f"- {TIER_WORDS.get(r['tier'], r['tier'])}: {r['kind']} {r['size']} at {r['effort']}: "
                         f"{r['n']} task(s), {round(100 * (r['first_pass'] or 0))}% first time, "
                         f"~{round(r['minutes'] or 0)} min, ~{int(r['tokens'] or 0) // 1000}k tokens")
    own = top(limit, categories=("ceo",))
    if own:
        lines.append("Your lessons:")
        lines += [f"- {x['text']}" for x in own]
    return "\n".join(lines)


DERIVED = "crew-effort-record"  # the source of the CEO's lessons that are worked out from its effort record


def derive_ceo_lessons(min_tasks: int = 3) -> list[str]:
    """Turn the effort record into plain lessons for the CEO, kept in step with the record: each is refreshed with
    the latest numbers and counts for more each time the record confirms it; one the record no longer supports
    goes. ("high is enough" must not stay once high has stopped being enough: a changed verdict is a new lesson,
    and the old one is gone.) Only these worked-out lessons change; the record itself is kept."""
    stats = effort_stats()
    by_group: dict[tuple[str, str, str], list[dict]] = {}
    for r in stats:
        by_group.setdefault((r["tier"], r["kind"], r["size"]), []).append(r)
    fresh: dict[str, str] = {}  # "effort record: tier kind size effort verdict" -> the lesson
    for (tier, kind, size), rows in by_group.items():
        solid = [r for r in rows if r["n"] >= min_tasks]
        who = TIER_WORDS.get(tier, tier)
        for r in solid:
            if r["effort"] not in EFFORT_ORDER:
                continue  # an effort level Crew does not know teaches nothing about the ones it uses
            rate = r["first_pass"] or 0
            if rate < 0.6:
                verdict = "more"
                text = (f"{kind} tasks of size {size} built by the {who} at {r['effort']} effort were approved first "
                        f"time only {round(100 * rate)}% of the time ({r['n']} tasks): give such work more effort"
                        + (", or the manager tier if it needs judgement." if tier == "workhorse" else "."))
            elif rate >= 0.9:
                lower = [x for x in solid if _effort_rank(x["effort"]) < _effort_rank(r["effort"])
                         and (x["first_pass"] or 0) >= 0.9]
                if lower:
                    continue  # a lower effort already does as well; that lesson is written for it
                verdict = "enough"
                text = (f"{kind} tasks of size {size} built by the {who} at {r['effort']} effort were approved first "
                        f"time {round(100 * rate)}% of the time ({r['n']} tasks, ~{round(r['minutes'] or 0)} min): "
                        f"{r['effort']} is enough for this kind of work.")
            else:
                continue
            fresh[f"effort record: {tier} {kind} {size} {r['effort']} {verdict}"] = text
    _keep_derived(fresh)
    return list(fresh.values())


def _keep_derived(fresh: dict[str, str]) -> None:
    """Make the worked-out lessons exactly `fresh` (keyed by what they are about), in one step."""
    with _lock:
        db = _db()
        try:
            db.execute("BEGIN IMMEDIATE")
            have: dict[str, int] = {}
            for row in db.execute("SELECT id, evidence FROM lessons WHERE source=? ORDER BY id", (DERIVED,)).fetchall():
                if row["evidence"] in fresh and row["evidence"] not in have:
                    have[row["evidence"]] = row["id"]
                else:  # no longer supported by the record, a repeat, or written before lessons had their keys
                    db.execute("DELETE FROM lessons WHERE id=?", (row["id"],))
            for key, text in fresh.items():
                norm = " ".join(sorted(_tokens(text)))
                if key in have:
                    db.execute("UPDATE lessons SET text=?, norm=?, weight=weight+1, last_seen=? WHERE id=?",
                               (text, norm, now(), have[key]))
                else:
                    db.execute("INSERT INTO lessons(ts,last_seen,category,text,evidence,source,project,weight,norm) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (now(), now(), "ceo", text, key, DERIVED, "", 1, norm))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()


def write_playbook() -> Path:
    """Human-readable digest of the strongest lessons, grouped by category."""
    items = top(200)
    by_cat: dict[str, list[dict]] = {}
    for x in items:
        by_cat.setdefault(x["category"], []).append(x)
    lines = ["# Crew playbook", "", "What past teams learned, strongest first. Generated — edit lessons, not this file.", ""]
    for cat in CATEGORIES:
        if cat in by_cat:
            lines.append(f"## {cat.capitalize()}")
            lines += [f"- {x['text']}  _(seen {x['weight']}×)_" for x in by_cat[cat][:15]]
            lines.append("")
    path = crew_home() / "PLAYBOOK.md"
    atomic_write(path, "\n".join(lines))
    return path
