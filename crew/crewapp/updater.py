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
import zlib
from pathlib import Path

from crewlib.util import atomic_write, crew_home

ROOT = Path(__file__).resolve().parent.parent  # the installed "crew" folder
REPO = os.environ.get("CREW_UPDATE_REPO", "Coder-ux-coder/Crew-AI")
BRANCH = os.environ.get("CREW_UPDATE_BRANCH", "Crew-AI")
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


def newer(info: dict) -> bool:
    """Is the version found by the last check newer than the one running now?"""
    return bool(info.get("latest")) and _parse(info.get("latest", "0")) > _parse(current().get("version", "0"))


def note_updated(old: str, new: str, auto: bool) -> None:
    """Remembered across the restart, so the reopened window can say that Crew was updated."""
    atomic_write(crew_home() / "updated.json", json.dumps({"from": old, "to": new, "auto": auto, "at": time.time()}))


def recent_update(minutes: float = 30) -> dict | None:
    try:
        data = json.loads((crew_home() / "updated.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if time.time() - float(data.get("at") or 0) < minutes * 60 else None


def last_check() -> dict:
    try:
        return json.loads((crew_home() / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class UpdateError(RuntimeError):
    """An update that could not be installed. The message says why, in plain words."""


STAGE = ".crew-update"  # beside the program files: the same disk, so each file moves in one step


def _retry(fn, *args, tries: int = 6, wait: float = 0.5):
    """Windows keeps a file busy for a moment while an antivirus or Explorer looks at it: try again briefly."""
    for attempt in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(wait)
    return None


def install() -> dict:
    """Download the newest version and put it in place of the program files: all of it, or — if anything goes
    wrong — none of it, so Crew is never left half old and half new. Raises UpdateError with a plain reason."""
    try:
        data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=180)
    except Exception as exc:  # noqa: BLE001 — offline, a proxy, GitHub down: all mean "try later"
        raise UpdateError(f"The new version could not be downloaded ({exc}). Nothing was changed; try again "
                          "later.") from None
    tmp = Path(tempfile.mkdtemp(prefix="crew-update-"))
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                top = names[0].split("/")[0] if names else ""
                zf.extractall(tmp)
        except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as exc:
            raise UpdateError("The download was incomplete (the connection may have dropped). Nothing was changed; "
                              f"try again. ({exc})") from None
        except OSError as exc:
            raise UpdateError(f"The new version could not be unpacked ({exc}). Nothing was changed; free some disk "
                              "space and try again.") from None
        source = tmp / top / "crew"
        if not top or not (source / "crewlib" / "cli.py").is_file():
            raise UpdateError("The download did not contain Crew. Nothing was changed.")
        try:
            new_version = json.loads((source / "VERSION.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            raise UpdateError("The download has no version number. Nothing was changed.") from None
        files = sorted(path.relative_to(source).as_posix() for path in source.rglob("*")
                       if path.is_file() and not any(part in SKIP for part in path.relative_to(source).parts))
        req = ROOT / "requirements-app.txt"
        old_requirements = req.read_text(encoding="utf-8") if req.is_file() else ""
        try:
            previous = set(json.loads((ROOT / MANIFEST).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            previous = set()
        _put_in_place(source, files)
        # Program files an older version had and this one no longer ships (never anything of the owner's).
        for rel in sorted(previous - set(files)):
            old_file = (ROOT / str(rel)).resolve()
            if ROOT.resolve() in old_file.parents and old_file.is_file() and str(rel).split("/")[0] in PROGRAM_DIRS:
                try:
                    old_file.unlink()
                except OSError:  # in use: a leftover file does no harm
                    pass
        atomic_write(ROOT / MANIFEST, json.dumps(files))
        new_requirements = req.read_text(encoding="utf-8") if req.is_file() else ""
        if new_requirements and new_requirements != old_requirements:
            try:
                subprocess.run([sys.executable.replace("pythonw", "python"), "-m", "pip", "install", "--user",
                                "--quiet", "--disable-pip-version-check", "-r", str(req)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=900)
            except (OSError, subprocess.SubprocessError) as exc:  # Crew runs without new add-ons; the page says so
                print(f"update: the add-ons could not be updated ({exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"version": new_version}


def _put_in_place(source: Path, files: list[str]) -> None:
    """Copy the whole new version beside the program first, then swap the files in. If one will not move, every
    file already swapped is put back: the program is either entirely old or entirely new."""
    stage = ROOT / STAGE
    shutil.rmtree(stage, ignore_errors=True)
    new_dir, old_dir = stage / "new", stage / "old"
    try:
        for rel in files:
            dst = new_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dst)
    except OSError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise UpdateError(f"The new version could not be prepared ({exc}). Nothing was changed; free some disk space "
                          "and try again.") from None
    done: list[tuple[Path, Path | None]] = []
    try:
        for rel in files:
            target, backup = ROOT / rel, None
            if target.exists():
                backup = old_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                _retry(os.replace, target, backup)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _retry(os.replace, new_dir / rel, target)
            except OSError:
                if backup is not None:
                    _retry(os.replace, backup, target)
                raise
            done.append((target, backup))
    except OSError as exc:
        stuck = []
        for target, backup in reversed(done):
            try:
                if backup is not None:
                    _retry(os.replace, backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                stuck.append(target.name)
        name = Path(getattr(exc, "filename", "") or "").name or str(exc)
        if stuck:
            raise UpdateError(f"A program file ({name}) was in use, and {len(stuck)} file(s) could not be put back. "
                              "Run the Crew installer again to repair Crew; your data is safe.") from None
        shutil.rmtree(stage, ignore_errors=True)
        raise UpdateError(f"A program file ({name}) was in use by another program and could not be replaced. Nothing "
                          "was changed; try again in a minute.") from None
    shutil.rmtree(stage, ignore_errors=True)


def restart_later(port: int, delay: float = 1.0) -> None:
    """Start a fresh copy of Crew once this one has closed, then close this one. On Windows the new copy runs
    without a console (pythonw), like the desktop icon: it writes to ~/.crew/app.log and can show a message."""
    exe = Path(sys.executable)
    runner = exe.with_name("pythonw.exe") if os.name == "nt" and exe.with_name("pythonw.exe").is_file() else exe
    script = (
        "import os, socket, subprocess, sys, time\n"
        f"port = {int(port)}\n"
        "for _ in range(120):\n"
        "    s = socket.socket()\n"
        "    try:\n"
        "        s.connect(('127.0.0.1', port)); s.close(); time.sleep(0.5)\n"
        "    except OSError:\n"
        "        break\n"
        f"subprocess.Popen([{str(runner)!r}, '-X', 'utf8', '-m', 'crewlib', 'app', '--port', str(port), '--no-open'],"
        f" cwd={str(ROOT)!r})\n"
    )
    kwargs = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def bye():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
