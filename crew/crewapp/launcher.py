"""Opening Crew — from the desktop icon, the Start menu or a terminal — the same way every time, never silently.

Crew keeps running in the background after its window is closed (so workflows run and the phone can reach it).
Opening it again must therefore find that running Crew and show its window; if it has stopped answering, replace
it; and every icon must start the same, current copy of Crew with a Python that still exists. Every launch is
written to ~/.crew/app.log, so a problem can always be traced.

  find_running()     the port of a Crew that answers (asked directly — never through the office's web proxy)
  stuck_server()     a Crew that holds its port but no longer answers
  open_window()      Crew's own window (Edge or Chrome app mode), else the default browser, else a message
  start_detached()   from a terminal on Windows: Crew runs on its own, so closing the terminal never stops it
  heal_shortcuts()   on Windows: every Crew icon starts this copy of Crew with this Python
  allow_devices()    on Windows: Edge and Chrome let Crew's own window use the microphone and paste without asking
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from crewlib.util import atomic_write, crew_home

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000  # Windows: run a helper (PowerShell, taskkill) without flashing a console window
DETACHED = 0x00000008 | 0x00000200  # Windows: no console, own process group
BREAKAWAY = 0x01000000  # Windows: leave the terminal's job, so closing the terminal cannot end Crew
BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")


def log(message: str) -> None:
    """To the terminal, or to ~/.crew/app.log when Crew was started from an icon (see crewlib/__main__.py)."""
    try:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)
    except Exception:  # noqa: BLE001 — logging must never stop Crew from opening
        pass


def alert(message: str) -> None:
    log(message)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Crew", 0x40)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ is Crew running?


def listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def answers(port: int, timeout: float = 3.0) -> bool:
    """Is Crew answering on this port? Asked directly: an office web proxy cannot reach this computer's own
    programs, so going through it would wrongly report that Crew is not running."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path, wait in (("/api/ping", timeout), ("/api/health", max(timeout, 10.0))):
        try:
            with opener.open(f"http://127.0.0.1:{port}{path}", timeout=wait) as resp:
                return "version" in json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # a Crew older than 2.3 has no /api/ping: ask it the slower way
                return False
        except Exception:  # noqa: BLE001 — anything but a proper answer means "not a working Crew"
            return False
    return False


def state_path() -> Path:
    return crew_home() / "running.json"  # (app.json holds the phone pairing)


def record(port: int, state: str = "running") -> None:
    """Which Crew runs (or is starting), where: read by the next launch."""
    try:
        atomic_write(state_path(), json.dumps({"pid": os.getpid(), "port": port, "state": state, "root": str(ROOT),
                                               "python": sys.executable, "started": time.time()}))
    except OSError as exc:
        log(f"could not record the running Crew: {exc}")


def recorded() -> dict:
    """The last record of which Crew runs (a damaged record reads as none: Crew then simply looks for itself)."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value, default: float = 0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _ports(first: int) -> list[int]:
    rec = recorded().get("port")
    ports = [int(rec)] if isinstance(rec, int) and 0 < rec < 65536 else []
    return ports + [p for p in range(first, first + 10) if p not in ports]


def find_running(first_port: int = 8765) -> int | None:
    """The port of a Crew that answers (the one recorded last is asked first)."""
    for port in _ports(first_port):
        if listening(port) and (answers(port) or answers(port, 5.0)):
            return port
    return None


def wait_running(first_port: int, seconds: float) -> int | None:
    """Wait for a Crew that is starting to answer, on whichever port it ends up."""
    end = time.time() + seconds
    while time.time() < end:
        found = find_running(first_port)
        if found:
            return found
        time.sleep(1.0)
    return None


def starting_elsewhere() -> dict | None:
    """Another Crew that is starting right now (the icon clicked twice, or Crew restarting after an update)."""
    rec = recorded()
    pid = int(_number(rec.get("pid")))
    if rec.get("state") != "starting" or pid == os.getpid() or time.time() - _number(rec.get("started")) > 300:
        return None
    return rec if pid_alive(pid) and is_crew_app(pid) else None


def wait_for(port: int, seconds: float) -> bool:
    """A program holds this port: if it is a Crew that is still starting up, wait until it answers."""
    end = time.time() + seconds
    while time.time() < end:
        if answers(port, 2.0):
            return True
        if not listening(port):
            return False
        time.sleep(0.5)
    return False


# ------------------------------------------------------------------ a Crew that stopped answering


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError):  # OverflowError: a number no process can have (a hand-edited record)
        return False


def command_line(pid: int) -> str:
    try:
        if os.name == "nt":
            return subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=NO_WINDOW).stdout
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def is_crew_app(pid: int) -> bool:
    """Only ever stop a process that really is Crew's app (a process number can be reused by another program)."""
    line = command_line(pid)
    return "crewlib" in line and " app" in line and pid != os.getpid()


