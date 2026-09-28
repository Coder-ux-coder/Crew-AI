"""The Crew app server: the local web app (desktop + phone), its API and live streams.

Security model (it can run agents that act on your computer, so this matters):
  * listens on 127.0.0.1 only, unless phone access is switched on;
  * other devices must pair once (a secret link shown as a QR code on the desktop);
  * every state-changing request needs the X-Crew header (web pages elsewhere cannot send it: no CSRF);
  * agent tools use a per-start secret token on internal endpoints, loopback only;
  * generated pages are served sandboxed (they cannot reach the app's API or cookies).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from crewlib import claude_cli, connections as conn_mod, lessons, usage as usage_mod
from crewlib.util import atomic_write, crew_home

from . import browser as browser_mod
from . import captures, chat, computer as computer_mod, launcher, phone as phone_mod, settings, skills, updater, writer
from .workflows import TEMPLATES as WORKFLOW_TEMPLATES, Workflows
from .runs import RunManager
from .sse import hub

STATIC = Path(__file__).resolve().parent / "static"
# Windows can map .js to text/plain in its registry, which breaks the app's modules: set the types explicitly.
for _ext, _type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                    (".svg", "image/svg+xml"), (".webmanifest", "application/manifest+json"), (".json", "application/json"),
                    (".webm", "video/webm"), (".mp4", "video/mp4"), (".png", "image/png"), (".md", "text/markdown"),
                    (".csv", "text/csv"), (".pdf", "application/pdf"), (".html", "text/html")):
    mimetypes.add_type(_type, _ext)
VERSION = updater.current().get("version", "2.0.0")
MAX_UPLOAD = 300 * 1024 * 1024


class App:
    def __init__(self, port: int, phone: bool):
        self.port = port
        self.phone_access = phone
        self.internal_token = secrets.token_urlsafe(24)
        conf = self._conf()
        self.pair_token = conf.setdefault("pair_token", secrets.token_urlsafe(24))
        atomic_write(crew_home() / "app.json", json.dumps(conf))
        url = f"http://127.0.0.1:{port}"
        extra = {"CREW_APP_URL": url, "CREW_APP_TOKEN": self.internal_token}
        self.runs = RunManager(extra_env=extra)
        self.chats = chat.ChatManager(app_url=url, app_token=self.internal_token)
        self.browser = browser_mod.service
        self.phone = phone_mod.service
        self.computer = computer_mod.service
        self._auth_cache: dict[str, tuple[float, dict]] = {}
        self.lan_servers: list = []
        skills.build_active_pack()
        self.workflows = Workflows(self)
        self.skill_install = {"state": "idle", "message": ""}
        self.last_action = time.time()  # the owner's last change (sending, starting, saving): updates wait for quiet

    def upkeep(self) -> None:
        """Once a day, in the background: keep Claude Code current and look for a newer Crew."""
        def work():
            try:
                claude_cli.update_if_stale()
            except Exception as exc:
                print(f"claude update: {exc}")
            launcher.heal_shortcuts()
            refresh_windows_icons()
            if not (crew_home() / "anthropic-skills.json").is_file() and settings.load()["accounts"]:
                self.install_anthropic_skills()  # once: Anthropic's official skills for every Claude subscription
            try:
                if time.time() - float(updater.last_check().get("checked") or 0) > 20 * 3600:
                    info = updater.check()
                    if info.get("available"):
                        hub.publish("app", "update", info)
            except Exception as exc:
                print(f"update check: {exc}")
        threading.Thread(target=work, daemon=True, name="crew-upkeep").start()

    def idle(self, quiet_minutes: float = 30) -> bool:
        """A quiet moment for an automatic update: nothing running, and the owner has not used Crew for a while."""
        if time.time() - self.last_action < quiet_minutes * 60:
            return False
        if any(getattr(s, "busy", False) for s in list(self.chats.sessions.values())):
            return False
        if getattr(self.workflows, "_running", None):
            return False
        try:
            return not any(r.get("running") for r in self.runs.list())
        except Exception:  # noqa: BLE001 — when in doubt, do not update
            return False

    def auto_updates(self) -> None:
        """Every half hour: look for a newer Crew (every six hours) and, when the owner wants it, install it at a
        quiet moment. The open window shows "Updating Crew…" and reconnects by itself."""
        def loop():
            time.sleep(300)
            while True:
                try:
                    self.update_if_quiet()
                except Exception as exc:  # noqa: BLE001 — an update problem must never stop Crew
                    print(f"automatic update: {exc}")
                time.sleep(1800)
        threading.Thread(target=loop, daemon=True, name="crew-auto-update").start()

    def update_if_quiet(self) -> bool:
        info = updater.last_check()
        if time.time() - float(info.get("checked") or 0) > 6 * 3600:
            info = updater.check()
            if info.get("available"):
                hub.publish("app", "update", info)
        if not updater.newer(info) or not settings.load()["app"].get("auto_update", True) or not self.idle():
            return False
        old = updater.current().get("version")
        launcher.log(f"updating Crew from {old} to {info.get('latest')} automatically (nothing was running)")
        hub.publish("app", "updating", {**info, "current": old})
        time.sleep(2)
        try:
            result = updater.install()
        except updater.UpdateError as exc:  # nothing was changed: Crew carries on, and the window says why
            launcher.log(f"automatic update failed: {exc}")
            hub.publish("app", "update_failed", {"message": str(exc)})
            return False
        updater.note_updated(old, result.get("version") or info.get("latest"), auto=True)
        self.chats.shutdown()
        self.workflows.stop()
        updater.restart_later(self.port)
        return True

    def install_anthropic_skills(self) -> None:
        """Anthropic's official skills (Word, Excel, PowerPoint, PDF, skill creator …) through Claude Code's
        own plugin marketplace, for every Claude subscription."""
        if self.skill_install["state"] == "running":
            return
        self.skill_install = {"state": "running", "message": "Installing Anthropic's skills…"}

        def work():
            from crewlib.agents import which
            exe = which("claude")
            problems = [] if exe else ["Claude Code is not installed."]
            for acc in [a for a in settings.load()["accounts"] if a["vendor"] == "claude" and exe]:
                env = self._account_env(acc)
                for args in (["plugin", "marketplace", "add", "anthropics/skills"],
                             ["plugin", "install", "document-skills@anthropic-agent-skills"],
                             ["plugin", "install", "example-skills@anthropic-agent-skills"]):
                    try:
                        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=300, env=env,
                                              stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
                        out = (proc.stdout + proc.stderr).lower()
                        if proc.returncode != 0 and "already" not in out:
                            problems.append(f"{acc['name']}: {(proc.stderr or proc.stdout).strip()[-200:]}")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        problems.append(f"{acc['name']}: {exc}")
            self.skill_install = ({"state": "done", "message": "Anthropic's skills are installed."} if not problems else
                                  {"state": "error", "message": "; ".join(problems)[:600]})
            if not problems:
                atomic_write(crew_home() / "anthropic-skills.json", json.dumps({"installed": time.time()}))
            hub.publish("app", "notice", {"kind": "skills", "text": self.skill_install["message"]})
        threading.Thread(target=work, daemon=True).start()

    def library(self, limit: int = 300) -> list[dict]:
        """Files made in chats (newest first), for the Library screen."""
        titles = {c["id"]: c["title"] for c in self.chats.db.q("SELECT id, title FROM chats")}
        items = []
        root = crew_home() / "chats"
        if root.is_dir():
            for chat_dir in root.iterdir():
                if chat_dir.name not in titles:
                    continue
                for p, rel, st in chat.walk_files(chat_dir, (".crew", "attachments")):
                    if p.name.startswith("."):
                        continue
                    items.append({"name": rel, "url": f"/files/chat/{chat_dir.name}/{rel}",
                                  "kind": chat.file_kind(p.name), "size": st.st_size, "modified": st.st_mtime,
                                  "where": titles[chat_dir.name], "origin": f"#/chat/{chat_dir.name}"})
        items.sort(key=lambda x: x["modified"], reverse=True)
        return items[:limit]

    @staticmethod
    def _conf() -> dict:
        try:
            return json.loads((crew_home() / "app.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------- accounts

    def account_status(self, refresh: bool = False) -> list[dict]:
        cfg = settings.load()
        out = []
        for acc in cfg["accounts"]:
            key = acc["name"]
            cached = self._auth_cache.get(key)
            if cached and not refresh and time.time() - cached[0] < 120:
                out.append(cached[1])
                continue
            info = {**acc, "signed_in": None, "detail": ""}
            try:
                info.update(self._probe_login(acc))
            except Exception as exc:  # never let one account break the page
                info["detail"] = str(exc)
            self._auth_cache[key] = (time.time(), info)
            out.append(info)
        return out

    @staticmethod
    def _account_env(acc: dict) -> dict:
        from crewlib.agents import child_env
        from crewlib.config import Account

        prof = Account(acc["name"], acc["vendor"], acc.get("profile", "")).profile_dir()
        env = {}
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR" if acc["vendor"] == "claude" else "CODEX_HOME"] = str(prof)
        return child_env(env)

    def _probe_login(self, acc: dict) -> dict:
        from crewlib.agents import which

        tool = "claude" if acc["vendor"] == "claude" else "codex"
        exe = which(tool)
        if not exe:
            return {"signed_in": False, "detail": f"{'Claude Code' if tool == 'claude' else 'Codex'} is not installed"}
        cmd = [exe, "auth", "status", "--json"] if tool == "claude" else [exe, "login", "status"]
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25, env=self._account_env(acc), **kwargs)
        if tool == "claude":
            try:
                data = json.loads(proc.stdout or "{}")
            except ValueError:
                data = {}
            return {"signed_in": bool(data.get("loggedIn")), "detail": data.get("email") or data.get("authMethod", "")}
        return {"signed_in": proc.returncode == 0, "detail": (proc.stdout or "").strip().splitlines()[-1:] and
                (proc.stdout or "").strip().splitlines()[-1] or ""}

    def open_login(self, name: str) -> str:
        acc = next((a for a in settings.load()["accounts"] if a["name"] == name), None)
        if acc is None:
            raise ValueError("No such subscription.")
        from crewlib.agents import which

        tool = "claude" if acc["vendor"] == "claude" else "codex"
        exe = which(tool)
        if not exe:
            raise ValueError(f"Install {'Claude Code' if tool == 'claude' else 'Codex'} first (run the Crew installer).")
        env = self._account_env(acc)
        args = [exe, "auth", "login"] if tool == "claude" else [exe, "login"]
        self._auth_cache.pop(name, None)
        if os.name == "nt":  # a small console window guides the sign-in, the browser opens by itself
            subprocess.Popen(["cmd", "/k", *args], env=env, creationflags=0x00000010)  # a new, visible console
        else:
            subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return "A sign-in window is opening. Finish signing in in your browser, then come back here."

    # ---------------------------------------------------------------- phone

    @staticmethod
    def lan_ips() -> list[str]:
        ips: set[str] = set()
        try:
            ips |= {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            pass
        try:  # the address used to reach the internet (no traffic is sent)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("192.0.2.1", 9))
                ips.add(sock.getsockname()[0])
        except OSError:
            pass
        return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))

    def lan_urls(self) -> list[str]:
        return [f"http://{ip}:{self.port}" for ip in self.lan_ips()]

    def set_phone_access(self, enabled: bool) -> str:
        """Also listen on this computer's home-network addresses, so a paired phone can open Crew."""
        for srv in self.lan_servers:
            srv.shutdown()
            srv.server_close()
        self.lan_servers = []
        self.phone_access = False
        if not enabled:
            return "Phone access is off. Only this computer can open Crew."
        failed = []
        for ip in self.lan_ips():
            try:
                srv = CrewServer((ip, self.port), Handler)
            except OSError:
                failed.append(ip)
                continue
            threading.Thread(target=srv.serve_forever, daemon=True, name=f"crew-lan-{ip}").start()
            self.lan_servers.append(srv)
        self.phone_access = bool(self.lan_servers)
        if not self.lan_servers:
            return "This computer is not on a network right now, so the phone cannot reach it."
        return ("Phone access is on. Scan the code with your phone’s camera." +
                (" (Windows may ask whether Python may use private networks: choose Allow.)" if os.name == "nt" else ""))


