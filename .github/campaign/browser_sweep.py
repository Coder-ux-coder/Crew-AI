"""Browser sweep of the Crew app — the dynamic sweep of DEBUG-CAMPAIGN.md (section 3).

Every screen at 1280x650 (the owner's laptop at 150%) and 390x844 (the phone), in light and dark. On the light theme
every visible control of each screen is clicked once, each time on a fresh copy of that screen. It records:
  * console errors, uncaught exceptions and unhandled promise rejections;
  * failed requests (4xx/5xx answers, and requests that never completed);
  * red notices (toasts) the owner would see;
  * horizontal overflow (the page scrolls sideways), elements outside the screen, and clipped text;
and saves a screenshot of every screen for review.

The real server runs in this process (tests/test_app.AppServer) with the scripted fakes, in a Crew folder of its own,
filled like the owner's: four subscriptions, chats (one in Urdu), a finished team project, a workflow, a skill, a
capture, a connection and a key. Nothing reaches the network or this computer (downloads, Claude Code updates and
opening folders are replaced by stand-ins).

    python .github/campaign/browser_sweep.py [--out folder] [--no-click] [--only route-substring]

Exit code 0 when nothing was flagged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CREW = REPO / "crew"
sys.path[:0] = [str(CREW), str(CREW / "tests")]

import test_app  # noqa: E402  (sets CREW_HOME and the fakes before the app is imported)
from test_app import AppServer, until  # noqa: E402

from crewapp import server, updater  # noqa: E402
from crewlib import claude_cli  # noqa: E402

SIZES = {"laptop": {"width": 1280, "height": 650}, "phone": {"width": 390, "height": 844}}
THEMES = ("light", "dark")
OWNER_ACCOUNTS = [{"name": "claude-1", "vendor": "claude", "profile": ""},
                  {"name": "ceo-pbit.gop.pk", "vendor": "claude", "profile": ""},
                  {"name": "zeeshandmg36-gmail.com", "vendor": "claude", "profile": ""},
                  {"name": "mohidzeeshanrana-gmail.com", "vendor": "codex", "profile": ""}]
URDU_TITLE = "پنجاب بورڈ آف انویسٹمنٹ — سرمایہ کاری کانفرنس کی تیاری اور مہمانوں کی فہرست"
CONTROLS = ("button, a[href], [role=button], [role=tab], [role=menuitem], input[type=checkbox], input[type=radio], "
            "select, summary, label.switch, .chip, [data-nav]")

# In-page checks: what the owner would see as broken.
LAYOUT_JS = r"""() => {
  const W = window.innerWidth, H = window.innerHeight, out = {overflow: 0, outside: [], clipped: [], toasts: []};
  out.overflow = Math.max(0, document.documentElement.scrollWidth - W, document.body.scrollWidth - W);
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const scrolls = (el) => { for (let p = el.parentElement; p; p = p.parentElement) { const s = getComputedStyle(p);
    if (/(auto|scroll)/.test(s.overflowX) || /(hidden|clip)/.test(s.overflowX)) return true; } return false; };
  const name = (el) => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '')
    + ' “' + (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 50) + '”');
  for (const el of document.querySelectorAll('#app *')) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if ((r.right > W + 1 || r.left < -1) && !scrolls(el) && !el.closest('.panel:not(.open), .drawer-closed'))
      out.outside.push(name(el) + ` [${Math.round(r.left)}..${Math.round(r.right)} of ${W}]`);
    const s = getComputedStyle(el);
    const text = (el.innerText || '').trim();
    if (!text || el.clientWidth === 0 || el.clientWidth > 500) continue;  // words in a small box: buttons, labels, chips
    const ellipsis = s.textOverflow === 'ellipsis' ||
      [...el.querySelectorAll('*')].some((c) => getComputedStyle(c).textOverflow === 'ellipsis');
    const wide = /(hidden|clip)/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 1 && !ellipsis;
    const tall = /(hidden|clip)/.test(s.overflowY) && el.clientHeight < 120 && el.scrollHeight > el.clientHeight + 2
      && s.webkitLineClamp === 'none' && !ellipsis;
    if (wide || tall) out.clipped.push(name(el) + ` [${el.scrollWidth}x${el.scrollHeight} in ${el.clientWidth}x${el.clientHeight}]`);
  }
  for (const t of document.querySelectorAll('.toast.bad')) out.toasts.push(t.innerText.trim().slice(0, 300));
  out.outside = [...new Set(out.outside)].slice(0, 12);
  out.clipped = [...new Set(out.clipped)].slice(0, 12);
  return out;
}"""
CONTROLS_JS = r"""(sel) => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled
      && r.bottom > 0 && r.top < window.innerHeight + 2000; };
  return [...document.querySelectorAll(sel)].map((el, i) => ({i, ok: visible(el) && !el.closest('#toasts'),
    label: (el.getAttribute('aria-label') || el.title || el.innerText || el.value || el.getAttribute('href') || '')
      .trim().replace(/\s+/g, ' ').slice(0, 60), tag: el.tagName.toLowerCase()})).filter((c) => c.ok);
}"""


class Sweep:
    def __init__(self, out: Path, click: bool, only: str | None):
        self.out, self.click, self.only = out, click, only
        self.findings: list[dict] = []
        self.screens = 0
        self.clicks = 0

    # -------------------------------------------------------------- the app, filled like the owner's

    def start_app(self) -> None:
        updater._get = lambda url, timeout=30: (_ for _ in ()).throw(OSError("offline (the browser sweep)"))
        claude_cli.update = lambda path=None: (True, "Claude Code is up to date (the browser sweep).")
        server.open_path = lambda path: None
        self.s = AppServer()
        self.base = f"http://127.0.0.1:{self.s.port}"

    def fill(self) -> dict:
        s = self.s
        s.api("PUT", "/api/settings", {"accounts": OWNER_ACCOUNTS, "app": {"owner_name": "Zeeshan"}})
        s.api("PUT", "/api/secrets", {"name": "HUNTER_API_KEY", "value": "hk_test_0123456789abcdef"})
        s.api("POST", "/api/connections/mcp", {"name": "notion", "type": "http", "url": "https://mcp.notion.com/mcp"})
        s.api("POST", "/api/skills", {"name": "Board report", "when": "writing a report for the board",
                                      "steps": "Open the template, fill in each section, check the numbers twice."})
        status, _, body = s.request("POST", "/api/captures?ext=png&label=screen", headers={"X-Crew": "1"},
                                    raw=test_app.PNG_1PX)
        ids = {"capture": json.loads(body)["name"]}
        chats = {}
        for key, prompt, engine in (("claude", "hello", "claude"), ("page", "make a page about Lahore", "claude"),
                                    ("steps", "give me the steps as a to-do list", "claude"),
                                    ("gpt", "hello", "codex")):
            cid = s.api("POST", "/api/chats", {"engine": engine})["id"]
            s.api("POST", f"/api/chats/{cid}/send", {"text": prompt})
            until(lambda cid=cid: not s.api("GET", f"/api/chats/{cid}")["busy"], timeout=90)
            chats[key] = cid
        s.api("PUT", f"/api/chats/{chats['steps']}", {"title": URDU_TITLE})
        s.api("PUT", f"/api/chats/{chats['claude']}", {"pinned": True})
        ids["chat"], ids["chat_urdu"], ids["chat_gpt"], ids["chat_page"] = (chats["claude"], chats["steps"],
                                                                             chats["gpt"], chats["page"])
        w = s.api("POST", "/api/workflows", {"name": "Morning briefing", "prompt": "Summarise the news briefly.",
                                             "engine": "claude", "schedule": {"kind": "daily", "time": "08:00",
                                                                              "days": [0, 1, 2, 3, 4]}})
        s.api("POST", f"/api/workflows/{w['id']}/run", {})
        until(lambda: not s.app.workflows._running, timeout=120)
        ids["workflow"] = w["id"]
        rid = s.api("POST", "/api/runs", {"request": "Build a small feature pack with tests", "mode": "team"})["id"]
        until(lambda: s.api("GET", f"/api/runs/{rid}").get("raw_phase") in ("done", "failed", "stopped"),
              timeout=300, step=2)
        ids["run"] = rid
        return ids

    # -------------------------------------------------------------- one screen

    def routes(self, ids: dict) -> list[str]:
        out = ["/", "/new", f"/chat/{ids['chat']}", f"/chat/{ids['chat_urdu']}", f"/chat/{ids['chat_gpt']}",
               f"/chat/{ids['chat_page']}", "/chats", "/projects", f"/projects/{ids['run']}", "/workflows",
               "/library", "/skills", "/connections", "/usage", "/usage/scorecard", "/browser", "/phone", "/computer",
               "/no-such-screen", "/chat/no-such-chat", "/projects/no-such-project"]
        out += [f"/settings/{k}" for k in ("general", "subscriptions", "models", "team", "instructions", "voice",
                                           "phone", "lessons", "updates")]
        return [r for r in out if not self.only or self.only in r]

    def watch(self, page, bag: list) -> None:
        def console(msg):
            if msg.type == "error":
                bag.append(("console error", msg.text))

        def failed(req):
            err = req.failure or ""
            if "ERR_ABORTED" in err and ("/events" in req.url or req.resource_type in ("eventsource", "media")):
                return  # a live stream closed when the screen changed
            bag.append(("request failed", f"{req.method} {req.url} {err}"))

        def response(resp):
            if resp.status >= 400:
                bag.append((f"HTTP {resp.status}", f"{resp.request.method} {resp.url}"))

        page.on("console", console)
        page.on("pageerror", lambda exc: bag.append(("uncaught", str(exc))))
        page.on("requestfailed", failed)
        page.on("response", response)
        page.on("dialog", lambda d: d.dismiss())
        page.on("filechooser", lambda fc: None)
        page.on("popup", lambda p: p.close())

    def open(self, page, route: str) -> None:
        page.goto(f"{self.base}/#{route}", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:  # noqa: BLE001 — live streams keep the network busy
            pass
        page.wait_for_timeout(400)

    def check(self, page, where: str, bag: list, shot: str | None = None) -> None:
        layout = page.evaluate(LAYOUT_JS)
        for kind, detail in bag:
            self.flag(kind, where, detail)
        bag.clear()
        if layout["overflow"] > 1:
            self.flag("page scrolls sideways", where, f"{layout['overflow']}px wider than the screen")
        for x in layout["outside"]:
            self.flag("outside the screen", where, x)
        for x in layout["clipped"]:
            self.flag("clipped text", where, x)
        for x in layout["toasts"]:
            self.flag("red notice", where, x)
        if shot:
            page.screenshot(path=str(self.out / shot))

    def flag(self, kind: str, where: str, detail: str) -> None:
        item = {"kind": kind, "where": where, "detail": detail[:600]}
        if item not in self.findings:
            self.findings.append(item)
            print(f"  !! {kind} @ {where}: {detail[:240]}", flush=True)

    # -------------------------------------------------------------- the sweep

    def run(self) -> int:
        from playwright.sync_api import sync_playwright

        self.out.mkdir(parents=True, exist_ok=True)
        self.start_app()
        try:
            ids = self.fill()
            print(f"filled: {ids}", flush=True)
            with sync_playwright() as pw:
                exe = test_app._chromium()
                fake_mic = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
                browser = pw.chromium.launch(**({"executable_path": exe} if exe else {}), args=fake_mic)
                try:
                    for size, viewport in SIZES.items():
                        for theme in THEMES:
                            ctx = browser.new_context(viewport=viewport, color_scheme=theme,
                                                      permissions=["microphone"], is_mobile=size == "phone",
                                                      has_touch=size == "phone")
                            try:
                                self.screens_of(ctx, ids, size, theme)
                            finally:
                                ctx.close()
                finally:
                    browser.close()
        finally:
            try:
                self.s.stop()
            except Exception:  # noqa: BLE001
                pass
        return 0 if not self.findings else 1

    def screens_of(self, ctx, ids: dict, size: str, theme: str) -> None:
        bag: list = []
        page = ctx.new_page()
        self.watch(page, bag)
        for route in self.routes(ids):
            where = f"{size}/{theme} #{route}"
            try:
                self.open(page, route)
                shot = f"{size}-{theme}-{re.sub(r'[^A-Za-z0-9]+', '_', route).strip('_') or 'home'}.png"
                self.check(page, where, bag, shot)
                self.screens += 1
            except Exception:  # noqa: BLE001
                self.flag("sweep error", where, traceback.format_exc()[-800:])
            if self.click and theme == "light":
                self.click_all(ctx, route, size)
        page.close()

    def click_all(self, ctx, route: str, size: str) -> None:
        """Every visible control of the screen, one at a time, each on a fresh copy of the screen."""
        bag: list = []
        page = ctx.new_page()
        self.watch(page, bag)
        try:
            self.open(page, route)
            bag.clear()
            controls = page.evaluate(CONTROLS_JS, CONTROLS)
            for c in controls:
                where = f"{size} #{route} → click {c['tag']} “{c['label']}”"
                try:
                    if page.url != f"{self.base}/#{route}":
                        self.open(page, route)
                    else:
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_timeout(350)
                    bag.clear()
                    el = page.locator(CONTROLS).nth(c["i"])
                    el.click(timeout=3000)
                    page.wait_for_timeout(700)
                    self.check(page, where, bag)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                    self.check(page, where + " → Escape", bag)
                    self.clicks += 1
                except Exception as exc:  # noqa: BLE001 — a control that cannot be clicked is worth a look
                    text = str(exc).splitlines()[0][:300]
                    if "Timeout" not in text:
                        self.flag("click failed", where, text)
        finally:
            page.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / ".github" / "campaign" / "sweep-out"))
    ap.add_argument("--no-click", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()
    sweep = Sweep(Path(a.out), not a.no_click, a.only)
    started = time.time()
    code = sweep.run()
    kinds: dict[str, int] = {}
    for f in sweep.findings:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    (Path(a.out) / "findings.json").write_text(json.dumps(sweep.findings, indent=1, ensure_ascii=False),
                                               encoding="utf-8")
    print(f"\n{sweep.screens} screens, {sweep.clicks} clicks in {time.time() - started:.0f}s; "
          f"{len(sweep.findings)} findings {kinds}", flush=True)
    from crewapp import browser  # the app's own browser (the Browser screen started it): stopped before leaving
    browser.service.stop()
    if browser.service.thread is not None:
        browser.service.thread.join(20)
    sys.stdout.flush()
    os._exit(code)  # two Playwright instances in one process can crash while Python shuts down


if __name__ == "__main__":
    sys.exit(main())
