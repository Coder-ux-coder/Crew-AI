"""The team's shared state: one SQLite file per run.

Every agent's tool server and the orchestrator open the same file. WAL mode
lets them read concurrently; writes that check-then-set (claiming a task,
taking a lease) run inside BEGIN IMMEDIATE so two agents can never both win.
"""

from __future__ import annotations

import fnmatch
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .tiers import TIERS, default_tier
from .util import dumps, loads, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    task_id INTEGER,
    urgent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seats (
    name TEXT PRIMARY KEY,
    vendor TEXT, role TEXT, account TEXT, model TEXT,
    status TEXT DEFAULT 'starting',
    session_id TEXT, worktree TEXT, current_task INTEGER,
    chat_cursor INTEGER DEFAULT 0,
    chat_used INTEGER DEFAULT 0,
    last_event_at REAL, last_progress_at REAL,
    turns INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
    restarts INTEGER DEFAULT 0, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    vendor TEXT, profile TEXT,
    status TEXT DEFAULT 'unknown',
    mode TEXT DEFAULT 'normal',
    util_5h REAL, reset_5h INTEGER, util_7d REAL, reset_7d INTEGER,
    parked_until INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT NOT NULL DEFAULT '[]',
    size TEXT NOT NULL DEFAULT 'M',
    kind TEXT NOT NULL DEFAULT 'build',
    suggested_owner TEXT,
    status TEXT NOT NULL DEFAULT 'todo',
    owner TEXT, branch TEXT, created_by TEXT,
    created_at REAL, started_at REAL, submitted_at REAL, finished_at REAL,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    summary TEXT, evidence TEXT, review_notes TEXT, block_reason TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT NOT NULL, seat TEXT, task_id INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS shared (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, seat TEXT NOT NULL, title TEXT NOT NULL, path TEXT, content TEXT NOT NULL, audience TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""

OPEN_STATES = ("todo", "in_progress", "review", "approved", "changes", "blocked")
ACTIVE_STATES = ("in_progress", "review", "approved", "changes", "blocked")  # hold their file lease
DONE_STATES = ("merged", "cancelled")
SIZES = ("S", "M", "L")
KINDS = ("foundation", "build", "fix", "test", "docs", "research", "verify")
NO_WRITE_KINDS = ("research", "verify")


class StoreError(ValueError):
    pass


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self.db.executescript(SCHEMA)
            for table, column, decl in (("tasks", "effort", "TEXT"), ("seats", "effort", "TEXT"),
                                        ("tasks", "tier", "TEXT"), ("tasks", "twin", "INTEGER"),
                                        ("messages", "recipient", "TEXT"), ("messages", "ref", "INTEGER"),
                                        ("messages", "original", "TEXT")):
                have = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    try:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    except sqlite3.OperationalError:  # another process added it at the same moment
                        pass

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        """Serializable write transaction (other writers wait on busy_timeout)."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ meta

    def get(self, key: str, default=None):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def set(self, key: str, value) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, dumps(value)))

    def orchestrator_alive(self, max_age: float = 60.0) -> bool | None:
        """Is this project's orchestrator running (its heartbeat is recent)? None for a project run by a Crew from
        before the heartbeat existed, which never wrote one."""
        beat = self.get("alive")
        if not isinstance(beat, dict):
            return None
        try:
            fresh = now() - float(beat.get("at") or 0) < max_age
        except (TypeError, ValueError):
            return False
        return fresh and not beat.get("stopped")

    # ------------------------------------------------------------- messages

    def post(self, sender: str, kind: str, text: str, task_id: int | None = None, urgent: bool = False,
             recipient: str | None = None, ref: int | None = None, original: str | None = None) -> int:
        """A team-chat message. With a recipient it is direct: only the owner ("you") and that one agent see it.
        ref and original: the prompt writer's version of an owner's draft keeps the draft's id and the owner's words."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO messages(ts,sender,kind,text,task_id,urgent,recipient,ref,original) VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), sender, kind, text, task_id, 1 if urgent else 0, recipient, ref, original),
            )
            return int(cur.lastrowid)

    def owner_message(self, text: str, to: str | None = None) -> int:
        """The owner's message to the team, or to one agent ("ceo" for the CEO). When the project uses the prompt
        writer it waits as a draft (no agent sees drafts) until the writer has turned it into a clear instruction."""
        if self.get("prompt_writer"):
            return self.post("you", "draft", text, recipient=to or None)
        return self.post("you", "direct" if to else "human", text, urgent=True, recipient=to or None)

    def conversation(self, seat: str, limit: int = 20) -> list[dict]:
        """The owner's private conversation with one agent (or "ceo"), oldest first."""
        rows = self._all("SELECT * FROM messages WHERE ((sender='you' AND recipient=? AND kind='direct') OR "
                         "(sender=? AND recipient='you')) ORDER BY id DESC LIMIT ?", (seat, seat, limit))
        return list(reversed(rows))

    def mark_drafted(self, msg_id: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE messages SET kind='drafted' WHERE id=? AND kind='draft'", (msg_id,))

    def owner_pending(self) -> list[dict]:
        """The owner's messages that still need work from the software: drafts for the prompt writer and
        questions for the CEO (the caller skips questions already answered)."""
        return self._all("SELECT * FROM messages WHERE sender='you' AND (kind='draft' OR (kind='direct' AND "
                         "recipient='ceo')) ORDER BY id")

    def messages_after(self, after_id: int = 0, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?", (after_id, limit))

    def recent_messages(self, limit: int = 50, public_only: bool = False) -> list[dict]:
        """The latest messages. public_only leaves out direct messages between the owner and one agent (for
        anything shown to other agents)."""
        where = "WHERE recipient IS NULL AND kind NOT IN ('draft','drafted') " if public_only else ""
        rows = self._all(f"SELECT * FROM messages {where}ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    def unread(self, seat: str, limit: int = 200) -> list[dict]:
        """What this seat has not read yet: the team chat plus direct messages addressed to it."""
        cursor = (self.seat(seat) or {}).get("chat_cursor") or 0
        return self._all(
            "SELECT * FROM messages WHERE id>? AND sender!=? AND (recipient IS NULL OR recipient=?) "
            "AND kind NOT IN ('draft','drafted') ORDER BY id LIMIT ?",
            (cursor, seat, seat, limit)
        )

    def owner_asks(self, seat: str, messages: list[dict] | None = None) -> list[dict]:
        """The owner's direct messages to this seat that it has not answered yet (any answer it sent the owner
        after a message answers it) — among `messages`, or all of them."""
        row = self._one("SELECT MAX(id) AS last FROM messages WHERE sender=? AND recipient='you'", (seat,))
        last = (row or {}).get("last") or 0
        if messages is None:
            return self._all("SELECT * FROM messages WHERE sender='you' AND recipient=? AND id>? AND kind='direct' "
                             "ORDER BY id", (seat, last))
        return [m for m in messages if m["sender"] == "you" and m.get("recipient") == seat and m["id"] > last
                and m["kind"] == "direct"]

    def mark_read(self, seat: str, up_to: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE seats SET chat_cursor=MAX(COALESCE(chat_cursor,0),?) WHERE name=?", (up_to, seat))

    # ------------------------------------------------------------- shared notes and files

    def add_shared(self, seat: str, title: str, content: str, path: str | None = None,
                   audience: list[str] | None = None) -> int:
        """Something one agent shares with the team: a note (what it learned) or a snapshot of a file."""
        with self.tx() as db:
            cur = db.execute("INSERT INTO shared(ts,seat,title,path,content,audience) VALUES(?,?,?,?,?,?)",
                             (now(), seat, title, path, content, ",".join(audience or []) or None))
            return int(cur.lastrowid)

    def shared(self, shared_id: int) -> dict | None:
        return self._one("SELECT * FROM shared WHERE id=?", (shared_id,))

    def shared_list(self, limit: int = 30) -> list[dict]:
        rows = self._all("SELECT id, ts, seat, title, path, audience, length(content) AS size FROM shared "
                         "ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    def last_message_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM messages")
        return int(row["m"] or 0) if row else 0

    # ----------------------------------------------------------------- seats

    def upsert_seat(self, name: str, **fields) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO seats(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,))
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE seats SET {cols} WHERE name=?", (*fields.values(), name))

    update_seat = upsert_seat

    def seat(self, name: str) -> dict | None:
        return self._one("SELECT * FROM seats WHERE name=?", (name,))

    def seats(self) -> list[dict]:
        return self._all("SELECT * FROM seats ORDER BY rowid")

    def add_seat_usage(self, name: str, tokens: int = 0, cost: float = 0.0, turns: int = 0) -> None:
        with self.tx() as db:
            db.execute("UPDATE seats SET tokens=tokens+?, cost_usd=cost_usd+?, turns=turns+? WHERE name=?",
                       (tokens, cost, turns, name))

    # -------------------------------------------------------------- accounts

    def upsert_account(self, name: str, **fields) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO accounts(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,))
            if fields:
                fields = {**fields, "updated_at": now()}
                cols = ", ".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE accounts SET {cols} WHERE name=?", (*fields.values(), name))

    def account(self, name: str) -> dict | None:
        return self._one("SELECT * FROM accounts WHERE name=?", (name,))

    def accounts(self) -> list[dict]:
        return self._all("SELECT * FROM accounts ORDER BY rowid")

    def add_account_usage(self, name: str, tokens: int = 0, cost: float = 0.0) -> None:
        with self.tx() as db:
            db.execute("UPDATE accounts SET tokens=tokens+?, cost_usd=cost_usd+? WHERE name=?", (tokens, cost, name))

    # ----------------------------------------------------------------- tasks

    def create_task(
        self,
        title: str,
        spec: str,
        acceptance: str,
        scope: list[str],
        depends_on: list[int],
        size: str = "M",
        kind: str = "build",
        suggested_owner: str | None = None,
        created_by: str = "lead",
        tier: str | None = None,
    ) -> int:
        title, spec = (title or "").strip(), (spec or "").strip()
        if not title or not spec:
            raise StoreError("a task needs a title and a spec")
        if size not in SIZES:
            raise StoreError(f"size must be one of {SIZES}")
        if kind not in KINDS:
            raise StoreError(f"kind must be one of {KINDS}")
        tier = tier or default_tier(kind, size)
        if tier not in TIERS:
            raise StoreError(f"tier must be one of {TIERS}")
        scope = [normalize_glob(p) for p in (scope or []) if str(p).strip()]
        if not scope and kind not in NO_WRITE_KINDS:
            raise StoreError("a task that changes files must declare its file scope (paths or globs it may edit)")
        deps = sorted({int(d) for d in (depends_on or [])})
        with self.tx() as db:
            for dep in deps:
                row = db.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()
                if row is None:
                    raise StoreError(f"depends_on refers to unknown task #{dep}")
                if row["status"] == "cancelled":
                    raise StoreError(f"depends_on refers to cancelled task #{dep}")
            cur = db.execute(
                """INSERT INTO tasks(title,spec,acceptance,scope,depends_on,size,kind,suggested_owner,
                   status,created_by,created_at,tier) VALUES(?,?,?,?,?,?,?,?,'todo',?,?,?)""",
                (title, spec, acceptance or "", dumps(scope), dumps(deps), size, kind,
                 suggested_owner, created_by, now(), tier),
            )
            return int(cur.lastrowid)

    def task(self, task_id: int) -> dict | None:
        row = self._one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
        return _decode_task(row) if row else None

    def tasks(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self._all(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", statuses)
        else:
            rows = self._all("SELECT * FROM tasks ORDER BY id")
        return [_decode_task(r) for r in rows]

    def update_task(self, task_id: int, **fields) -> None:
        if not fields:
            return
        for key in ("scope", "depends_on"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = dumps(fields[key])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), int(task_id)))

    def append_note(self, task_id: int, who: str, note: str) -> None:
        from .util import hhmm

        line = f"[{hhmm(now())} {who}] {note.strip()}\n"
        with self.tx() as db:
            db.execute("UPDATE tasks SET notes=notes||? WHERE id=?", (line, int(task_id)))

    def add_task_usage(self, task_id: int, tokens: int = 0, cost: float = 0.0) -> None:
        with self.tx() as db:
            db.execute("UPDATE tasks SET tokens=tokens+?, cost_usd=cost_usd+? WHERE id=?", (tokens, cost, int(task_id)))

    def deps_done(self, task: dict) -> bool:
        for dep in task["depends_on"]:
            other = self.task(dep)
            if not other or other["status"] != "merged":
                return False
        return True

    def lease_conflicts(self, task: dict) -> list[dict]:
        """Active tasks (other than this one) whose file scope overlaps this task's scope."""
        if not task["scope"]:
            return []
        clashes = []
        for other in self.tasks(ACTIVE_STATES):
            if other["id"] in (task["id"], task.get("twin")) or not other["scope"]:
                continue  # head-to-head twins build the same files on separate branches
            if scopes_overlap(task["scope"], other["scope"]):
                clashes.append(other)
        return clashes

    def ready_tasks(self) -> list[dict]:
        """To-do tasks whose dependencies are merged and whose files are free."""
        return [t for t in self.tasks(("todo",)) if self.deps_done(t) and not self.lease_conflicts(t)]

    def start_task(self, task_id: int, owner: str, branch: str) -> dict:
        """Atomically give a to-do task to a seat. Raises if it is not claimable."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
            if row is None:
                raise StoreError(f"no task #{task_id}")
            task = _decode_task(dict(row))
            if task["status"] not in ("todo", "changes"):
                raise StoreError(f"task #{task_id} is {task['status']}, not claimable")
            for dep in task["depends_on"]:
                d = db.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()
                if d is None or d["status"] != "merged":
                    raise StoreError(f"task #{task_id} waits on task #{dep}")
            if task["scope"]:
                marks = ",".join("?" * len(ACTIVE_STATES))
                for other in db.execute(f"SELECT * FROM tasks WHERE status IN ({marks}) AND id!=? AND id!=?",
                                        (*ACTIVE_STATES, task["id"], task.get("twin") or -1)).fetchall():
                    o = _decode_task(dict(other))
                    if o["scope"] and scopes_overlap(task["scope"], o["scope"]):
                        raise StoreError(f"task #{task_id} overlaps files held by task #{o['id']} ({o['owner']})")
            db.execute(
                "UPDATE tasks SET status='in_progress', owner=?, branch=?, started_at=COALESCE(started_at,?), "
                "attempts=attempts+1 WHERE id=?",
                (owner, branch, now(), task["id"]),
            )
        return self.task(task_id)

    # ---------------------------------------------------------------- events

    def event(self, kind: str, seat: str | None = None, task_id: int | None = None, **data) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events(ts,kind,seat,task_id,data) VALUES(?,?,?,?,?)",
                       (now(), kind, seat, task_id, dumps(data)))

    def events(self, kind: str | None = None, limit: int = 1000) -> list[dict]:
        if kind:
            rows = self._all("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        else:
            rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = loads(r["data"], {})
        return list(reversed(rows))


# ------------------------------------------------------------------ helpers


def _decode_task(row: dict) -> dict:
    row["scope"] = loads(row.get("scope"), []) or []
    row["depends_on"] = loads(row.get("depends_on"), []) or []
    if not row.get("tier"):  # a task from an older run: the tier its kind and size imply
        row["tier"] = default_tier(row.get("kind"), row.get("size"))
    return row


def normalize_glob(pattern: str) -> str:
    p = str(pattern).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/") or "**"


def _static_prefix(pattern: str) -> str:
    """The literal part of a glob before the first wildcard ('src/api/**' -> 'src/api/')."""
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


def _path_prefix(a: str, b: str) -> bool:
    """True if path a is b or an ancestor directory of b (component-wise)."""
    a = a.rstrip("/")
    return a == "" or b == a or b.startswith(a + "/")


def globs_overlap(a: str, b: str) -> bool:
    """Conservative: may say 'overlap' when unsure (that only serializes work, never corrupts it)."""
    if a == b:
        return True
    sa, sb = _static_prefix(a), _static_prefix(b)
    wild_a, wild_b = sa != a, sb != b
    if not wild_a and not wild_b:
        return _path_prefix(a, b) or _path_prefix(b, a)
    if wild_a and fnmatch.fnmatchcase(b if not wild_b else sb, a):
        return True
    if wild_b and fnmatch.fnmatchcase(a if not wild_a else sa, b):
        return True
    # Two wildcards (or a wildcard and a directory): overlap if one literal prefix contains the other.
    return _path_prefix(sa, sb) or _path_prefix(sb, sa)


def scopes_overlap(a: list[str], b: list[str]) -> bool:
    return any(globs_overlap(x, y) for x in a for y in b)