# ====================================================================== routing

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "Crew"
    app: App  # set on the class at startup

    def log_message(self, *args):
        return

    # ---------------------------------------------------------------- utils

    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authorized(self) -> bool:
        if self._loopback():
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        tok = cookie.get("crew_token")
        return bool(tok and secrets.compare_digest(tok.value, self.app.pair_token))

    def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5 * 1024 * 1024:
            raise ValueError("Request too large.")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw or b"{}")

    def _file(self, path: Path, sandbox: bool = False, cache: bool = False) -> None:
        if not path.is_file():
            return self._error(404, "Not found.")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml",
                                                  "application/manifest+json"):
            ctype += "; charset=utf-8"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if sandbox:
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups allow-modals")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(data)

    def _sse(self, topic: str, initial: list[tuple[str, dict]] | None = None, on_ready=None) -> None:
        q = hub.subscribe(topic)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for event, data in initial or []:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
            if on_ready:  # runs once this viewer is listening, so nothing it sends is missed
                threading.Thread(target=on_ready, daemon=True).start()
            while True:
                try:
                    payload = q.get(timeout=15)
                except Exception:
                    payload = ": ping\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(topic, q)

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = unquote(url.path)
        self.query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if path.startswith("/internal/"):
                if not self._loopback() or self.headers.get("X-Crew-Token") != self.app.internal_token:
                    return self._error(403, "Forbidden.")
                return self.internal(path)
            if path == "/pair":
                return self.pair_landing()
            if not self._authorized():
                if path.startswith("/api/"):
                    return self._error(401, "This device is not paired. Scan the QR code in Crew's Settings → Phone.")
                return self._file(STATIC / "unpaired.html")
            if method != "GET" and path.startswith("/api/"):
                if self.headers.get("X-Crew") != "1":
                    return self._error(403, "Missing app header.")
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    return self._error(403, "Cross-site request refused.")
                self.app.last_action = time.time()
            for m, rx, name in ROUTES:
                if m == method:
                    match = rx.match(path)
                    if match:
                        return getattr(self, name)(*match.groups())
            if method == "GET":
                return self.static(path)
            return self._error(404, "Not found.")
        except (ValueError, KeyError, phone_mod.PhoneError, browser_mod.BrowserUnavailable,
                computer_mod.ComputerError) as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self._error(500, f"Something went wrong: {exc}")

    def do_GET(self):
        self._head = False
        self._dispatch("GET")

    def do_HEAD(self):
        if self.path.split("?")[0].endswith("/events"):
            self.send_response(405)
            self.end_headers()
            return
        self._head = True
        self._dispatch("GET")

    def do_POST(self):
        self._head = False
        self._dispatch("POST")

    def do_PUT(self):
        self._head = False
        self._dispatch("PUT")

    def do_DELETE(self):
        self._head = False
        self._dispatch("DELETE")

    # --------------------------------------------------------------- static

    def static(self, path: str) -> None:
        if path in ("/", "/index.html") or not re.match(r"^/[\w./-]*$", path):
            return self._file(STATIC / "index.html")
        if path == "/favicon.ico":
            return self._file(STATIC / "icons" / "icon-192.png", cache=True)
        if path.startswith("/captures/"):
            p = captures.resolve(path.split("/", 2)[2])
            return self._file(p) if p else self._error(404, "Not found.")
        if path.startswith("/files/"):
            return self.project_file(path)
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._file(STATIC / "index.html")
        if target.name == "sw.js":  # the service worker must always be fresh
            return self._file(target)
        return self._file(target, cache=target.suffix in (".png", ".woff2") or "vendor" in target.parts)

    def project_file(self, path: str) -> None:
        parts = path.split("/", 4)  # ['', 'files', kind, id, rel]
        if len(parts) < 5:
            return self._error(404, "Not found.")
        kind, ident, rel = parts[2], parts[3], parts[4]
        root = (self.app.runs.project_dir(ident) if kind == "run" else
                self.app.chats.workspace(ident) if kind == "chat" else None)
        if root is None:
            return self._error(404, "Not found.")
        target = (root / rel).resolve()
        if root.resolve() not in target.parents:
            return self._error(403, "Outside the project.")
        if target.is_dir():
            target = target / "index.html"
        active = target.suffix.lower() in (".html", ".htm", ".xhtml", ".svg", ".xml", ".js", ".mjs")
        return self._file(target, sandbox=active)

    # -------------------------------------------------------------- pairing

    def pair_landing(self) -> None:
        token = self.query.get("t", "")
        if not secrets.compare_digest(token, self.app.pair_token):
            return self._file(STATIC / "unpaired.html")
        self.send_response(302)
        self.send_header("Set-Cookie", f"crew_token={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Location", "/")
        self.end_headers()

    def _pair_info(self, message: str = "") -> dict:
        urls = [f"{u}/pair?t={self.app.pair_token}" for u in self.app.lan_urls()] if self.app.phone_access else []
        return {"enabled": self.app.phone_access, "urls": urls, "message": message,
                "local": f"http://localhost:{self.app.port}/pair?t={self.app.pair_token}", "port": self.app.port}

    @route("GET", "/api/pair")
    def api_pair(self):
        if not self._loopback():
            return self._error(403, "Open this on the computer running Crew.")
        return self._json(self._pair_info())

    @route("POST", "/api/phone-access")
    def api_phone_access(self):
        if not self._loopback():
            return self._error(403, "Change this on the computer running Crew.")
        enabled = bool(self._body().get("enabled"))
        message = self.app.set_phone_access(enabled)
        settings.save({"app": {"phone_enabled": enabled}})
        return self._json(self._pair_info(message))

    @route("POST", "/api/phone-access/new-code")
    def api_phone_new_code(self):
        """Forget every paired phone: a new pairing code replaces the old one."""
        if not self._loopback():
            return self._error(403, "Change this on the computer running Crew.")
        conf = self.app._conf()
        conf["pair_token"] = self.app.pair_token = secrets.token_urlsafe(24)
        atomic_write(crew_home() / "app.json", json.dumps(conf))
        return self._json(self._pair_info("Paired phones were signed out. Scan the new code to pair again."))

    @route("POST", "/api/improve")
    def api_improve(self):
        """The prompt writer: the owner's words as a clear, precise prompt (shown in the message box first)."""
        b = self._body()
        return self._json(writer.improve(self.app.chats, b.get("text", ""), b.get("reader") or "claude",
                                         b.get("recent") or ""))

    @route("GET", "/api/ping")
    def api_ping(self):
        """Instant: is this Crew alive? (The launcher asks before it opens a window or replaces a stuck Crew.)
        A Crew window on another port of this same machine may read the answer: after an update, Crew can come
        back on a neighbouring port, and the window must know it is really Crew before it goes there."""
        origin = self.headers.get("Origin") or ""
        same_machine = origin and urlparse(origin).hostname == urlparse("//" + (self.headers.get("Host") or "")).hostname
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                   "Vary": "Origin"} if same_machine else {}
        return self._json({"crew": True, "version": VERSION, "pid": os.getpid(), "port": self.app.port}, headers=headers)

    @route("GET", "/api/health")
    def api_health(self):
        from shutil import which as sys_which

        from crewlib.agents import which
        try:
            import PIL  # noqa: F401
            pillow = True
        except ImportError:
            pillow = False
        items = [
            {"id": "claude", "label": "Claude Code — runs your Claude subscriptions", "ok": bool(which("claude"))},
            {"id": "codex", "label": "Codex — runs your ChatGPT subscription", "ok": bool(which("codex")),
             "optional": True},
            {"id": "git", "label": "Git — keeps every version of the team’s work", "ok": bool(sys_which("git"))},
            {"id": "browser", "label": "Browser add-on — the side-panel browser", "ok": browser_mod.availability()[0]},
            {"id": "adb", "label": "Phone connector — controls your Android phone", "ok": bool(phone_mod.adb_path()),
             "optional": True},
            {"id": "pillow", "label": "Picture tools — smoother phone streaming", "ok": pillow, "optional": True},
        ]
        return self._json({"items": items, "version": VERSION, "home": str(crew_home()),
                           "python": sys.version.split()[0], "loopback": self._loopback()})

    @route("POST", "/api/open-home")
    def api_open_home(self):
        if not self._loopback():
            return self._error(403, "Open this on the computer running Crew.")
        open_path(crew_home())
        return self._json({"ok": True})

    # ------------------------------------------------------------- overview

    @route("GET", "/api/overview")
    def api_overview(self):
        st = settings.load()
        return self._json({
            "version": VERSION, "models": st["models"], "team": st["team"], "app": st["app"],
            "known_models": st["known_models"], "efforts": st["efforts"], "codex_efforts": st["codex_efforts"],
            "accounts": st["accounts"],
            "runs": self.app.runs.list()[:12], "chats": self.app.chats.list()[:30],
            "browser": self.app.browser.status(), "phone_access": self.app.phone_access,
            "local": self._loopback(),
        })

    # ----------------------------------------------------------------- runs

    @route("GET", "/api/scorecard")
    def api_scorecard(self):
        from crewlib import scorecard
        m = settings.load()["models"]
        return self._json(scorecard.summary(m.get("codex") or "", m.get("work") or ""))

    @route("GET", "/api/runs")
    def api_runs(self):
        return self._json({"runs": self.app.runs.list()})

    @route("POST", "/api/runs")
    def api_run_start(self):
        b = self._body()
        rid = self.app.runs.start(b.get("request", ""), repo=b.get("folder") or None, mode=b.get("mode") or None,
                                  hours=b.get("hours"), head_to_head=b.get("head_to_head") or None,
                                  accounts=b.get("accounts") or None)
        return self._json({"id": rid})

    @route("GET", r"/api/runs/([\w.-]+)")
    def api_run_state(self, rid):
        data = self.app.runs.state(rid, int(self.query.get("after", 0) or 0))
        return self._json(data) if data else self._json({"id": rid, "starting": True, "messages": []})

    @route("POST", r"/api/runs/([\w.-]+)/say")
    def api_run_say(self, rid):
        b = self._body()
        return self._json({"ok": self.app.runs.say(rid, b.get("text", ""), b.get("to") or None)})

    @route("POST", r"/api/runs/([\w.-]+)/interrupt")
    def api_run_interrupt(self, rid):
        return self._json({"ok": self.app.runs.interrupt(rid, self._body().get("seat", ""))})

    @route("POST", r"/api/runs/([\w.-]+)/stop")
    def api_run_stop(self, rid):
        return self._json({"ok": self.app.runs.stop(rid)})

    @route("POST", r"/api/runs/([\w.-]+)/resume")
    def api_run_resume(self, rid):
        self.app.runs.resume(rid)
        return self._json({"ok": True})

    @route("POST", r"/api/runs/([\w.-]+)/open-folder")
    def api_run_folder(self, rid):
        folder = self.app.runs.project_dir(rid)
        if not folder or not folder.is_dir():
            return self._error(404, "The project folder is not there yet.")
        open_path(folder)
        return self._json({"ok": True})

    # ---------------------------------------------------------------- chats

    @route("GET", "/api/chats")
    def api_chats(self):
        return self._json({"chats": self.app.chats.list()})

    @route("POST", "/api/chats")
    def api_chat_new(self):
        b = self._body()
        return self._json(self.app.chats.create(b.get("engine"), b.get("model"), b.get("effort"), b.get("mode"),
                                                b.get("account")))

    @route("PUT", r"/api/chats/([\w-]+)")
    def api_chat_update(self, cid):
        b = self._body()
        if "title" in b:
            self.app.chats.rename(cid, b["title"])
        if "pinned" in b:
            self.app.chats.pin(cid, bool(b["pinned"]))
        return self._json({"ok": True})

    @route("POST", r"/api/chats/([\w-]+)/mode")
    def api_chat_mode(self, cid):
        return self._json(self.app.chats.set_mode(cid, self._body().get("mode", "auto")))

    @route("POST", r"/api/chats/([\w-]+)/approve")
    def api_chat_approve(self, cid):
        return self._json(self.app.chats.approve_plan(cid, self._body().get("note", "")))

    @route("GET", r"/api/chats/([\w-]+)/files")
    def api_chat_files(self, cid):
        return self._json({"files": self.app.chats.files(cid)})

    @route("GET", r"/api/chats/([\w-]+)")
    def api_chat(self, cid):
        chat_ = self.app.chats.get(cid)
        return self._json(chat_) if chat_ else self._error(404, "No such conversation.")

    @route("POST", r"/api/chats/([\w-]+)/send")
    def api_chat_send(self, cid):
        b = self._body()
        return self._json(self.app.chats.send(cid, b.get("text", ""), b.get("model"), b.get("effort"),
                                              b.get("attachments") or [], b.get("mode"), b.get("engine"),
                                              b.get("account")))

    @route("POST", r"/api/chats/([\w-]+)/upload")
    def api_chat_upload(self, cid):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self._error(400, "Nothing to attach, or the file is too large (300 MB at most).")
        return self._json(self.app.chats.save_attachment(cid, self.query.get("name", "file"), self.rfile.read(length)))

    @route("POST", r"/api/chats/([\w-]+)/attach-capture")
    def api_chat_attach_capture(self, cid):
        src = captures.resolve(self._body().get("name", ""))
        if src is None:
            return self._error(404, "That capture is gone.")
        return self._json(self.app.chats.save_attachment(cid, src.name, src.read_bytes()))

    @route("POST", r"/api/chats/([\w-]+)/stop")
    def api_chat_stop(self, cid):
        self.app.chats.stop(cid)
        return self._json({"ok": True})

    @route("DELETE", r"/api/chats/([\w-]+)")
    def api_chat_delete(self, cid):
        self.app.chats.delete(cid)
        return self._json({"ok": True})

    @route("POST", r"/api/chats/([\w-]+)/project")
    def api_chat_project(self, cid):
        rid = self.app.runs.start(self.app.chats.as_project_request(cid), mode=self._body().get("mode") or None)
        return self._json({"id": rid})

    @route("GET", r"/api/chats/([\w-]+)/events")
    def api_chat_events(self, cid):
        return self._sse(f"chat:{cid}")

    # -------------------------------------------------------------- browser

    @route("GET", "/api/browser/status")
    def api_browser_status(self):
        return self._json(self.app.browser.status())

    @route("GET", "/api/browser/events")
    def api_browser_events(self):
        def boot():
            try:
                self.app.browser.start()
                hub.publish("browser", "meta", self.app.browser.status())
                self.app.browser.kick()
            except browser_mod.BrowserUnavailable as exc:
                hub.publish("browser", "error", {"message": str(exc)})
            except Exception:  # a picture that could not be taken is not worth an error message
                pass
        return self._sse("browser", [("meta", self.app.browser.status())], on_ready=boot)

    @route("POST", r"/api/browser/(\w+)")
    def api_browser_action(self, action):
        return self._json(browser_action(self.app.browser, action, self._body(), "you"))

    # ---------------------------------------------------------------- phone

    @route("GET", "/api/phone/status")
    def api_phone_status(self):
        return self._json(self.app.phone.status())

    @route("GET", "/api/phone/events")
    def api_phone_events(self):
        self.app.phone.ensure_stream()
        return self._sse("phone")

    @route("POST", r"/api/phone/(\w+)")
    def api_phone_action(self, action):
        b = self._body()
        if action == "pair":
            return self._json({"message": self.app.phone.pair(b.get("address", ""), b.get("code", ""))})
        if action == "connect":
            msg = self.app.phone.connect(b.get("address", ""))
            try:
                self.app.phone.reverse(self.app.port)
            except phone_mod.PhoneError:
                pass
            return self._json({"message": msg})
        if action == "reverse":
            self.app.phone.reverse(self.app.port)
            return self._json({"url": f"http://localhost:{self.app.port}"})
        return self._json(phone_action(self.app.phone, action, b, "you"))

    # ------------------------------------------------------------- computer

    @route("GET", "/api/computer/status")
    def api_computer_status(self):
        return self._json(self.app.computer.status())

    @route("GET", "/api/computer/events")
    def api_computer_events(self):
        self.app.computer.ensure_stream()
        return self._sse("computer", [("status", self.app.computer.status())])

    @route("POST", r"/api/computer/(\w+)")
    def api_computer_action(self, action):
        if action == "screenshot":
            data, mime, _, _ = self.app.computer.screenshot(max_width=3840)
            return self._json(captures.save(data, ".jpg" if mime == "image/jpeg" else ".png", label="computer"))
        return self._json(self.app.computer.act(action, self._body(), "you"))

    # ------------------------------------------------------------- captures

    @route("GET", "/api/captures")
    def api_captures(self):
        return self._json({"captures": captures.list_all()})

    @route("POST", "/api/captures")
    def api_capture_upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self._error(400, "Nothing to save, or the file is too large.")
        data = self.rfile.read(length)
        return self._json(captures.save(data, self.query.get("ext", "png"), self.query.get("label", "capture")))

    @route("DELETE", r"/api/captures/([\w.-]+)")
    def api_capture_delete(self, name):
        return self._json({"ok": captures.delete(name)})

    # --------------------------------------------------------------- skills

    @route("GET", "/api/skills")
    def api_skills(self):
        return self._json({"skills": skills.list_all()})

    @route("GET", r"/api/skills/([a-z0-9-]+)")
    def api_skill(self, sid):
        s = skills.get(sid)
        return self._json(s) if s else self._error(404, "No such skill.")

    @route("POST", "/api/skills")
    def api_skill_create(self):
        b = self._body()
        return self._json(skills.create(b.get("name", ""), b.get("when", ""), b.get("steps", "")))

    @route("POST", r"/api/skills/([a-z0-9-]+)/toggle")
    def api_skill_toggle(self, sid):
        s = skills.set_enabled(sid, bool(self._body().get("enabled")))
        return self._json(s) if s else self._error(404, "No such skill.")

    @route("DELETE", r"/api/skills/([a-z0-9-]+)")
    def api_skill_delete(self, sid):
        return self._json({"ok": skills.delete(sid)})

    # ------------------------------------------------------------- settings

    @route("GET", "/api/settings")
    def api_settings(self):
        return self._json({**settings.load(), "rules": settings.rules(), "assistant": chat.instructions(),
                           "secrets": settings.secret_names()})

    @route("PUT", "/api/settings")
    def api_settings_save(self):
        return self._json(settings.save(self._body()))

    @route("PUT", "/api/rules")
    def api_rules_save(self):
        settings.save_rules(self._body().get("text", ""))
        return self._json({"ok": True})

    @route("PUT", "/api/assistant-instructions")
    def api_assistant_save(self):
        chat.save_instructions(self._body().get("text", ""))
        return self._json({"ok": True})

    @route("PUT", "/api/secrets")
    def api_secret_save(self):
        b = self._body()
        settings.save_secret(b.get("name", ""), b.get("value") or None)
        return self._json({"secrets": settings.secret_names()})

    @route("GET", "/api/accounts/status")
    def api_accounts(self):
        return self._json({"accounts": self.app.account_status(self.query.get("refresh") == "1")})

    @route("POST", r"/api/accounts/([\w.-]+)/login")
    def api_account_login(self, name):
        return self._json({"message": self.app.open_login(name)})

    # ---------------------------------------------------------- usage & Claude Code

    @route("GET", "/api/usage")
    def api_usage(self):
        snap = usage_mod.snapshot(7)
        accounts = []
        for acc in settings.load()["accounts"]:
            lim = snap["limits"].get(acc["name"]) or {}
            tok = snap["tokens"].get(acc["name"]) or {"today": 0, "week": 0, "days": {}}
            accounts.append({**acc, "limits": lim, "tokens": tok})
        return self._json({"accounts": accounts, "now": snap["now"]})

    @route("GET", "/api/claude")
    def api_claude(self):
        path, version = claude_cli.best()
        return self._json({"path": path, "version": claude_cli.version_text(version), "info": self.app.chats.info(),
                           "update": claude_cli.state()})

    @route("POST", "/api/claude/update")
    def api_claude_update(self):
        ok, message = claude_cli.update()
        return self._json({"ok": ok, "message": message})

    # ---------------------------------------------------------- Anthropic's skills

    @route("POST", "/api/skills/anthropic")
    def api_skills_anthropic(self):
        self.app.install_anthropic_skills()
        return self._json(self.app.skill_install)

    @route("GET", "/api/skills/anthropic")
    def api_skills_anthropic_state(self):
        return self._json(self.app.skill_install)

    # ---------------------------------------------------------- connections

    @route("GET", "/api/connections")
    def api_connections(self):
        keys = [{**k, "label": conn_mod.key_label(k["name"])} for k in settings.secret_names()]
        return self._json({"keys": keys, "presets": conn_mod.KEY_PRESETS, "mcp": conn_mod.listing(),
                           "claude_desktop": bool(conn_mod.claude_desktop_config())})

    @route("POST", "/api/connections/mcp")
    def api_connection_add(self):
        b = self._body()
        headers = b.get("headers") or {}
        if isinstance(headers, str):
            headers = dict(h.split(":", 1) for h in headers.splitlines() if ":" in h)
        args = b.get("args") or []
        if isinstance(args, str):
            args = args.split()
        conn_mod.add_mcp(b.get("name", ""), b.get("type", "http"), url=b.get("url", ""), headers=headers,
                         command=b.get("command", ""), args=args, env=b.get("env") or {})
        return self._json({"mcp": conn_mod.listing()})

    @route("DELETE", r"/api/connections/mcp/([\w-]+)")
    def api_connection_remove(self, name):
        conn_mod.remove_mcp(name)
        return self._json({"mcp": conn_mod.listing()})

    @route("POST", r"/api/connections/mcp/([\w-]+)/toggle")
    def api_connection_toggle(self, name):
        conn_mod.set_enabled(name, bool(self._body().get("enabled")))
        return self._json({"mcp": conn_mod.listing()})

    @route("POST", "/api/connections/import-claude")
    def api_connection_import(self):
        added = conn_mod.import_claude_desktop()
        return self._json({"added": added, "mcp": conn_mod.listing()})

    # ---------------------------------------------------------- workflows

    @route("GET", "/api/library")
    def api_library(self):
        return self._json({"items": self.app.library()})

    @route("GET", "/api/workflows")
    def api_workflows(self):
        return self._json({"workflows": self.app.workflows.list(), "templates": WORKFLOW_TEMPLATES})

    @route("POST", "/api/workflows")
    def api_workflow_new(self):
        return self._json(self.app.workflows.create(self._body()))

    @route("PUT", r"/api/workflows/(\w+)")
    def api_workflow_update(self, wid):
        return self._json(self.app.workflows.update(wid, self._body()))

    @route("DELETE", r"/api/workflows/(\w+)")
    def api_workflow_delete(self, wid):
        return self._json({"ok": self.app.workflows.delete(wid)})

    @route("POST", r"/api/workflows/(\w+)/run")
    def api_workflow_run(self, wid):
        return self._json(self.app.workflows.run(wid, extra=self._body().get("extra", "")))

    @route("GET", r"/api/workflows/(\w+)/runs")
    def api_workflow_runs(self, wid):
        return self._json({"runs": self.app.workflows.runs(wid)})

    # ---------------------------------------------------------- app-wide events & updates

    @route("GET", "/api/events")
    def api_events(self):
        return self._sse("app")

    @route("GET", "/api/update")
    def api_update_state(self):
        info = updater.check() if self.query.get("refresh") == "1" else (updater.last_check() or {})
        info["current"] = updater.current().get("version")
        info["available"] = updater.newer(info)
        info["updated"] = updater.recent_update()
        info["auto"] = settings.load()["app"].get("auto_update", True)
        return self._json(info)

    @route("POST", "/api/update")
    def api_update_install(self):
        if not self._loopback():
            return self._error(403, "Update Crew on the computer it runs on.")
        busy = [r for r in self.app.runs.list() if r.get("running")]
        if busy and not self._body().get("anyway"):
            return self._json({"confirm": True, "running": len(busy), "error": (
                f"{'A team project is' if len(busy) == 1 else f'{len(busy)} team projects are'} working right now. "
                "It keeps working while Crew updates, but the safest moment to update is when nothing is running.")},
                409)
        old = updater.current().get("version")
        try:
            result = updater.install()
        except updater.UpdateError as exc:  # nothing was changed: Crew keeps running as it was
            return self._error(503, str(exc))
        updater.note_updated(old, result.get("version"), auto=False)
        self.app.chats.shutdown()
        self.app.workflows.stop()
        updater.restart_later(self.app.port)
        return self._json({"ok": True, **result})

    @route("POST", "/api/startup")
    def api_startup(self):
        enabled = bool(self._body().get("enabled"))
        message = set_start_with_windows(enabled)
        settings.save({"app": {"start_with_windows": enabled}})
        return self._json({"ok": True, "message": message})

    @route("GET", "/api/lessons")
    def api_lessons(self):
        items = [{"category": x["category"], "text": x["text"], "weight": x["weight"]} for x in lessons.top(80)]
        return self._json({"lessons": [x for x in items if x["category"] != "ceo"],
                           "ceo": [x for x in items if x["category"] == "ceo"],
                           "effort_record": lessons.effort_stats()})

    # ------------------------------------------------------ internal (agents)

    def internal(self, path: str) -> None:
        _, _, group, action = path.split("/", 3)
        b = self._body()
        try:
            if group == "browser":
                if action == "screenshot":
                    data = self.app.browser.screenshot(bool(b.get("full_page")), fmt="jpeg", driver="assistant")
                    return self._json({"image": base64.b64encode(data).decode(), "mime": "image/jpeg",
                                       "text": f"{self.app.browser.title} — {self.app.browser.url}"})
                return self._json({"result": browser_action(self.app.browser, action, b, "assistant")})
            if group == "computer":
                if action == "screenshot":
                    return self._json(self.app.computer.shot_for_assistant())
                return self._json({"result": self.app.computer.act(action, b, "assistant")})
            if group == "phone":
                if action == "screenshot":
                    png = self.app.phone.screenshot()
                    data, mime = phone_mod.shrink(png)
                    return self._json({"image": base64.b64encode(data).decode(), "mime": mime})
                return self._json({"result": phone_action(self.app.phone, action, b, "assistant")})
        except (ValueError, phone_mod.PhoneError, browser_mod.BrowserUnavailable, computer_mod.ComputerError) as exc:
            return self._json({"error": str(exc)})
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"})
        return self._json({"error": "Unknown tool."})


