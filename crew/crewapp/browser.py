"""The app's own browser: a real Chromium/Edge run by Playwright, streamed live into
the side panel. You and the AI share it: you click and type on the picture, the
assistant uses the browser_* tools, and both see the same page.

All Playwright calls run on one worker thread (Playwright's sync API is not
thread-safe); other threads submit commands and wait for results.
"""

from __future__ import annotations

import base64
import glob
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path

from crewlib.util import crew_home

from .sse import hub

DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 412, "height": 915, "scale": 2.6,
         "ua": ("Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Mobile Safari/537.36")}
KEYS = {"enter": "Enter", "tab": "Tab", "escape": "Escape", "backspace": "Backspace", "delete": "Delete",
        "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight", "pageup": "PageUp",
        "pagedown": "PageDown", "home": "Home", "end": "End", "space": " "}


LOCAL_ADDRESS = re.compile(r"^(localhost|127(\.\d{1,3}){3}|\[::1\])(:\d+)?([/?#]|$)", re.I)


def normalize_address(text: str) -> str:
    """What was typed in the address bar, as an address: a search for words; this computer's own servers (a team
    project's preview, localhost:5173) over plain http, as they are served; everything else over https."""
    local = bool(LOCAL_ADDRESS.match(text))
    if not local and (" " in text or ("." not in text and not text.startswith(("http", "about:", "file:")))):
        from urllib.parse import quote_plus
        return "https://www.google.com/search?q=" + quote_plus(text)
    if not text.startswith(("http://", "https://", "about:", "file:")):
        return ("http://" if local else "https://") + text
    return text


class BrowserUnavailable(RuntimeError):
    pass


# What the browser says when a page cannot be reached, in the owner's words.
NET_ERRORS = (
    ("ERR_NAME_NOT_RESOLVED", "No website was found at that address. Check how it is spelled."),
    ("ERR_CONNECTION_REFUSED", "Nothing answered at that address. (A team project's preview only works while its "
                               "program runs.)"),
    ("ERR_CONNECTION_TIMED_OUT", "The website did not answer in time. Try again in a moment."),
    ("ERR_INTERNET_DISCONNECTED", "This computer is not connected to the internet."),
    ("ERR_TUNNEL_CONNECTION_FAILED", "The page could not be reached: the network (or its proxy) did not let it "
                                     "through."),
    ("ERR_PROXY_CONNECTION_FAILED", "The page could not be reached: the network's proxy did not answer."),
    ("ERR_CERT", "The website's security certificate is not valid, so the browser did not open it."),
    ("ERR_INVALID_URL", "That is not a web address the browser can open."),
    ("ERR_ABORTED", "The page stopped loading (it may have started a download instead)."),
)


def plain(exc: Exception) -> str:
    """A Playwright error as a short sentence: its first line, without Playwright's call log."""
    text = str(exc)
    for code, words in NET_ERRORS:
        if code in text:
            return words
    first = (text.strip().splitlines() or [""])[0]
    first = re.sub(r"^[A-Z]\w*\.\w+: ", "", first)  # "Page.goto: …", "Locator.click: …"
    if first.startswith("Timeout"):
        return "The page took too long to answer. Try again in a moment."
    return "The browser could not do that: " + (first[:200] or type(exc).__name__)


