"""Small shared helpers: paths, time, JSON, redaction."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
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


def utf8_stdio() -> None:
    """Talk UTF-8 on stdin/stdout whatever the environment says (tool servers speak JSON-RPC, which is UTF-8).

    Codex starts tool servers with only a few environment variables, so on Windows Python would otherwise use
    the console code page (cp1252): a team message with "→" or "✔" (symbols Crew itself writes) then broke every tool call."""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # not a text stream (tests, pythonw): leave it
            pass


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