# ====================================================================== actions


def browser_action(b: browser_mod.BrowserService, action: str, body: dict, driver: str):
    if action in ("navigate", "open"):
        return b.navigate(body.get("url", ""), driver=driver)
    if action == "click":
        if body.get("text"):
            return b.click_text(body["text"], driver=driver)
        return b.click(body.get("x", 0.5), body.get("y", 0.5), bool(body.get("double")), driver=driver)
    if action == "type":
        return b.type(body.get("text", ""), bool(body.get("submit")), driver=driver)
    if action == "press":
        return b.press(body.get("key", "enter"), driver=driver)
    if action == "scroll":
        if "direction" in body:
            dy = 700 * float(body.get("screens") or 1) * (-1 if body["direction"] == "up" else 1)
        else:
            dy = float(body.get("dy", 400))
        return b.scroll(dy, driver=driver)
    if action in ("back", "forward", "reload"):
        return b.history(action, driver=driver)
    if action == "device":
        return b.set_device(body.get("device", "desktop"), driver=driver)
    if action == "read":
        return b.read(driver=driver)
    if action == "fill":
        return b.fill(body.get("field", ""), body.get("text", ""), driver=driver)
    if action == "screenshot":
        data = b.screenshot(bool(body.get("full")), driver=driver)
        return captures.save(data, ".png", label=(b.title or "browser")[:40])
    raise ValueError(f"Unknown browser action '{action}'.")