def _position(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        raise ValueError("Give the position on the page as numbers.") from None


def availability() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "The browser add-on is not installed yet. Run the Crew installer (it installs Playwright)."
    return True, ""


def _executable() -> dict:
    """Prefer the owner's Edge on Windows (no extra download), then an explicit path, then Playwright's Chromium."""
    if os.environ.get("CREW_CHROMIUM") and Path(os.environ["CREW_CHROMIUM"]).exists():
        return {"executable_path": os.environ["CREW_CHROMIUM"]}
    if os.name == "nt":
        return {"channel": "msedge"}
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return {"executable_path": found[-1]} if found else {}


class BrowserService:
    def __init__(self):
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error = ""
        self.device = "desktop"
        self.url = ""
        self.title = ""
        self.driver = "you"
        self.driver_at = 0.0
        self.last_used = time.time()
        self.page = None
        self.ctx = None
        self.cdp = None
        self._stop = False
        self._default_ua = ""
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and self.ready.is_set()

    def start(self) -> None:
        ok, why = availability()
        if not ok:
            raise BrowserUnavailable(why)
        with self._start_lock:  # the panel and an agent may both start it at once: only one browser
            if self.running():
                return
            self.ready.clear()
            self.error = ""
            self._stop = False
            self.thread = threading.Thread(target=self._main, daemon=True, name="crew-browser")
            self.thread.start()
            if not self.ready.wait(45) or self.error:
                raise BrowserUnavailable(self.error or "The browser took too long to start.")

    def stop(self) -> None:
        self._stop = True

    def call(self, fn, *args, driver: str = "you", timeout: float = 90):
        if not self.running():
            self.start()
        self.last_used = time.time()
        if driver != "you":
            self.driver, self.driver_at = driver, time.time()
        fut: Future = Future()
        self.q.put((fn, args, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            raise BrowserUnavailable("The browser is still busy with the last step. Try again in a moment.") from None
        except Exception as exc:
            if type(exc).__module__.startswith("playwright"):  # its errors carry a long call log: say it plainly
                raise ValueError(plain(exc)) from None
            raise

    def status(self) -> dict:
        ok, why = availability()
        ai = self.driver != "you" and time.time() - self.driver_at < 5
        return {"available": ok, "reason": why, "running": self.running(), "url": self.url, "title": self.title,
                "device": self.device, "driver": self.driver if ai else "you", "error": self.error}

    # ---------------------------------------------------------- worker side

    def _main(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.error = str(exc)
            self.ready.set()
            return
        profile = crew_home() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                self.ctx = p.chromium.launch_persistent_context(
                    str(profile), headless=True, viewport=dict(DESKTOP), device_scale_factor=1,
                    args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                          "--no-default-browser-check"], **_executable())
                self.ctx.on("page", self._on_new_page)
                page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
                self._attach(page)
                if page.url in ("", "about:blank"):
                    page.set_content(WELCOME)
                self.ready.set()
                idle_since = time.time()
                while not self._stop:
                    try:
                        fn, args, fut = self.q.get_nowait()
                    except queue.Empty:
                        self.page.wait_for_timeout(40)  # lets Playwright deliver screencast frames
                        if hub.count("browser") == 0 and time.time() - self.last_used > 1800:
                            break  # nobody watching or using it for 30 minutes
                        continue
                    idle_since = time.time()
                    try:
                        fut.set_result(fn(*args))
                    except Exception as exc:  # report to the caller, keep the browser alive
                        fut.set_exception(exc)
                    self._publish_meta()
                del idle_since
                self.ctx.close()
        except Exception as exc:
            self.error = f"The browser stopped: {exc}"
            self.ready.set()
        finally:
            self.page = self.ctx = self.cdp = None

    def _on_new_page(self, page) -> None:
        self._attach(page)  # follow new tabs and pop-ups

    def _attach(self, page) -> None:
        if self.cdp is not None:
            try:
                self.cdp.send("Page.stopScreencast")
                self.cdp.detach()
            except Exception:
                pass
        self.page = page
        self.cdp = self.ctx.new_cdp_session(page)
        if not self._default_ua:
            try:
                self._default_ua = self.cdp.send("Browser.getVersion").get("userAgent", "")
            except Exception:
                self._default_ua = ""
        self.cdp.on("Page.screencastFrame", self._on_frame)
        if self.device == "phone":
            self._apply_phone()
        size = PHONE if self.device == "phone" else DESKTOP
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 62, "maxWidth": size["width"],
                                               "maxHeight": size["height"], "everyNthFrame": 1})
        page.on("framenavigated", lambda frame: self._publish_meta() if frame == page.main_frame else None)

    def _on_frame(self, params: dict) -> None:
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        meta = params.get("metadata") or {}
        hub.publish("browser", "frame", {"data": params["data"], "w": meta.get("deviceWidth"),
                                         "h": meta.get("deviceHeight")})

    def _publish_meta(self) -> None:
        try:
            self.url = self.page.url
            self.title = self.page.title()
        except Exception:
            pass
        hub.publish("browser", "meta", self.status())

    def _viewport(self) -> tuple[int, int]:
        size = PHONE if self.device == "phone" else DESKTOP
        return size["width"], size["height"]

    def _apply_phone(self) -> None:
        self.cdp.send("Emulation.setDeviceMetricsOverride", {"width": PHONE["width"], "height": PHONE["height"],
                                                            "deviceScaleFactor": PHONE["scale"], "mobile": True})
        self.cdp.send("Emulation.setUserAgentOverride", {"userAgent": PHONE["ua"]})
        self.cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

    # ------------------------------------------------------------- commands
    # (each runs on the worker thread via call())

    def _navigate(self, url: str) -> dict:
        url = (url or "").strip()
        if not url:
            raise ValueError("Type a web address or a search.")
        url = normalize_address(url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return {"url": self.page.url, "title": self.page.title()}

    def _click(self, xr: float, yr: float, double: bool = False) -> dict:
        w, h = self._viewport()
        x, y = _position(xr) * w, _position(yr) * h
        if self.device == "phone":
            self.page.touchscreen.tap(x, y)
        elif double:
            self.page.mouse.dblclick(x, y)
        else:
            self.page.mouse.click(x, y)
        self.page.wait_for_timeout(250)
        return {"ok": True}

    def _type(self, text: str, submit: bool = False) -> dict:
        if len(text) > 300:  # typed key by key, a long text would keep the browser busy for minutes (or hours)
            self.page.keyboard.insert_text(text)
        else:
            self.page.keyboard.type(text, delay=8)
        if submit:
            self.page.keyboard.press("Enter")
        return {"ok": True}

    def _press(self, key: str) -> dict:
        try:
            self.page.keyboard.press(KEYS.get(key.lower(), key))
        except Exception as exc:
            if "Unknown key" in str(exc):
                raise ValueError(f"The browser has no key called “{key[:40]}”. Use one of: "
                                 + ", ".join(KEYS) + ", or a letter.") from None
            raise
        return {"ok": True}

    def _scroll(self, dy: float) -> dict:
        w, h = self._viewport()
        self.page.mouse.move(w / 2, h / 2)
        self.page.mouse.wheel(0, float(dy))
        self.page.wait_for_timeout(150)
        return {"ok": True}

    def _history(self, which: str) -> dict:
        {"back": self.page.go_back, "forward": self.page.go_forward, "reload": self.page.reload}[which](
            wait_until="domcontentloaded", timeout=30000)
        return {"url": self.page.url}

    def _set_device(self, device: str) -> dict:
        if device not in ("desktop", "phone") or device == self.device:
            return {"device": self.device}
        self.device = device
        if device == "phone":
            self._apply_phone()
        else:
            self.cdp.send("Emulation.clearDeviceMetricsOverride")
            if self._default_ua:
                self.cdp.send("Emulation.setUserAgentOverride", {"userAgent": self._default_ua})
            self.cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
        self.cdp.send("Page.stopScreencast")
        size = PHONE if device == "phone" else DESKTOP
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 62, "maxWidth": size["width"],
                                               "maxHeight": size["height"], "everyNthFrame": 1})
        if self.page.url.startswith("http"):
            self.page.reload(wait_until="domcontentloaded")
        return {"device": device}

    def _screenshot(self, full: bool = False, fmt: str = "png") -> bytes:
        return self.page.screenshot(full_page=bool(full), type="jpeg" if fmt == "jpeg" else "png",
                                    quality=70 if fmt == "jpeg" else None)

    def _read(self) -> dict:
        text = self.page.evaluate("""() => {
            const t = document.body ? document.body.innerText : '';
            const links = [...document.querySelectorAll('a[href]')].filter(a => a.offsetParent !== null)
              .slice(0, 60).map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80),
                                       href: a.href}));
            const fields = [...document.querySelectorAll('input,textarea,select,button')]
              .filter(e => e.offsetParent !== null).slice(0, 60).map(e => ({
                kind: e.tagName.toLowerCase() + (e.type ? ':' + e.type : ''),
                label: (e.getAttribute('aria-label') || e.placeholder || e.name || e.innerText || e.value || '')
                         .trim().slice(0, 60)}));
            return {text: t.slice(0, 12000), links, fields};
        }""")
        return {"url": self.page.url, "title": self.page.title(), **text}

    def _click_text(self, text: str) -> dict:
        target = self.page.get_by_text(text, exact=False).first
        try:
            target.click(timeout=8000)
        except Exception:
            try:
                self.page.get_by_role("button", name=text).first.click(timeout=5000)
            except Exception:
                raise ValueError(f"Nothing on the page says “{text[:80]}”.") from None
        self.page.wait_for_timeout(400)
        return {"ok": True, "url": self.page.url}

    def _fill(self, field: str, text: str) -> dict:
        for locate in (lambda: self.page.get_by_label(field), lambda: self.page.get_by_placeholder(field),
                       lambda: self.page.locator(f"[name='{field}']")):
            loc = locate().first
            try:
                loc.fill(text, timeout=4000)
                return {"ok": True}
            except Exception:
                continue
        raise ValueError(f"No field called '{field}' on this page.")

    # ------------------------------------------------------- public helpers

    def navigate(self, url, driver="you"):
        return self.call(self._navigate, url, driver=driver)

    def click(self, xr, yr, double=False, driver="you"):
        return self.call(self._click, xr, yr, double, driver=driver)

    def type(self, text, submit=False, driver="you"):
        return self.call(self._type, text, submit, driver=driver)

    def press(self, key, driver="you"):
        return self.call(self._press, key, driver=driver)

    def scroll(self, dy, driver="you"):
        return self.call(self._scroll, dy, driver=driver)

    def history(self, which, driver="you"):
        return self.call(self._history, which, driver=driver)

    def set_device(self, device, driver="you"):
        return self.call(self._set_device, device, driver=driver)

    def screenshot(self, full=False, fmt="png", driver="you"):
        return self.call(self._screenshot, full, fmt, driver=driver)

    def kick(self) -> None:
        """Send the current picture now: the live stream only sends pictures when the page changes,
        so someone who just opened the view would otherwise wait for the next change."""
        data = self.call(self._screenshot, False, "jpeg")
        hub.publish("browser", "frame", {"data": base64.b64encode(data).decode(), "w": None, "h": None})

    def read(self, driver="you"):
        return self.call(self._read, driver=driver)

    def click_text(self, text, driver="you"):
        return self.call(self._click_text, text, driver=driver)

    def fill(self, field, text, driver="you"):
        return self.call(self._fill, field, text, driver=driver)


WELCOME = """<!doctype html><html><head><meta charset="utf-8"><title>Crew browser</title>
<style>body{margin:0;height:100vh;display:grid;place-items:center;font:18px/1.5 system-ui,Segoe UI,sans-serif;
background:#f3f5f4;color:#1b2621}main{max-width:520px;padding:24px;text-align:center}
h1{font-size:28px;margin:0 0 8px}p{color:#56615b;margin:0}</style></head>
<body><main><h1>Crew browser</h1><p>Type a web address or a search above. You and the assistant share this
browser: sign in to sites here once and the assistant can use them for you.</p></main></body></html>"""


def frame_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


service = BrowserService()