def stuck_server(first_port: int = 8765) -> int | None:
    """The process of the last Crew, if it still holds its port but no longer answers."""
    rec = recorded()
    pid, port = int(_number(rec.get("pid"))), int(_number(rec.get("port"), first_port))
    if rec.get("state") == "starting" or pid == os.getpid() or not 0 < port < 65536:
        return None
    if not pid_alive(pid) or not listening(port) or wait_for(port, 8):
        return None
    return pid if is_crew_app(pid) else None


def stop_process(pid: int, port: int | None = None) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30,
                           creationflags=NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"could not stop process {pid}: {exc}")
    for _ in range(40):  # up to 10 s for its port to be free again
        if not pid_alive(pid) and (port is None or not listening(port)):
            return
        time.sleep(0.25)


# ------------------------------------------------------------------ the window


def open_window(url: str) -> bool:
    """Crew in its own app window (Edge or Chrome app mode), else the default browser; if even that fails, a
    message with the address, so the owner is never left with nothing."""
    if os.name == "nt":
        for raw in BROWSERS:
            exe = os.path.expandvars(raw)
            if not os.path.isfile(exe):
                continue
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1360,880"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                log(f"window: opened {url} with {exe}")
                return True
            except OSError as exc:
                log(f"window: {exe} did not start ({exc})")
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            log(f"window: opened {url} in the default browser")
            return True
        except OSError as exc:
            log(f"window: the default browser did not start ({exc})")
    else:
        try:
            if webbrowser.open(url):
                return True
        except Exception as exc:  # noqa: BLE001
            log(f"window: {exc}")
    alert(f"Crew is running, but its window could not be opened.\n\nOpen this address in your web browser:\n{url}")
    return False


# ------------------------------------------------------------------ started from a terminal


def pythonw() -> Path | None:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if os.name == "nt" and candidate.is_file() else None


def start_detached(port: int, phone: bool, open_it: bool) -> int | None:
    """Windows, from a terminal: start Crew on its own (no console), wait until it answers, and return — closing
    the terminal then never stops Crew. None when that is not possible (the caller runs Crew in the terminal)."""
    running = find_running(port)
    if running:
        url = f"http://localhost:{running}"
        print(f"Crew is already running: {url}")
        if open_it:
            open_window(url)
        return 0
    pyw = pythonw()
    if pyw is None:
        return None
    cmd = [str(pyw), "-X", "utf8", "-m", "crewlib", "app", "--port", str(port)]
    cmd += (["--phone"] if phone else []) + ([] if open_it else ["--no-open"])
    for flags in (DETACHED | BREAKAWAY, DETACHED):  # a terminal may not allow leaving its job; then just detach
        try:
            subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
            break
        except OSError as exc:
            log(f"start: {exc}")
    else:
        return None
    print("Starting Crew. It keeps running when you close this window …")
    for _ in range(120):
        time.sleep(0.5)
        found = find_running(port)
        if found:
            print(f"Crew is running: http://localhost:{found}  (you can close this window)")
            return 0
    print(f"Crew did not answer yet. What happened is written in {crew_home() / 'app.log'}")
    return 1


# ------------------------------------------------------------------ the icons


def _ps(text) -> str:
    return str(text).replace("'", "''")


