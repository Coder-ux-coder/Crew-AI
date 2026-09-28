"""Your Android phone (e.g. Samsung Galaxy), mirrored and controlled through ADB,
Android's official developer bridge. Works over USB or Wi-Fi ("Wireless
debugging" in Developer options). You and the assistant share the same controls.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from crewlib.util import crew_home

from .sse import hub

KEYCODES = {"home": 3, "back": 4, "recents": 187, "enter": 66, "delete": 67, "power": 26, "volume_up": 24,
            "volume_down": 25, "tab": 61, "search": 84, "menu": 82, "wake": 224, "sleep": 223}


class PhoneError(RuntimeError):
    pass


def adb_path() -> str | None:
    if os.environ.get("CREW_ADB"):
        return os.environ["CREW_ADB"]
    found = shutil.which("adb")
    if found:
        return found
    local = crew_home() / "tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    return str(local) if local.is_file() else None


class PhoneService:
    def __init__(self):
        self.serial: str | None = None
        self.screen: tuple[int, int] | None = None
        self.driver = "you"
        self.driver_at = 0.0
        self._stream: threading.Thread | None = None
        self._lock = threading.Lock()
        self._server_started = False

    # ------------------------------------------------------------------ adb

    def _start_server(self, exe: str) -> None:
        """Start adb's background helper on its own first. If it were started by a command whose output we
        capture, it would inherit that output pipe and keep the command 'running' (a known Windows hang)."""
        if self._server_started:
            return
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        try:
            subprocess.run([exe, "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, **kwargs)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self._server_started = True

    def _adb(self, *args: str, timeout: float = 25, binary: bool = False, device: bool = True):
        exe = adb_path()
        if not exe:
            raise PhoneError("The phone connector (Android platform tools) is not installed. Run the Crew installer.")
        self._start_server(exe)
        cmd = [exe]
        if device:
            serial = self.serial or self._pick()
            cmd += ["-s", serial]
        cmd += list(args)
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            raise PhoneError("The phone did not answer in time. Check that it is unlocked, awake and still "
                             "connected, then try again.") from None
        except OSError as exc:
            raise PhoneError(f"The phone connector could not run ({exc.strerror or exc}). Run the Crew installer "
                             "again to repair it.") from None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).decode(errors="replace").strip()
            raise PhoneError(err or f"adb {' '.join(args)} failed")
        return proc.stdout if binary else proc.stdout.decode(errors="replace")

    def devices(self) -> list[dict]:
        out = self._adb("devices", "-l", device=False)
        items = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            items.append({"serial": parts[0], "state": parts[1],
                          "model": info.get("model", "").replace("_", " "), "wifi": ":" in parts[0]})
        return items

    def _pick(self) -> str:
        ready = [d for d in self.devices() if d["state"] == "device"]
        if not ready:
            raise PhoneError("No phone is connected. Open Phone in the app and follow the steps to connect it.")
        self.serial = ready[0]["serial"]
        self.screen = None
        return self.serial

    def status(self) -> dict:
        if not adb_path():
            return {"available": False, "connected": False,
                    "reason": "The phone connector is not installed yet. Run the Crew installer."}
        try:
            devs = self.devices()
        except (PhoneError, subprocess.TimeoutExpired, OSError) as exc:
            return {"available": True, "connected": False, "reason": str(exc), "devices": []}
        ready = [d for d in devs if d["state"] == "device"]
        if self.serial and self.serial not in [d["serial"] for d in ready]:
            self.serial = None
        unauthorized = [d for d in devs if d["state"] == "unauthorized"]
        ai = self.driver != "you" and time.time() - self.driver_at < 5
        return {"available": True, "connected": bool(ready), "devices": devs,
                "device": (ready[0]["model"] or ready[0]["serial"]) if ready else "",
                "needs_approval": bool(unauthorized), "driver": self.driver if ai else "you",
                "reason": "" if ready else ("Tap ‘Allow’ on your phone’s screen." if unauthorized else
                                            "No phone connected yet.")}

    def pair(self, host_port: str, code: str) -> str:
        if not re.fullmatch(r"[\w.\-]+:\d{2,5}", host_port or "") or not re.fullmatch(r"\d{6}", code or ""):
            raise PhoneError("Enter the IP address and port shown on the phone (like 192.168.1.20:37123) and the "
                             "6-digit pairing code.")
        return self._adb("pair", host_port, code, device=False, timeout=30).strip()

    def connect(self, host_port: str) -> str:
        if not re.fullmatch(r"[\w.\-]+:\d{2,5}", host_port or ""):
            raise PhoneError("Enter the IP address and port shown under Wireless debugging (like 192.168.1.20:41555).")
        out = self._adb("connect", host_port, device=False, timeout=20).strip()
        if "connected" not in out.lower() or "cannot" in out.lower() or "failed" in out.lower():
            raise PhoneError(out or "Could not connect.")
        self.serial = host_port
        self.screen = None
        return out

    def reverse(self, port: int) -> None:
        """Let the phone reach the app at http://localhost:<port> (a secure address, so its microphone works)."""
        self._adb("reverse", f"tcp:{port}", f"tcp:{port}")

    # ---------------------------------------------------------- screen / input

    def size(self) -> tuple[int, int]:
        if self.screen is None:
            out = self._adb("shell", "wm", "size")
            m = re.findall(r"(\d+)x(\d+)", out)
            if not m:
                raise PhoneError("Could not read the phone's screen size.")
            w, h = map(int, m[-1])  # "Override size" (last) wins over "Physical size"
            self.screen = (w, h)
        return self.screen

    def screenshot(self) -> bytes:
        data = self._adb("exec-out", "screencap", "-p", binary=True, timeout=20)
        if not data.startswith(b"\x89PNG"):
            raise PhoneError("The phone did not return a picture (is the screen locked with a secure lock?).")
        return data

    def _mark(self, driver: str) -> None:
        if driver != "you":
            self.driver, self.driver_at = driver, time.time()

    def tap(self, xr: float, yr: float, driver: str = "you") -> dict:
        self._mark(driver)
        w, h = self.size()
        try:
            x, y = int(max(0.0, min(1.0, float(xr))) * w), int(max(0.0, min(1.0, float(yr))) * h)
        except (TypeError, ValueError, OverflowError):
            raise PhoneError("Give the position on the screen as numbers.") from None
        self._adb("shell", "input", "tap", str(x), str(y))
        return {"ok": True, "x": x, "y": y}

    def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300, driver: str = "you") -> dict:
        self._mark(driver)
        w, h = self.size()
        try:
            pts = [str(int(max(0.0, min(1.0, float(v))) * (w if i % 2 == 0 else h)))
                   for i, v in enumerate((x1, y1, x2, y2))]
            ms = max(50, min(5000, int(ms)))
        except (TypeError, ValueError, OverflowError):
            raise PhoneError("Give the swipe's start and end on the screen as numbers.") from None
        self._adb("shell", "input", "swipe", *pts, str(ms))
        return {"ok": True}

    def swipe_dir(self, direction: str, driver: str = "you") -> dict:
        moves = {"up": (0.5, 0.75, 0.5, 0.25), "down": (0.5, 0.25, 0.5, 0.75),
                 "left": (0.85, 0.5, 0.15, 0.5), "right": (0.15, 0.5, 0.85, 0.5)}
        if direction not in moves:
            raise PhoneError("direction must be up, down, left or right")
        return self.swipe(*moves[direction], driver=driver)

    def type(self, text: str, driver: str = "you") -> dict:
        self._mark(driver)
        if any(ord(ch) > 126 for ch in text):
            raise PhoneError("The phone connector can only type plain English characters. Use the phone's own "
                             "keyboard for other languages.")
        escaped = text.replace("\\", "\\\\").replace("'", "'\\''").replace(" ", "%s")
        self._adb("shell", f"input text '{escaped}'")
        return {"ok": True}

    def key(self, name: str, driver: str = "you") -> dict:
        self._mark(driver)
        code = KEYCODES.get(name.lower())
        if code is None:
            raise PhoneError(f"Unknown key '{name}'. Use one of: {', '.join(KEYCODES)}")
        self._adb("shell", "input", "keyevent", str(code))
        return {"ok": True}

    def apps(self) -> list[str]:
        out = self._adb("shell", "pm", "list", "packages", "-3")
        return sorted(line.split(":", 1)[1] for line in out.splitlines() if line.startswith("package:"))

    def open_app(self, name: str, driver: str = "you") -> dict:
        self._mark(driver)
        query = (name or "").lower().replace(" ", "")
        common = {"whatsapp": "com.whatsapp", "chrome": "com.android.chrome", "youtube": "com.google.android.youtube",
                  "gmail": "com.google.android.gm", "maps": "com.google.android.apps.maps",
                  "settings": "com.android.settings", "camera": "com.sec.android.app.camera",
                  "gallery": "com.sec.android.gallery3d", "messages": "com.samsung.android.messaging",
                  "phone": "com.samsung.android.dialer", "calendar": "com.samsung.android.calendar",
                  "playstore": "com.android.vending", "samsunginternet": "com.sec.android.app.sbrowser"}
        package = common.get(query)
        if not package:
            matches = [p for p in self.apps() if query in p.replace(".", "").lower()]
            package = matches[0] if matches else None
        if not package:
            raise PhoneError(f"No app matching '{name}' is installed.")
        self._adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        return {"ok": True, "package": package}

    def elements(self) -> list[dict]:
        """What is on screen: visible text, descriptions and where to tap (for the assistant)."""
        self._adb("shell", "uiautomator", "dump", "/sdcard/crew-ui.xml", timeout=30)
        xml = self._adb("shell", "cat", "/sdcard/crew-ui.xml", timeout=20)
        return parse_ui(xml, self.size())

    def tap_text(self, text: str, driver: str = "you") -> dict:
        want = (text or "").strip().lower()
        items = self.elements()
        best = next((e for e in items if e["text"].lower() == want or e["desc"].lower() == want), None) or \
            next((e for e in items if want in e["text"].lower() or want in e["desc"].lower()), None)
        if not best:
            raise PhoneError(f"Nothing on the phone's screen says '{text}'.")
        return self.tap(best["x"], best["y"], driver=driver)

    # --------------------------------------------------------------- stream

    def ensure_stream(self) -> None:
        with self._lock:
            if self._stream is None or not self._stream.is_alive():
                self._stream = threading.Thread(target=self._stream_loop, daemon=True, name="crew-phone")
                self._stream.start()

    def _stream_loop(self) -> None:
        idle = 0
        while idle < 20:
            if hub.count("phone") == 0:
                idle += 1
                time.sleep(0.5)
                continue
            idle = 0
            try:
                png = self.screenshot()
                data, mime = shrink(png)
                hub.publish("phone", "frame", {"data": base64.b64encode(data).decode(), "mime": mime})
                time.sleep(0.35)
            except (PhoneError, subprocess.TimeoutExpired, OSError) as exc:
                hub.publish("phone", "status", {"connected": False, "reason": str(exc)})
                time.sleep(2)


def shrink(png: bytes) -> tuple[bytes, str]:
    """Make phone frames light enough to stream (uses Pillow when it is installed)."""
    try:
        from PIL import Image
    except ImportError:
        return png, "image/png"
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((540, 1200))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=68)
    return out.getvalue(), "image/jpeg"


def parse_ui(xml: str, size: tuple[int, int]) -> list[dict]:
    start = xml.find("<?xml")
    root = ET.fromstring(xml[start:] if start >= 0 else xml)
    w, h = size
    out = []
    for node in root.iter("node"):
        text, desc = node.get("text", "").strip(), node.get("content-desc", "").strip()
        clickable = node.get("clickable") == "true"
        if not (text or desc or clickable):
            continue
        m = re.findall(r"\d+", node.get("bounds", ""))
        if len(m) != 4:
            continue
        x1, y1, x2, y2 = map(int, m)
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"text": text[:80], "desc": desc[:80], "clickable": clickable,
                    "id": node.get("resource-id", "").split("/")[-1],
                    "x": round((x1 + x2) / 2 / w, 4), "y": round((y1 + y2) / 2 / h, 4)})
    return out[:120]


service = PhoneService()
