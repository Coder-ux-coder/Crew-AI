"""How much of each subscription is used: the 5-hour and weekly limits Claude reports, and tokens
per day. Written by every Crew process (the app, team runs, the assistant); read by the app.

SQLite at ~/.crew/usage.db (safe with several writers).
"""

from __future__ import annotations

import re
import sqlite3
import time

from .util import crew_home, open_db


def _prepare(db: sqlite3.Connection) -> None:
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS limits (account TEXT PRIMARY KEY, status TEXT, kind TEXT,
            five_util REAL, five_reset INTEGER, week_util REAL, week_reset INTEGER, updated REAL);
        CREATE TABLE IF NOT EXISTS tokens (account TEXT, day TEXT, input INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0, turns INTEGER DEFAULT 0,
            PRIMARY KEY (account, day));
    """)


def _db() -> sqlite3.Connection:
    return open_db(crew_home() / "usage.db", _prepare, "the record of your subscriptions' usage", timeout=30,
                   isolation_level=None)


def record_rate(account: str, info: dict) -> None:
    """Store a Claude Code rate_limit_event (utilization is 0–1)."""
    if not account or not isinstance(info, dict):
        return
    windows = info.get("unifiedWindows") or {}
    five, week = windows.get("five_hour") or {}, windows.get("seven_day") or {}
    if not five and info.get("rateLimitType") == "five_hour":
        five = {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt")}
    if not week and info.get("rateLimitType") in ("seven_day", "seven_day_opus"):
        week = {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt")}
    try:
        db = _db()
        try:
            db.execute(
                "INSERT INTO limits(account,status,kind,five_util,five_reset,week_util,week_reset,updated) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET status=excluded.status, kind=excluded.kind, "
                "five_util=COALESCE(excluded.five_util, limits.five_util), "
                "five_reset=COALESCE(excluded.five_reset, limits.five_reset), "
                "week_util=COALESCE(excluded.week_util, limits.week_util), "
                "week_reset=COALESCE(excluded.week_reset, limits.week_reset), updated=excluded.updated",
                (account, info.get("status"), info.get("rateLimitType"), five.get("utilization"), five.get("resetsAt"),
                 week.get("utilization"), week.get("resetsAt"), time.time()))
        finally:
            db.close()
    except sqlite3.Error:
        pass


def record_tokens(account: str, usage: dict | None) -> None:
    """Add one turn's token use (Claude Code's result.usage) to today's total for the account."""
    if not account or not usage:
        return
    day = time.strftime("%Y-%m-%d")
    vals = (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0))
    try:
        db = _db()
        try:
            db.execute(
                "INSERT INTO tokens(account,day,input,output,cache_read,cache_write,turns) VALUES(?,?,?,?,?,?,1) "
                "ON CONFLICT(account,day) DO UPDATE SET input=input+excluded.input, output=output+excluded.output, "
                "cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write, "
                "turns=turns+1", (account, day, *vals))
        finally:
            db.close()
    except sqlite3.Error:
        pass


_RESET_EPOCH = re.compile(r"\|(\d{10})\b")  # Claude Code: "Claude AI usage limit reached|1790000000"
_RESET_IN = re.compile(r"(?:try again|resets?|available again)\s+in\s+(\d+\s*[a-z]+(?:(?:\s*,\s*|\s+and\s+|\s+)"
                       r"\d+\s*[a-z]+)*)", re.I)  # Codex: "… or try again in 2 hours 5 minutes."
_RESET_AT = re.compile(r"(?:try again at|resets?(?: at)?)\s+(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\b)?", re.I)


def limit_resets_at(text: str, at: float | None = None) -> float | None:
    """When a usage limit lifts, from the words of its message ("…|1790000000", "try again in 2 hours 5 minutes",
    "resets 3pm"); None when the message does not say."""
    at = time.time() if at is None else at
    text = text or ""
    m = _RESET_EPOCH.search(text)
    if m:
        return int(m.group(1))
    m = _RESET_IN.search(text)
    if m:
        def unit(word: str) -> int:
            w = word.lower()
            return 60 if w == "m" else next((n for k, n in (("w", 604800), ("d", 86400), ("h", 3600), ("mi", 60),
                                                            ("s", 1)) if w.startswith(k)), 0)
        total = sum(int(n) * unit(u) for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I))
        return at + total if total else None
    m = _RESET_AT.search(text)
    if m:
        if not (m.group(2) or m.group(3)):
            return None  # "resets 3 days …": not a time of day
        hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        hour += 12 if half == "p" and hour < 12 else -12 if half == "a" and hour == 12 else 0
        if hour > 23 or minute > 59:
            return None
        day = time.localtime(at)
        when = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0, 0, 0, -1))
        return when if when > at else when + 86400
    return None


def limited_until(lim: dict | None, at: float | None = None) -> float:
    """When a subscription at its usage limit has room again (0: it is not at its limit), from its recorded
    limits (as snapshot() gives them: a window already past its reset counts as empty). Both windows count: the
    5-hour one and the weekly one."""
    if not lim:
        return 0.0
    at = time.time() if at is None else at
    five, week = float(lim.get("five_reset") or 0), float(lim.get("week_reset") or 0)
    ends = [end for end, used in ((five, lim.get("five_util")), (week, lim.get("week_util")))
            if end > at and float(used or 0) >= 1.0]
    if lim.get("status") == "rejected":  # refused: until the window it hit resets
        hit = week if str(lim.get("kind") or "").startswith("seven_day") else five
        if hit > at:
            ends.append(hit)
    return max(ends, default=0.0)


def snapshot(days: int = 7) -> dict:
    """Everything the Usage screen shows: limits per account and tokens for the last `days` days. Figures that
    cannot be read (the file is held by another program, the disk fails) count as none: they only advise."""
    try:
        db = _db()
        try:
            limits = {r["account"]: dict(r) for r in db.execute("SELECT * FROM limits")}
            since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
            rows = [dict(r) for r in db.execute("SELECT * FROM tokens WHERE day >= ? ORDER BY day", (since,))]
        finally:
            db.close()
    except sqlite3.Error:
        limits, rows = {}, []
    now = time.time()
    for lim in limits.values():  # a window that has reset since we last heard counts as empty
        if lim.get("five_reset") and lim["five_reset"] < now:
            lim["five_util"], lim["five_reset"] = 0.0, None
        if lim.get("week_reset") and lim["week_reset"] < now:
            lim["week_util"], lim["week_reset"] = 0.0, None
        lim["limited_until"] = limited_until(lim, now) or None
        if lim.get("status") == "rejected" and not lim["limited_until"]:
            lim["status"] = "allowed"  # the limit it reached has lifted since it was reported
    today = time.strftime("%Y-%m-%d")
    per_account: dict[str, dict] = {}
    for r in rows:
        acc = per_account.setdefault(r["account"], {"today": 0, "week": 0, "days": {}})
        total = r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
        acc["week"] += total
        acc["days"][r["day"]] = {"total": total, "output": r["output"], "turns": r["turns"]}
        if r["day"] == today:
            acc["today"] = total
    return {"limits": limits, "tokens": per_account, "now": now}
