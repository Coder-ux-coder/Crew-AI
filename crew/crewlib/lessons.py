"""Experience memory shared by every agent, run and project.

Lessons live in ~/.crew/memory.db (SQLite, safe for many writers). A lesson
repeated in other words reinforces the existing one instead of duplicating it,
so the strongest lessons rise to the top. The top lessons are injected into
every agent's instructions at start; ~/.crew/PLAYBOOK.md is the digest for
humans.
"""

from __future__ import annotations

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
    """Load the built-in lessons (research + build experience) once per machine."""
    db = _db()
    try:
        done = db.execute("SELECT value FROM memo WHERE key='seeded'").fetchone()
    finally:
        db.close()
    version = str(_SEEDS.stat().st_mtime_ns) if _SEEDS.is_file() else "0"
    if done and done["value"] == version:
        return
    if _SEEDS.is_file():
        for item in json.loads(_SEEDS.read_text(encoding="utf-8")):
            add(item["category"], item["text"], evidence=item.get("evidence", ""), source="crew-seed")
    db = _db()
    try:
        db.execute("INSERT INTO memo(key,value) VALUES('seeded',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (version,))
    finally:
        db.close()


def render_for_agents(limit: int = 25) -> str:
    items = [x for x in top(limit + 10) if x["category"] != "ceo"][:limit]  # the CEO's own lessons stay with the CEO
    if not items:
        return ""
    return "\n".join(f"- ({x['category']}) {x['text']}" for x in items)


# ------------------------------------------------------------------ the CEO's effort record

def record_effort_outcome(kind: str, size: str, effort: str, rounds: int, minutes: float, tokens: int,
                          project: str = "") -> None:
    """One finished task: how hard its builder thought, and how it went (first-time approval, time, tokens)."""
    if effort not in EFFORT_ORDER:
        return
    db = _db()
    try:
        db.execute("INSERT INTO effort_outcomes(ts,project,kind,size,effort,rounds,first_pass,minutes,tokens) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (now(), project, kind or "build", size or "M", effort, int(rounds or 0), int((rounds or 0) == 0),
                    round(float(minutes or 0), 1), int(tokens or 0)))
    finally:
        db.close()


def effort_stats() -> list[dict]:
    """Per (kind, size, effort): how many tasks, share approved first time, typical minutes and tokens."""
    db = _db()
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT kind, size, effort, COUNT(*) AS n, AVG(first_pass) AS first_pass, AVG(minutes) AS minutes, "
            "AVG(tokens) AS tokens, AVG(rounds) AS rounds FROM effort_outcomes GROUP BY kind, size, effort")]
    finally:
        db.close()
    rows.sort(key=lambda r: (r["kind"], r["size"], EFFORT_ORDER.index(r["effort"])))
    return rows


def render_for_ceo(limit: int = 12) -> str:
    """What the CEO has learned about effort: its own record plus its written lessons."""
    lines = []
    stats = effort_stats()
    if stats:
        lines.append("Your record so far (task kind, size, effort → tasks, approved first time, typical minutes, "
                     "typical tokens):")
        for r in stats:
            lines.append(f"- {r['kind']} {r['size']} at {r['effort']}: {r['n']} task(s), "
                         f"{round(100 * (r['first_pass'] or 0))}% first time, ~{round(r['minutes'] or 0)} min, "
                         f"~{int(r['tokens'] or 0) // 1000}k tokens")
    own = top(limit, categories=("ceo",))
    if own:
        lines.append("Your lessons:")
        lines += [f"- {x['text']}" for x in own]
    return "\n".join(lines)


def derive_ceo_lessons(min_tasks: int = 3) -> list[str]:
    """Turn the effort record into plain lessons for the CEO (reinforced as evidence accumulates)."""
    stats = effort_stats()
    by_group: dict[tuple[str, str], list[dict]] = {}
    for r in stats:
        by_group.setdefault((r["kind"], r["size"]), []).append(r)
    written = []
    for (kind, size), rows in by_group.items():
        solid = [r for r in rows if r["n"] >= min_tasks]
        for r in solid:
            rate = r["first_pass"] or 0
            if rate < 0.6:
                text = (f"{kind} tasks of size {size} at {r['effort']} effort were approved first time only "
                        f"{round(100 * rate)}% of the time ({r['n']} tasks): give such work more effort.")
            elif rate >= 0.9:
                lower = [x for x in solid if EFFORT_ORDER.index(x["effort"]) < EFFORT_ORDER.index(r["effort"])
                         and (x["first_pass"] or 0) >= 0.9]
                if lower:
                    continue  # a lower effort already does as well; that lesson is written for it
                text = (f"{kind} tasks of size {size} at {r['effort']} effort were approved first time "
                        f"{round(100 * rate)}% of the time ({r['n']} tasks, ~{round(r['minutes'] or 0)} min): "
                        f"{r['effort']} is enough for this kind of work.")
            else:
                continue
            add("ceo", text, evidence="effort record", source="crew-effort-record")
            written.append(text)
    return written


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
