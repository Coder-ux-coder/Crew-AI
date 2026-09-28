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
    if not isinstance(latest, dict) or not isinstance(latest.get("version"), str):
        out["error"] = "GitHub's answer was not a Crew version (a web proxy may have replaced it). Try again later."
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
        at = float(data.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return data if time.time() - at < minutes * 60 else None


def last_check() -> dict:
    try:
        data = json.loads((crew_home() / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class UpdateError(ValueError):
    """An update that could not be installed, in plain words. Nothing of Crew was changed."""


def _replace(src: Path, dst: Path) -> None:
    """Rename a file over another; tried a few times, because an antivirus program or Windows' search indexer
    often holds a new file for a moment."""
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.4)


def _leftovers() -> None:
    """Tidy what an interrupted update left beside the program files: a copy that was never swapped in
    (name.new) goes; an original (name.old) goes back in place if its file is missing, else it goes."""
    for folder in [ROOT, *(ROOT / d for d in PROGRAM_DIRS)]:
        if not folder.is_dir():
            continue
        for p in list(folder.rglob("*") if folder != ROOT else folder.glob("*")):
            try:
                if not p.is_file() or p.suffix not in (".new", ".old"):
                    continue
                live = p.with_suffix("")
                if p.suffix == ".old" and not live.exists():
                    os.replace(p, live)
                else:
                    p.unlink()
            except OSError:
                pass


def _put_in_place(files: list[tuple[Path, str]]) -> None:
    """Every new file is first copied beside its target (name.new); only then are they swapped in, the old ones
    kept (name.old) until all are in place. If one cannot be replaced — Windows keeps a file that is in use
    locked — every file already swapped goes back: Crew never runs half old and half new."""
    staged: list[tuple[Path, Path, str]] = []
    try:
        for src, rel in files:
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            new = target.with_name(target.name + ".new")
            shutil.copy2(src, new)
            staged.append((target, new, rel))
    except OSError as exc:
        for _, new, _ in staged:
            new.unlink(missing_ok=True)
        raise UpdateError(f"The update could not be copied into Crew's folder ({exc.strerror or exc}). Nothing was "
                          "changed; check that the disk is not full, then try again.") from None
    swapped: list[tuple[Path, Path | None]] = []
    try:
        for target, new, _ in staged:
            old = target.with_name(target.name + ".old") if target.exists() else None
            if old is not None:
                _replace(target, old)
            try:
                _replace(new, target)
            except OSError:
                if old is not None:
                    _replace(old, target)
                raise
            swapped.append((target, old))
    except OSError as exc:
        for target, old in reversed(swapped):
            try:
                if old is not None:
                    _replace(old, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        for _, new, _ in staged:
            new.unlink(missing_ok=True)
        name = Path(getattr(exc, "filename", "") or "").name or "a file"
        raise UpdateError(f"Crew could not replace {name}: another program is using it. Nothing was changed. Close "
                          "that program (or restart the computer) and update again.") from None
    for _, old in swapped:
        if old is not None:
            old.unlink(missing_ok=True)


_installing = threading.Lock()


def install() -> dict:
    """Download the newest version and put it in place of the program files — all of them, or (if that is not
    possible) none, with a plain explanation (UpdateError). Never two at once (a click and the automatic update)."""
    if not _installing.acquire(blocking=False):
        raise UpdateError("Crew is already installing the update. It reopens by itself in a minute or two.")
    try:
        return _install()
    finally:
        _installing.release()


def _install() -> dict:
    try:
        data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=180)
    except Exception as exc:  # noqa: BLE001 — no network, a proxy, GitHub down …
        raise UpdateError(f"The update could not be downloaded ({exc}). Nothing was changed; try again later.") from None
    tmp = Path(tempfile.mkdtemp(prefix="crew-update-"))
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if not names:
                    raise zipfile.BadZipFile("empty")
                top = names[0].split("/")[0]
                zf.extractall(tmp)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError) as exc:
            raise UpdateError(f"The download was incomplete or damaged ({exc}). Nothing was changed; try again.") from None
        except OSError as exc:
            raise UpdateError(f"The update could not be unpacked ({exc.strerror or exc}). Nothing was changed; check "
                              "that the disk is not full, then try again.") from None
        source = tmp / top / "crew"
        if not (source / "crewlib" / "cli.py").is_file():
            raise UpdateError("The download did not contain Crew. Nothing was changed.")
        if ROOT.resolve() == source.resolve():
            raise UpdateError("Nothing to update.")
        try:
            new_version = json.loads((source / "VERSION.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            raise UpdateError("The download is not a complete version of Crew (its version file is missing). "
                              "Nothing was changed.") from None
        old_requirements = _text(ROOT / "requirements-app.txt")
        new_requirements = _text(source / "requirements-app.txt")
        try:
            previous = set(json.loads((ROOT / MANIFEST).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            previous = set()
        files = []
        for path in source.rglob("*"):
            rel = path.relative_to(source)
            if path.is_file() and not any(part in SKIP for part in rel.parts):
                files.append((path, rel.as_posix()))
        files.sort(key=lambda f: f[1])  # the same order every time (the folder listing's order is not)
        _leftovers()
        _put_in_place(files)
        installed = [rel for _, rel in files]
        # Program files an older version had and this one no longer ships (never anything of the owner's).
        for rel in sorted(previous - set(installed)):
            old_file = (ROOT / rel).resolve()
            if ROOT.resolve() in old_file.parents and old_file.is_file() and rel.split("/")[0] in PROGRAM_DIRS:
                try:
                    old_file.unlink()
                except OSError:
                    pass  # an unused old file does no harm; it goes at the next update
        atomic_write(ROOT / MANIFEST, json.dumps(sorted(installed)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if new_requirements and new_requirements != old_requirements:
        try:  # the add-ons (browser, pictures); Crew works without them, so this never stops an update
            subprocess.run([sys.executable.replace("pythonw", "python"), "-m", "pip", "install", "--user", "--quiet",
                            "--disable-pip-version-check", "-r", str(ROOT / "requirements-app.txt")],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"update: the add-ons were not updated ({exc}); Crew works without them")
    return {"version": new_version}


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


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
