"""One-click updates: fetch the newest Crew from GitHub, replace the program files, restart.

Only the program folder is replaced. Everything that is yours — settings, API keys, conversations, captures,
projects, sign-ins — lives in your user folder (~/.crew and Claude Code's own folder) and is never touched.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from crewlib.util import atomic_write, crew_home

ROOT = Path(__file__).resolve().parent.parent  # the installed "crew" folder
REPO = os.environ.get("CREW_UPDATE_REPO", "Coder-ux-coder/claude")
BRANCH = os.environ.get("CREW_UPDATE_BRANCH", "claude/nice-ramanujan-saysts")
SKIP = {"__pycache__", ".git", "tests"}
MANIFEST = "installed-files.json"
PROGRAM_DIRS = {"crewlib", "crewapp", "install"}


def current() -> dict:
    try:
        return json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "0.0.0", "date": ""}


def _parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Crew-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check() -> dict:
    """{'current': ..., 'latest': ..., 'available': bool, 'notes': [...]}"""
    mine = current()
    out = {"current": mine.get("version"), "date": mine.get("date"), "available": False, "latest": None, "notes": []}
    try:
        raw = _get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/crew/VERSION.json?t={int(time.time())}")
        latest = json.loads(raw)
    except Exception as exc:
        out["error"] = f"Could not reach GitHub: {exc}"
        return out
    out["latest"] = latest.get("version")
    out["notes"] = latest.get("notes") or []
    out["available"] = _parse(latest.get("version", "0")) > _parse(mine.get("version", "0"))
    st = crew_home() / "update.json"
    atomic_write(st, json.dumps({**out, "checked": time.time()}))
    return out


def last_check() -> dict:
    try:
        return json.loads((crew_home() / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def install() -> dict:
    """Download the newest version and copy it over the program files."""
    data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=180)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        top = zf.namelist()[0].split("/")[0]
        tmp = Path(tempfile.mkdtemp(prefix="crew-update-"))
        zf.extractall(tmp)
    source = tmp / top / "crew"
    if not (source / "crewlib" / "cli.py").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("The download did not contain Crew.")
    new_version = json.loads((source / "VERSION.json").read_text(encoding="utf-8")).get("version")
    old_requirements = (ROOT / "requirements-app.txt").read_text(encoding="utf-8") if (ROOT / "requirements-app.txt").is_file() else ""
    if ROOT.resolve() == source.resolve():
        raise RuntimeError("Nothing to update.")
    try:
        previous = set(json.loads((ROOT / MANIFEST).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        previous = set()
    installed = []
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if any(part in SKIP for part in rel.parts):
            continue
        target = ROOT / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_name(target.name + ".new")
            shutil.copy2(path, tmp_target)
            os.replace(tmp_target, target)
            installed.append(rel.as_posix())
    # Program files an older version had and this one no longer ships (never anything of the owner's).
    for rel in sorted(previous - set(installed)):
        old_file = (ROOT / rel).resolve()
        if ROOT.resolve() in old_file.parents and old_file.is_file() and rel.split("/")[0] in PROGRAM_DIRS:
            old_file.unlink(missing_ok=True)
    atomic_write(ROOT / MANIFEST, json.dumps(sorted(installed)))
    new_requirements = (source / "requirements-app.txt").read_text(encoding="utf-8") if (source / "requirements-app.txt").is_file() else ""
    shutil.rmtree(tmp, ignore_errors=True)
    if new_requirements and new_requirements != old_requirements:
        subprocess.run([sys.executable.replace("pythonw", "python"), "-m", "pip", "install", "--user", "--quiet",
                        "--disable-pip-version-check", "-r", str(ROOT / "requirements-app.txt")],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    return {"version": new_version}


def restart_later(port: int, delay: float = 1.0) -> None:
    """Start a fresh copy of Crew once this one has closed, then close this one."""
    script = (
        "import os, socket, subprocess, sys, time\n"
        f"port = {int(port)}\n"
        "for _ in range(120):\n"
        "    s = socket.socket()\n"
        "    try:\n"
        "        s.connect(('127.0.0.1', port)); s.close(); time.sleep(0.5)\n"
        "    except OSError:\n"
        "        break\n"
        f"subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'crewlib', 'app', '--port', str(port), '--no-open'],"
        f" cwd={str(ROOT)!r})\n"
    )
    kwargs = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def bye():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