def heal_shortcuts() -> list[str]:
    """Windows: the desktop, Start-menu and Startup icons start this copy of Crew with this Python. An icon that
    points at a Python that was removed, or at another (older) copy of Crew, would otherwise do nothing at all.
    Only icons that exist are corrected; returns what was changed."""
    if os.name != "nt":
        return []
    if str(ROOT).lower().startswith(tempfile.gettempdir().lower()):
        return []  # a temporary copy: leave the icons alone
    target = pythonw() or Path(sys.executable)
    icon = ROOT / "crewapp" / "static" / "icons" / "crew.ico"
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$t='{_ps(target)}';$d='{_ps(ROOT)}';$i='{_ps(icon)},0';"
        "foreach($e in @(@([Environment]::GetFolderPath('Desktop'),'-X utf8 -m crewlib app'),"
        "@([Environment]::GetFolderPath('Programs'),'-X utf8 -m crewlib app'),"
        "@([Environment]::GetFolderPath('Startup'),'-X utf8 -m crewlib app --no-open'))){"
        "$f=Join-Path $e[0] 'Crew.lnk';if(-not(Test-Path $f)){continue};$s=$w.CreateShortcut($f);"
        "if($s.TargetPath -ne $t -or $s.Arguments -ne $e[1] -or $s.WorkingDirectory -ne $d -or $s.IconLocation -ne $i){"
        "$s.TargetPath=$t;$s.Arguments=$e[1];$s.WorkingDirectory=$d;$s.IconLocation=$i;$s.Save();Write-Output $f}}"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                             creationflags=NO_WINDOW).stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"icons: {exc}")
        return []
    fixed = [line.strip() for line in out.splitlines() if line.strip()]
    for f in fixed:
        log(f"icons: {f} now starts {ROOT} with {target}")
    return fixed


BROWSER_POLICIES = (r"Software\Policies\Microsoft\Edge", r"Software\Policies\Google\Chrome")
DEVICE_LISTS = ("AudioCaptureAllowedUrls", "ClipboardAllowedForUrls")  # the microphone; pasting into a live view


def crew_addresses(port: int = 8765) -> list[str]:
    """Crew's own addresses on this computer: every port it may run on, by name and by number."""
    return [f"http://{host}:{p}" for p in sorted({port, *range(8765, 8775)}) for host in ("localhost", "127.0.0.1")]


def device_list(current: list[str], ours: list[str], enabled: bool) -> list[str]:
    """A browser's allow-list with Crew's addresses added (enabled) or taken out; the owner's own entries stay."""
    if enabled:
        return current + [url for url in ours if url not in current]
    return [url for url in current if url not in ours]


def allow_devices(enabled: bool = True, port: int = 8765) -> list[str]:
    """Windows: Edge and Chrome let Crew's own window use the microphone (voice typing, spoken conversations) and
    paste from the clipboard without asking each time, through the browsers' own allow-lists for this Windows user.
    Only Crew's addresses on this computer are listed: pages the team builds open sandboxed, without an address of
    their own, so they never share it. Off: Crew's addresses are taken out again. Returns what changed."""
    if os.name != "nt":
        return []
    import winreg

    ours, changed = crew_addresses(port), []
    for base in BROWSER_POLICIES:
        for name in DEVICE_LISTS:
            path = f"{base}\\{name}"
            opener = winreg.CreateKeyEx if enabled else winreg.OpenKeyEx
            try:
                key = opener(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            except FileNotFoundError:
                continue  # never listed there: nothing to take out
            except OSError as exc:
                log(f"microphone: {path}: {exc}")
                continue
            with key:
                values, i = {}, 0
                while True:
                    try:
                        value_name, data, _kind = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    values[value_name] = data
                    i += 1
                numbered = sorted((n for n in values if n.isdigit()), key=int)
                current = [str(values[n]) for n in numbered]
                wanted = device_list(current, ours, enabled)
                if wanted == current:
                    continue
                for n in numbered:  # the browsers read a list as 1, 2, 3 … up to the first gap: it is written anew
                    winreg.DeleteValue(key, n)
                for n, url in enumerate(wanted, 1):
                    winreg.SetValueEx(key, str(n), 0, winreg.REG_SZ, url)
            if not wanted:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                except OSError:
                    pass
            changed.append(path)
            log(f"microphone: {path} {'allows' if enabled else 'no longer lists'} Crew's own addresses")
    return changed