def phone_action(p: phone_mod.PhoneService, action: str, body: dict, driver: str):
    if action == "status":
        return p.status()
    if action == "tap":
        if body.get("text"):
            return p.tap_text(body["text"], driver=driver)
        return p.tap(body.get("x", 0.5), body.get("y", 0.5), driver=driver)
    if action == "swipe":
        if body.get("direction"):
            return p.swipe_dir(body["direction"], driver=driver)
        return p.swipe(body["x1"], body["y1"], body["x2"], body["y2"], int(body.get("ms", 300)), driver=driver)
    if action == "type":
        return p.type(body.get("text", ""), driver=driver)
    if action == "key":
        return p.key(body.get("key", "home"), driver=driver)
    if action in ("open", "open_app"):
        return p.open_app(body.get("name", ""), driver=driver)
    if action == "screen":
        return {"elements": p.elements()}
    if action == "screenshot":
        return captures.save(p.screenshot(), ".png", label="phone")
    raise ValueError(f"Unknown phone action '{action}'.")


def set_start_with_windows(enabled: bool) -> str:
    """Add or remove Crew from Windows' Startup folder (so workflows and the phone can always reach it)."""
    if os.name != "nt":
        return "Starting with the computer is set up by the installer on Windows."
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    link = startup / "Crew.lnk"
    if not enabled:
        link.unlink(missing_ok=True)
        return "Crew will no longer start with Windows."
    root = Path(__file__).resolve().parent.parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = pyw if pyw.is_file() else Path(sys.executable)
    icon = root / "crewapp" / "static" / "icons" / "crew.ico"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
          f"$s.TargetPath='{target}';$s.Arguments='-X utf8 -m crewlib app --no-open';"
          f"$s.WorkingDirectory='{root}';$s.IconLocation='{icon},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                   capture_output=True, timeout=60, creationflags=launcher.NO_WINDOW)
    return "Crew will start quietly when you sign in to Windows."


