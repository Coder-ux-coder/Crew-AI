"""Workflows: saved jobs you run with one click or on a schedule (every morning, every Monday, every two
hours …). Each run is an ordinary conversation (Claude or ChatGPT) or a team project, kept in the run history.

Schedules run while Crew is running on this computer (Settings → start Crew with Windows keeps it available).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta

from crewlib.util import clip, now

from .sse import hub

TEMPLATES = [
    {"name": "Morning briefing", "engine": "claude",
     "prompt": "Search the news from the last 24 hours on Pakistan's economy, Punjab's industry and investment into "
               "Pakistan. Give me a one-page briefing: the five most important items, why each matters for the "
               "Punjab Board of Investment, and the source links.",
     "schedule": {"kind": "daily", "time": "08:00", "days": [0, 1, 2, 3, 4]}},
    {"name": "Weekly investor round-up", "engine": "claude",
     "prompt": "Summarise this week's announcements about foreign and domestic investment in Pakistan (new projects, "
               "MoUs, SEZ news, policy changes). A table with date, investor, sector, amount and source, then three "
               "opportunities worth following up.",
     "schedule": {"kind": "weekly", "time": "09:00", "days": [0]}},
    {"name": "Speech or letter draft", "engine": "claude",
     "prompt": "Draft a formal letter, in my voice as CEO of the Punjab Board of Investment, on the topic I give "
               "when I run this. Ask me for the topic first if it is missing.",
     "schedule": {"kind": "manual"}},
    {"name": "Build a one-page event website", "engine": "team",
     "prompt": "Build a one-page website for an investment event: agenda, speakers, venue map link and a "
               "registration form that saves entries to a CSV file.",
     "schedule": {"kind": "manual"}},
]

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def next_run(schedule: dict, after: float | None = None) -> float | None:
    """The next time this schedule is due (local time), or None for manual workflows."""
    kind = (schedule or {}).get("kind", "manual")
    base = datetime.fromtimestamp(after or time.time())
    if kind == "hourly":
        every = max(1, int(schedule.get("every") or 1))
        return (base + timedelta(hours=every)).replace(second=0, microsecond=0).timestamp()
    if kind == "once":
        try:
            at = datetime.fromisoformat(schedule.get("at", "")).timestamp()
        except ValueError:
            return None
        return at if at > base.timestamp() else None
    if kind in ("daily", "weekly"):
        try:
            hh, mm = (int(x) for x in str(schedule.get("time", "08:00")).split(":", 1))
        except ValueError:
            hh, mm = 8, 0
        days = schedule.get("days")
        if days is None:
            days = list(range(7)) if kind == "daily" else [0]
        days = {int(d) % 7 for d in days} or set(range(7))
        for step in range(0, 8):
            day = base + timedelta(days=step)
            cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > base and cand.weekday() in days:
                return cand.timestamp()
    return None


def describe(schedule: dict) -> str:
    kind = (schedule or {}).get("kind", "manual")
    if kind == "manual":
        return "When you run it"
    if kind == "hourly":
        every = int(schedule.get("every") or 1)
        return "Every hour" if every == 1 else f"Every {every} hours"
    if kind == "once":
        return "Once, " + schedule.get("at", "").replace("T", " at ")
    days = schedule.get("days")
    t = schedule.get("time", "08:00")
    if kind == "daily" and (days is None or len(days) == 7):
        return f"Every day at {t}"
    if days is not None and sorted(days) == [0, 1, 2, 3, 4]:
        return f"Weekdays at {t}"
    names = ", ".join(DAY_NAMES[int(d) % 7] for d in sorted(days or [0]))
    return f"{names} at {t}"


class Workflows:
    def __init__(self, app):
        self.app = app
        self.db = app.chats.db
        self.db.db.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, name TEXT, prompt TEXT, engine TEXT,
                model TEXT, effort TEXT, schedule TEXT, enabled INTEGER DEFAULT 1, created REAL, updated REAL,
                last_run REAL, next_run REAL, runs INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT,
                started REAL, finished REAL, status TEXT, chat_id TEXT, run_id TEXT, summary TEXT, trigger TEXT);
        """)
        have = {r[1] for r in self.db.db.execute("PRAGMA table_info(workflows)")}
        if "account" not in have:  # the subscription the owner chose for this workflow ("" = automatic)
            self.db.db.execute("ALTER TABLE workflows ADD COLUMN account TEXT DEFAULT ''")
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._stop = False

    # ------------------------------------------------------------ storage

    def _row(self, w: dict) -> dict:
        w = dict(w)
        w["schedule"] = json.loads(w.get("schedule") or '{"kind": "manual"}')
        w["enabled"] = bool(w.get("enabled"))
        w["when"] = describe(w["schedule"])
        w["running"] = w["id"] in self._running
        last = self.db.q("SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
        w["last"] = last[0] if last else None
        return w

    def list(self) -> list[dict]:
        return [self._row(w) for w in self.db.q("SELECT * FROM workflows ORDER BY created DESC")]

    def get(self, wid: str) -> dict | None:
        rows = self.db.q("SELECT * FROM workflows WHERE id=?", (wid,))
        return self._row(rows[0]) if rows else None

    def _clean(self, body: dict, current: dict | None = None) -> dict:
        cur = current or {}
        name = " ".join(str(body.get("name", cur.get("name", ""))).split())[:80]
        prompt = str(body.get("prompt", cur.get("prompt", ""))).strip()
        engine = body.get("engine", cur.get("engine", "claude"))
        if not name:
            raise ValueError("Give the workflow a name.")
        if len(prompt) < 5:
            raise ValueError("Write what the workflow should do.")
        if engine not in ("claude", "codex", "team"):
            raise ValueError("Choose Claude, ChatGPT or the team.")
        schedule = body.get("schedule", cur.get("schedule")) or {"kind": "manual"}
        if schedule.get("kind") not in ("manual", "daily", "weekly", "hourly", "once"):
            raise ValueError("Unknown schedule.")
        account = body.get("account", cur.get("account")) or ""
        if account and engine != "team":
            self.app.chats._valid_account(engine, account)  # a subscription of that product, or an error
        return {"name": name, "prompt": prompt, "engine": engine,
                "model": body.get("model", cur.get("model")) or "", "effort": body.get("effort", cur.get("effort")) or "auto",
                "schedule": schedule, "enabled": bool(body.get("enabled", cur.get("enabled", True))),
                "account": "" if engine == "team" else account}

    def create(self, body: dict) -> dict:
        w = self._clean(body)
        wid = uuid.uuid4().hex[:10]
        self.db.x("INSERT INTO workflows(id,name,prompt,engine,model,effort,schedule,enabled,created,updated,next_run,"
                  "account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (wid, w["name"], w["prompt"], w["engine"], w["model"], w["effort"], json.dumps(w["schedule"]),
                   int(w["enabled"]), now(), now(), next_run(w["schedule"]), w["account"]))
        return self.get(wid)

    def update(self, wid: str, body: dict) -> dict:
        cur = self.get(wid)
        if cur is None:
            raise KeyError(wid)
        w = self._clean(body, cur)
        self.db.x("UPDATE workflows SET name=?,prompt=?,engine=?,model=?,effort=?,schedule=?,enabled=?,updated=?,"
                  "next_run=?,account=? WHERE id=?",
                  (w["name"], w["prompt"], w["engine"], w["model"], w["effort"], json.dumps(w["schedule"]),
                   int(w["enabled"]), now(), next_run(w["schedule"]) if w["enabled"] else None, w["account"], wid))
        return self.get(wid)

    def delete(self, wid: str) -> bool:
        self.db.x("DELETE FROM workflow_runs WHERE workflow_id=?", (wid,))
        self.db.x("DELETE FROM workflows WHERE id=?", (wid,))
        return True

    def runs(self, wid: str) -> list[dict]:
        return self.db.q("SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY id DESC LIMIT 50", (wid,))

    # ------------------------------------------------------------ running

    def run(self, wid: str, trigger: str = "you", extra: str = "") -> dict:
        w = self.get(wid)
        if w is None:
            raise KeyError(wid)
        with self._lock:
            if wid in self._running:
                raise ValueError("This workflow is already running.")
            self._running.add(wid)
        rid = self.db.x("INSERT INTO workflow_runs(workflow_id,started,status,trigger) VALUES(?,?,?,?)",
                        (wid, now(), "running", trigger))
        self.db.x("UPDATE workflows SET last_run=?, runs=runs+1 WHERE id=?", (now(), wid))
        prompt = w["prompt"] + (f"\n\n{extra.strip()}" if extra.strip() else "")
        try:
            if w["engine"] == "team":
                run_id = self.app.runs.start(prompt)
                self.db.x("UPDATE workflow_runs SET run_id=? WHERE id=?", (run_id, rid))
                threading.Thread(target=self._watch_project, args=(wid, rid, run_id), daemon=True).start()
                return {"run": rid, "project": run_id}
            chat = self.app.chats.create(engine=w["engine"], model=w["model"] or None, effort=w["effort"] or None,
                                         account=w.get("account") or None)
            self.app.chats.rename(chat["id"], f"{w['name']} · {time.strftime('%d %b %H:%M')}")
            self.app.chats.db.x("UPDATE chats SET kind='workflow' WHERE id=?", (chat["id"],))
            self.db.x("UPDATE workflow_runs SET chat_id=? WHERE id=?", (chat["id"], rid))
            result = self.app.chats.send(chat["id"], prompt)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "could not start")
            threading.Thread(target=self._watch_chat, args=(wid, rid, chat["id"]), daemon=True).start()
            return {"run": rid, "chat": chat["id"]}
        except Exception as exc:
            self._finish(wid, rid, "failed", str(exc))
            raise

    def _finish(self, wid: str, rid: int, status: str, summary: str) -> None:
        self.db.x("UPDATE workflow_runs SET finished=?, status=?, summary=? WHERE id=?",
                  (now(), status, clip(summary, 600), rid))
        with self._lock:
            self._running.discard(wid)
        w = self.get(wid)
        if w:
            hub.publish("app", "notice", {"kind": "workflow", "status": status, "workflow": wid,
                                          "text": f"{w['name']}: " + ("done" if status == "done" else "did not finish"),
                                          "summary": clip(summary, 200)})

    def _watch_chat(self, wid: str, rid: int, cid: str) -> None:
        deadline = time.time() + 6 * 3600
        quiet = 0
        while time.time() < deadline and quiet < 3:  # quiet for a few seconds: not between a retry and its answer
            time.sleep(2)
            s = self.app.chats.sessions.get(cid)
            quiet = quiet + 1 if s is not None and not s.busy and not self.app.chats.updating else 0
        chat = self.app.chats.get(cid) or {}
        answers = [m for m in chat.get("messages", []) if m["role"] == "assistant"]
        last = answers[-1] if answers else None
        ok = bool(last) and not (last.get("meta") or {}).get("error")
        self._finish(wid, rid, "done" if ok else "failed", last["text"] if last else "No answer.")

    def _watch_project(self, wid: str, rid: int, run_id: str) -> None:
        deadline = time.time() + 48 * 3600
        state = {}
        while time.time() < deadline:
            time.sleep(10)
            state = self.app.runs.state(run_id) or {}
            if state.get("raw_phase") in ("done", "stopped", "failed"):
                break
        ok = state.get("raw_phase") == "done"
        self._finish(wid, rid, "done" if ok else "failed", (state.get("report") or state.get("phase") or "")[:600])

    # ------------------------------------------------------------ the clock

    def start_clock(self) -> None:
        threading.Thread(target=self._clock, daemon=True, name="crew-workflows").start()

    def stop(self) -> None:
        self._stop = True

    def _clock(self) -> None:
        while not self._stop:
            time.sleep(20)
            try:
                self.tick()
            except Exception as exc:
                print(f"workflow clock: {exc}")

    def tick(self) -> list[str]:
        """Start every workflow that is due. A run missed while the computer was off happens once, on the next tick."""
        started = []
        for w in self.db.q("SELECT * FROM workflows WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now(),)):
            schedule = json.loads(w["schedule"] or "{}")
            self.db.x("UPDATE workflows SET next_run=? WHERE id=?", (next_run(schedule), w["id"]))
            if w["id"] not in self._running:
                try:
                    self.run(w["id"], trigger="schedule")
                    started.append(w["id"])
                except Exception as exc:  # recorded in the run history
                    print(f"workflow {w['id']} failed to start: {exc}")
        return started
