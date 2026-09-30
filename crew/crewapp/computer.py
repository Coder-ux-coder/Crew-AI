"""Your Windows computer — its screen, mouse and keyboard — for the assistant, and for you
from your phone. Uses Windows' own functions: nothing extra to install.

Safety valve: push the mouse pointer into the top-left corner of the screen and the
assistant's computer control pauses for 30 seconds (it is told why).
"""

from __future__ import annotations

import base64
import os
import re
import struct
import threading
import time
import zlib

from .sse import hub

MAX_WIDTH = 1280  # pictures for the assistant and the live view
TYPE_CHUNK = 40  # characters typed between two looks at the corner (about a sixth of a second)


class ComputerError(RuntimeError):
    pass


# ------------------------------------------------------------------ pictures


def png(rgb: bytes, width: int, height: int) -> bytes:
    """Encode 8-bit RGB pixels as PNG (standard library only)."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def bgra_to_rgb(buf: bytes, width: int, height: int) -> bytes:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = buf[2::4], buf[1::4], buf[0::4]
    return bytes(rgb)


def encode(rgb: bytes, width: int, height: int) -> tuple[bytes, str]:
    """JPEG when Pillow is installed (much smaller), PNG otherwise."""
    try:
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.frombytes("RGB", (width, height), rgb).save(out, "JPEG", quality=72)
        return out.getvalue(), "image/jpeg"
    except ImportError:
        return png(rgb, width, height), "image/png"


def fit(width: int, height: int, max_width: int = MAX_WIDTH) -> tuple[int, int]:
    if width <= max_width:
        return width, height
    return max_width, max(1, round(height * max_width / width))


# ------------------------------------------------------------------ keys

VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
      "del": 0x2E, "insert": 0x2D, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
      "shift": 0x10, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B, "capslock": 0x14, "printscreen": 0x2C,
      "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD, "playpause": 0xB3}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x2C}


def parse_keys(combo: str) -> list[int]:
    """'ctrl+shift+s' → virtual-key codes, in press order."""
    codes = []
    for part in re.split(r"\s*\+\s*", (combo or "").strip().lower()):
        if not part:
            continue
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ComputerError(f"Unknown key '{part}'. Use names like enter, tab, esc, ctrl+c, alt+tab, win+r, f5.")
    if not codes:
        raise ComputerError("Say which key to press, for example enter or ctrl+s.")
    return codes


# ------------------------------------------------------------------ Windows


class WindowsDesktop:
    """Screen capture with GDI; mouse and keyboard with SendInput."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.ct = ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, so clicks land where the picture says
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        self.u, self.g = u, g
        u.GetDC.argtypes, u.GetDC.restype = [w.HWND], w.HDC
        u.ReleaseDC.argtypes, u.ReleaseDC.restype = [w.HWND, w.HDC], ctypes.c_int
        u.GetSystemMetrics.argtypes, u.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
        u.SetCursorPos.argtypes, u.SetCursorPos.restype = [ctypes.c_int, ctypes.c_int], w.BOOL
        u.GetCursorPos.argtypes, u.GetCursorPos.restype = [ctypes.POINTER(w.POINT)], w.BOOL
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [w.HDC], w.HDC
        g.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = w.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [w.HDC, w.HGDIOBJ], w.HGDIOBJ
        g.SetStretchBltMode.argtypes, g.SetStretchBltMode.restype = [w.HDC, ctypes.c_int], ctypes.c_int
        g.SetBrushOrgEx.argtypes, g.SetBrushOrgEx.restype = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], w.BOOL
        g.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]
        g.StretchBlt.restype = w.BOOL
        g.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
        g.GetDIBits.restype = ctypes.c_int
        g.DeleteObject.argtypes, g.DeleteObject.restype = [w.HGDIOBJ], w.BOOL
        g.DeleteDC.argtypes, g.DeleteDC.restype = [w.HDC], w.BOOL

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", w.DWORD), ("u", UNION)]

        self.INPUT, self.MOUSEINPUT, self.KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        u.SendInput.argtypes, u.SendInput.restype = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int], w.UINT

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                        ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                        ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                        ("biClrImportant", w.DWORD)]

        self.BITMAPINFOHEADER = BITMAPINFOHEADER
        self.w = w

    def size(self) -> tuple[int, int]:
        return self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1)  # the main screen, in real pixels

    def grab(self, width: int, height: int) -> bytes:
        """The main screen scaled to width × height, as BGRA pixels, top row first."""
        sw, sh = self.size()
        screen = self.u.GetDC(None)
        mem = self.g.CreateCompatibleDC(screen)
        bmp = self.g.CreateCompatibleBitmap(screen, width, height)
        old = self.g.SelectObject(mem, bmp)
        try:
            self.g.SetStretchBltMode(mem, 4)  # HALFTONE: smooth downscaling
            self.g.SetBrushOrgEx(mem, 0, 0, None)
            if not self.g.StretchBlt(mem, 0, 0, width, height, screen, 0, 0, sw, sh, 0x00CC0020 | 0x40000000):
                raise ComputerError("Windows did not give a picture of the screen (is it locked?).")
            header = self.BITMAPINFOHEADER(biSize=40, biWidth=width, biHeight=-height, biPlanes=1, biBitCount=32,
                                           biCompression=0)
            buf = self.ct.create_string_buffer(width * height * 4)
            if not self.g.GetDIBits(mem, bmp, 0, height, buf, self.ct.byref(header), 0):
                raise ComputerError("Could not read the screen picture.")
            return buf.raw
        finally:
            self.g.SelectObject(mem, old)
            self.g.DeleteObject(bmp)
            self.g.DeleteDC(mem)
            self.u.ReleaseDC(None, screen)

    def cursor(self) -> tuple[int, int]:
        pt = self.w.POINT()
        self.u.GetCursorPos(self.ct.byref(pt))
        return pt.x, pt.y

    def _send(self, *inputs) -> None:
        arr = (self.INPUT * len(inputs))(*inputs)
        if self.u.SendInput(len(inputs), arr, self.ct.sizeof(self.INPUT)) != len(inputs):
            raise ComputerError("Windows blocked the input (a window running as administrator cannot be controlled).")

    def _mouse(self, flags: int, data: int = 0):
        inp = self.INPUT(type=0)
        inp.u.mi = self.MOUSEINPUT(dx=0, dy=0, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def _key(self, vk: int = 0, scan: int = 0, flags: int = 0):
        inp = self.INPUT(type=1)
        inp.u.ki = self.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def move(self, x: int, y: int) -> None:
        self.u.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        down, up = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40)}[button]
        self.move(x, y)
        time.sleep(0.03)
        for _ in range(count):
            self._send(self._mouse(down), self._mouse(up))
            time.sleep(0.06)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        self._send(self._mouse(0x02))
        for i in range(1, 11):
            time.sleep(0.02)
            self.move(x1 + (x2 - x1) * i / 10, y1 + (y2 - y1) * i / 10)
        self._send(self._mouse(0x04))

    def scroll(self, clicks: int) -> None:
        self._send(self._mouse(0x0800, 120 * clicks))  # positive: up

    def type(self, text: str) -> None:
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            if code == 10:  # new line: press Enter
                self._send(self._key(0x0D), self._key(0x0D, flags=0x0002))
            elif code != 13:
                self._send(self._key(0, code, 0x0004), self._key(0, code, 0x0004 | 0x0002))  # UNICODE: any language
            time.sleep(0.004)

    def keys(self, codes: list[int]) -> None:
        ext = lambda c: 0x0001 if c in EXTENDED else 0  # noqa: E731
        presses = [self._key(c, flags=ext(c)) for c in codes]
        releases = [self._key(c, flags=ext(c) | 0x0002) for c in reversed(codes)]
        self._send(*presses, *releases)

    def open(self, target: str) -> None:
        try:
            os.startfile(target)  # type: ignore[attr-defined]  # files, folders, web addresses, most app names
        except OSError:  # e.g. Store apps: search for it in the Start menu instead
            self.keys([0x5B])
            time.sleep(0.8)
            self.type(target)
            time.sleep(1.0)
            self.keys([0x0D])