ICON_VERSION = 2


def refresh_windows_icons() -> None:
    """Once per new app icon: point Crew's shortcuts at it again and refresh Windows' icon cache, so the desktop,
    Start menu and taskbar stop showing the old icon after an update."""
    flag = crew_home() / "icons.json"
    try:
        if os.name != "nt" or json.loads(flag.read_text(encoding="utf-8")).get("version") == ICON_VERSION:
            return
    except (OSError, ValueError):
        pass
    if os.name != "nt":
        return
    icon = Path(__file__).resolve().parent / "static" / "icons" / "crew.ico"
    ps = ("$w=New-Object -ComObject WScript.Shell;"
          "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'),"
          "[Environment]::GetFolderPath('Startup'))){$f=Join-Path $d 'Crew.lnk';"
          f"if(Test-Path $f){{$l=$w.CreateShortcut($f);$l.IconLocation='{icon},0';$l.Save()}}}};"
          "Start-Process -FilePath ie4uinit.exe -ArgumentList '-show' -WindowStyle Hidden")
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=60, creationflags=launcher.NO_WINDOW)
        atomic_write(flag, json.dumps({"version": ICON_VERSION, "at": time.time()}))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"icon refresh: {exc}")


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_app_window(url: str) -> None:
    """Open Crew in its own app window (Edge/Chrome app mode) when possible, else a normal browser tab."""
    launcher.open_window(url)


class CrewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows that option would let two programs share one port

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _log_when_windowless() -> None:
    """Started from the desktop icon there is no console: keep messages in ~/.crew/app.log."""
    if sys.stdout is None or sys.stderr is None:
        log = open(crew_home() / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def _alert(message: str) -> None:
    print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Crew", 0x40)
        except Exception:
            pass


def _bind(port: int, patient: bool):
    """Take Crew's port. Windows keeps a port for a few minutes after a program closes (its last connections
    linger), so a Crew restarting after an update, or started quietly with Windows, waits for its own port — an
    open window is looking at it — instead of moving to another. Started from the icon, it moves on at once (the
    window follows it). Returns the server, the port of a Crew that turned out to be running, or None."""
    deadline = time.time() + (240 if patient else 0)
    candidate = port
    while candidate < port + 10:
        try:
            return CrewServer(("127.0.0.1", candidate), Handler)
        except OSError:
            if launcher.listening(candidate):
                if launcher.wait_for(candidate, 3):
                    return candidate
            elif candidate == port and time.time() < deadline:
                time.sleep(1.0)
                continue
            candidate += 1
    return None


def main(port: int = 8765, phone: bool = False, open_window: bool = True) -> int:
    """Start Crew, or show the Crew that is already running. Opening Crew never fails silently: every step is
    in ~/.crew/app.log, and a problem the owner must know about is shown in a message."""
    _log_when_windowless()
    launcher.log(f"Crew {updater.current().get('version')} asked to {'open' if open_window else 'start'} "
                 f"(process {os.getpid()}, {sys.executable}, {launcher.ROOT})")
    running = launcher.find_running(port)
    if running:  # Crew keeps running after its window is closed: just show it again
        url = f"http://localhost:{running}"
        launcher.log(f"Crew is already running at {url}")
        if open_window:
            launcher.open_window(url)
        return 0
    other = launcher.starting_elsewhere()
    if other:  # the icon clicked twice, or Crew restarting after an update: wait for it rather than start twice
        launcher.log(f"another Crew (process {other.get('pid')}) is starting; waiting for it")
        found = launcher.wait_running(port, 300)
        if open_window:
            launcher.open_window(f"http://localhost:{found or other.get('port') or port}")
        return 0
    stuck = launcher.stuck_server(port)
    if stuck:
        launcher.log(f"the previous Crew (process {stuck}) stopped answering; replacing it")
        launcher.stop_process(stuck, launcher.recorded().get("port"))
    launcher.record(port, "starting")
    server = _bind(port, patient=not open_window)
    if isinstance(server, int):  # a Crew answered on that port after all
        url = f"http://localhost:{server}"
        launcher.log(f"Crew is already running at {url}")
        if open_window:
            launcher.open_window(url)
        return 0
    if server is None:
        _alert(f"Crew could not start: ports {port}-{port + 9} are all taken by other programs.")
        return 1
    port = server.server_address[1]
    launcher.record(port, "starting")
    try:
        app = App(port, False)
    except Exception as exc:
        traceback.print_exc()
        server.server_close()
        _alert(f"Crew could not start: {exc}\n\nDetails are in {crew_home() / 'app.log'}")
        return 1
    Handler.app = app
    launcher.record(port, "running")
    url = f"http://localhost:{port}"
    app.workflows.start_clock()
    app.upkeep()
    app.auto_updates()
    if phone or settings.load()["app"].get("phone_enabled"):
        app.set_phone_access(True)
    launcher.log(f"Crew is running at {url}" +
                 ("  (phone access on — pair from Settings → Phone)" if app.phone_access else ""))
    if open_window:
        threading.Timer(0.6, launcher.open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.chats.shutdown()
        app.browser.stop()
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--phone", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.port, a.phone, not a.no_open))
