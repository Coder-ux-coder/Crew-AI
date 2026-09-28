"""Small shared helpers: paths, time, JSON, redaction."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

if sys.version_info < (3, 11):  # tomllib and modern typing
    sys.exit("Crew needs Python 3.11 or newer. Install it from https://www.python.org/downloads/")


def crew_home() -> Path:
    """Where Crew keeps accounts, runs, lessons and your rules (default ~/.crew)."""
    home = Path(os.environ.get("CREW_HOME") or Path.home() / ".crew")
    home.mkdir(parents=True, exist_ok=True)
    return home


def now() -> float:
    return time.time()


def hhmm(ts: float | None) -> str:
    if not ts:
        return "--:--"
    return time.strftime("%H:%M", time.localtime(ts))


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


LOG_LIMIT = 5_000_000  # bytes: a larger log starts again, and the last one is kept as app.log.1


def open_log(path: Path):
    """Crew's log, for appending; one that has grown large starts afresh (the last one is kept as app.log.1).
    (crewlib/__main__.py has its own copy: it runs before anything else is imported.)"""
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:  # not there yet, or held by another Crew window (Windows): carry on appending
        pass
    return open(path, "a", encoding="utf-8", buffering=1)


_problems_lock = threading.Lock()


def db_damaged(exc: BaseException) -> bool:
    """SQLite's answer for a file that is not (or no longer) a database it can read; not for one that is busy,
    locked or missing."""
    if not isinstance(exc, sqlite3.DatabaseError) or isinstance(exc, sqlite3.OperationalError):
        return False
    words = str(exc).lower()
    return type(exc) is sqlite3.DatabaseError or "malformed" in words or "not a database" in words


def open_db(path: Path, prepare, what: str, **connect) -> sqlite3.Connection:
    """Open one of Crew's SQLite files; `prepare` sets it up (tables, settings). A file that cannot be read as a
    database at all is kept under a dated name (never deleted, with its -wal and -shm companions), a new one
    takes its place, and the owner is told (data_problems). `what` names it in the owner's words."""
    for attempt in (1, 2):
        db = sqlite3.connect(str(path), **connect)
        try:
            prepare(db)
            return db
        except sqlite3.DatabaseError as exc:
            db.close()
            if attempt == 2 or not db_damaged(exc):
                raise
            kept = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(path, kept)
            except FileNotFoundError:
                continue  # another Crew process set it aside a moment ago
            except OSError:
                raise exc from None  # held open elsewhere (Windows): not moved, so nothing new is written over it
            for extra in ("-wal", "-shm"):
                try:
                    os.replace(path.with_name(path.name + extra), kept.with_name(kept.name + extra))
                except OSError:
                    pass
            _note_problem(what, kept.name)
    raise AssertionError("unreachable")


def data_problems() -> list[dict]:
    """The files Crew found damaged and set aside, newest first (for the app to tell the owner)."""
    try:
        items = json.loads((crew_home() / "data-problems.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in items if isinstance(x, dict) and x.get("kept")] if isinstance(items, list) else []


def _note_problem(what: str, kept: str) -> None:
    with _problems_lock:
        items = [{"what": what, "kept": kept, "at": time.time()}, *data_problems()][:20]
        try:
            atomic_write(crew_home() / "data-problems.json", json.dumps(items))
        except OSError:
            pass


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + f" …[+{len(text) - limit + 20} chars]"


def tail(text: str, lines: int = 40, chars: int = 4000) -> str:
    """Last lines of a log, bounded, so agents see summaries rather than walls of output."""
    text = (text or "").rstrip()
    out = "\n".join(text.splitlines()[-lines:])
    return out[-chars:]


# --------------------------------------------------------------------- secrets


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file (comments and blank lines ignored, optional quotes)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and value:
            values[key] = value
    return values


class Redactor:
    """Masks secret values wherever text leaves the process (chat, logs, reports)."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._values = sorted(
            {v for v in (secrets or {}).values() if len(v) >= 6}, key=len, reverse=True
        )

    def values(self) -> list[str]:
        """The secret values themselves (for Crew's own leak scan; never shown anywhere)."""
        return list(self._values)

    def __call__(self, text: str) -> str:
        if not text or not self._values:
            return text
        for value in self._values:
            if value in text:
                text = text.replace(value, "•••")
        return text