class FakeDesktop:
    """A pretend screen for tests (CREW_FAKE_DESKTOP=1): records what it is asked to do."""

    def __init__(self):
        self.log: list[str] = []
        self.pointer = (960, 540)

    def size(self):
        return 1920, 1080

    def grab(self, width, height):
        row = bytearray()
        for x in range(width):
            row += bytes((150, 90 + (x * 60 // width), 20, 255)) if x > width * 0.1 else bytes((60, 40, 30, 255))
        return bytes(row) * height

    def cursor(self):
        return self.pointer

    def move(self, x, y):
        self.pointer = (int(x), int(y))

    def click(self, x, y, button="left", count=1):
        self.pointer = (int(x), int(y))
        self.log.append(f"click {button} {int(x)},{int(y)} x{count}")

    def drag(self, x1, y1, x2, y2):
        self.log.append(f"drag {int(x1)},{int(y1)} -> {int(x2)},{int(y2)}")

    def scroll(self, clicks):
        self.log.append(f"scroll {clicks}")

    def type(self, text):
        self.log.append(f"type {text}")

    def keys(self, codes):
        self.log.append("keys " + "+".join(hex(c) for c in codes))

    def open(self, target):
        self.log.append(f"open {target}")


# ------------------------------------------------------------------ the service


class ComputerService:
    def __init__(self):
        self._desk = None
        self._error = ""
        self.driver = "you"
        self.driver_at = 0.0
        self.paused_until = 0.0
        self.last_shot = (MAX_WIDTH, 720)  # size of the last picture the assistant saw
        self.ai_pointer = (-1, -1)  # where the assistant last put the pointer
        self._stream: threading.Thread | None = None
        self._lock = threading.Lock()

    def desk(self):
        if self._desk is None:
            if os.environ.get("CREW_FAKE_DESKTOP") == "1":
                self._desk = FakeDesktop()
            elif os.name == "nt":
                try:
                    self._desk = WindowsDesktop()
                except Exception as exc:  # pragma: no cover - depends on the machine
                    self._error = f"Computer control could not start: {exc}"
            else:
                self._error = "Computer control works when Crew runs on Windows."
        if self._desk is None:
            raise ComputerError(self._error)
        return self._desk

    def status(self) -> dict:
        try:
            d = self.desk()
        except ComputerError as exc:
            return {"available": False, "reason": str(exc)}
        w, h = d.size()
        ai = self.driver != "you" and time.time() - self.driver_at < 5
        return {"available": True, "width": w, "height": h, "driver": self.driver if ai else "you",
                "paused": time.time() < self.paused_until}

    # ---------------------------------------------------------- pictures

    def screenshot(self, max_width: int = MAX_WIDTH) -> tuple[bytes, str, int, int]:
        d = self.desk()
        sw, sh = d.size()
        w, h = fit(sw, sh, max_width)
        data, mime = encode(bgra_to_rgb(d.grab(w, h), w, h), w, h)
        return data, mime, w, h

    def shot_for_assistant(self) -> dict:
        data, mime, w, h = self.screenshot()
        self.last_shot = (w, h)
        sw, sh = self.desk().size()
        return {"image": base64.b64encode(data).decode(), "mime": mime,
                "text": f"The screen ({sw}×{sh}) shown at {w}×{h}. Give positions as pixels of this picture."}

    # ---------------------------------------------------------- actions

    def _guard(self, driver: str) -> None:
        if driver == "you":
            return
        d = self.desk()
        x, y = d.cursor()
        # The main screen's top-left corner: not "nowhere" (-1, -1, before the assistant has used the mouse), nor a
        # second screen to the left of or above the main one (its positions are below zero)
        corner = lambda p: 0 <= p[0] <= 2 and 0 <= p[1] <= 2  # noqa: E731
        if corner((x, y)) and not corner(self.ai_pointer):  # the owner pushed the pointer into the corner
            self.paused_until = time.time() + 30
        if time.time() < self.paused_until:
            raise ComputerError("The owner paused computer control (pointer in the top-left corner). "
                                "Wait, then ask the owner before trying again.")
        self.driver, self.driver_at = driver, time.time()

    def _point(self, body: dict, driver: str) -> tuple[int, int]:
        """Positions: fractions (0–1) from the owner's live view, or picture pixels from the assistant."""
        sw, sh = self.desk().size()
        try:
            if "xr" in body or "yr" in body:
                return (round(max(0.0, min(1.0, float(body.get("xr", 0.5)))) * (sw - 1)),
                        round(max(0.0, min(1.0, float(body.get("yr", 0.5)))) * (sh - 1)))
            if "x" not in body or "y" not in body:
                raise ComputerError("Give x and y: pixel positions in the latest screenshot.")
            pw, ph = self.last_shot
            x = max(0, min(sw - 1, round(float(body["x"]) * sw / pw)))
            y = max(0, min(sh - 1, round(float(body["y"]) * sh / ph)))
        except (TypeError, ValueError, OverflowError):
            raise ComputerError("Give the position as numbers.") from None
        return x, y

    def _moved(self, driver: str, x: int, y: int) -> None:
        if driver != "you":
            self.ai_pointer = (x, y)

    def act(self, action: str, body: dict, driver: str = "you") -> dict:
        d = self.desk()
        self._guard(driver)
        if action == "click":
            x, y = self._point(body, driver)
            button = body.get("button") if body.get("button") in ("left", "right", "middle") else "left"
            self._moved(driver, x, y)
            d.click(x, y, button, 2 if body.get("double") else 1)
            return {"ok": True}
        if action == "move":
            x, y = self._point(body, driver)
            self._moved(driver, x, y)
            d.move(x, y)
            return {"ok": True}
        if action == "drag":
            a = self._point({k[:-1]: v for k, v in body.items() if k.endswith("1")}, driver)
            b = self._point({k[:-1]: v for k, v in body.items() if k.endswith("2")}, driver)
            self._moved(driver, *b)
            d.drag(*a, *b)
            return {"ok": True}
        if action == "scroll":
            try:
                amount = max(1, min(30, int(body.get("amount") or 3)))
            except (TypeError, ValueError, OverflowError):
                raise ComputerError("Say how far to scroll as a number of steps.") from None
            d.scroll(amount if body.get("direction") == "up" else -amount)
            return {"ok": True}
        if action == "type":
            text = str(body.get("text", ""))
            if not text:
                raise ComputerError("Nothing to type.")
            # A few words at a time, the corner looked at in between: a long text stops when the owner says so
            for start in range(0, len(text), TYPE_CHUNK):
                if start:
                    self._guard(driver)
                d.type(text[start:start + TYPE_CHUNK])
            return {"ok": True}
        if action == "key":
            d.keys(parse_keys(str(body.get("keys") or body.get("key") or "")))
            return {"ok": True}
        if action == "open":
            target = str(body.get("target", "")).strip()
            if not target:
                raise ComputerError("Say what to open: an app name, a file, a folder or a web address.")
            d.open(target)
            return {"ok": True}
        raise ComputerError(f"Unknown computer action '{action}'.")

    # ---------------------------------------------------------- live view

    def ensure_stream(self) -> None:
        with self._lock:
            if self._stream is None or not self._stream.is_alive():
                self._stream = threading.Thread(target=self._loop, daemon=True, name="crew-computer")
                self._stream.start()

    def _loop(self) -> None:
        idle = 0
        while idle < 20:
            if hub.count("computer") == 0:
                idle += 1
                time.sleep(0.5)
                continue
            idle = 0
            try:
                data, mime, w, h = self.screenshot(1280)
                hub.publish("computer", "frame", {"data": base64.b64encode(data).decode(), "mime": mime})
                time.sleep(0.6)
            except Exception as exc:
                hub.publish("computer", "status", {"available": False, "reason": str(exc)})
                time.sleep(3)


service = ComputerService()
