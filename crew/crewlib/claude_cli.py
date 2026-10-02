"""Find the newest Claude Code on this computer, and keep it up to date.

New models need a recent Claude Code ("version 2.1.280 or newer is required"). A computer
can have several copies (the official installer, npm, an old download), and the first one
on PATH is not always the newest, so Crew compares them and uses the newest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .util import atomic_write, crew_home

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
REQUIRED_RE = re.compile(r"version\s+(\d+\.\d+\.\d+)\s+or newer is required", re.I)
_cache: dict = {"at": 0.0, "path": None, "version": None}
_lock = threading.Lock()


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.search(text or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def version_text(v: tuple[int, int, int] | None) -> str:
    return ".".join(map(str, v)) if v else "unknown"


def required_version(error_text: str) -> str | None:
    """The version an API error asks for ('... version 2.1.280 or newer is required ...'), if any."""
    m = REQUIRED_RE.search(error_text or "")
    return m.group(1) if m else None


def _candidates() -> list[str]:
    home = Path.home()
    found = [shutil.which("claude")]
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        found += [str(home / ".local" / "bin" / "claude.exe"), str(local / "Programs" / "claude" / "claude.exe"),
                  str(appdata / "npm" / "claude.cmd")]
    else:
        found += [str(home / ".local" / "bin" / "claude"), str(home / ".claude" / "local" / "claude"),
                  "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
    out: list[str] = []
    for path in found:
        try:
            if path and Path(path).is_file() and path not in out:
                out.append(path)
        except OSError:
            continue  # an inaccessible optional installation must not hide usable copies
    return out


def probe(path: str) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=25,
                              stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(proc.stdout)


def best(refresh: bool = False) -> tuple[str | None, tuple[int, int, int] | None]:
    """(path, version) of the newest Claude Code found; cached for ten minutes."""
    override = os.environ.get("CREW_CLAUDE_BIN")
    if override:
        return override, probe(override)
    with _lock:
        if not refresh and _cache["path"] and time.time() - _cache["at"] < 600:
            return _cache["path"], _cache["version"]
        choice, newest = None, None
        for path in _candidates():
            v = probe(path)
            if v and (newest is None or v > newest):
                choice, newest = path, v
        if choice is None:
            cands = _candidates()
            choice = cands[0] if cands else None
        _cache.update(at=time.time(), path=choice, version=newest)
        return choice, newest


def _state_path() -> Path:
    return crew_home() / "claude-cli.json"


def state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(path: str | None = None) -> tuple[bool, str]:
    """Run `claude update`. Returns (success, plain-language message)."""
    exe = path or best()[0]
    if not exe:
        return False, "Claude Code is not installed. Run the Crew installer."
    before = probe(exe)
    try:
        proc = subprocess.run([exe, "update"], capture_output=True, text=True, timeout=600,
                              stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        output, proc = str(exc), None
    path_after, after = best(refresh=True)
    ok = bool(after) and (before is None or after >= before) and (proc is not None and proc.returncode == 0)
    st = state()
    st.update(last_update=time.time(), last_output=output[-2000:], version=version_text(after), path=path_after)
    atomic_write(_state_path(), json.dumps(st))
    if ok and before and after and after > before:
        return True, f"Claude Code was updated from {version_text(before)} to {version_text(after)}."
    if ok:
        return True, f"Claude Code is up to date ({version_text(after)})."
    return False, ("Claude Code could not update itself. Close Crew, then run the Crew installer again. "
                   f"Details: {output[-300:] or 'no message'}")


def update_if_stale(hours: float = 20) -> tuple[bool, str] | None:
    """Update at most once a day (called when the app starts)."""
    try:
        last = float(state().get("last_update") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if time.time() - last < hours * 3600:
        return None
    return update()
