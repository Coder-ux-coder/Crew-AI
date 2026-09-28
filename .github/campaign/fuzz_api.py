"""API fuzzer for the Crew app — the dynamic sweep of DEBUG-CAMPAIGN.md (section 3).

Generated from crewapp.server.ROUTES: every route is called with its path ids and body fields missing, empty, of the
wrong type, huge, in Urdu, path-like and hostile. It flags:
  * any 500, and any request the server did not answer (a dropped connection);
  * any traceback written to stderr, and any exception that ended a thread;
  * any request slower than 30 seconds;
  * any change to the owner's data made by a request that was refused (4xx), or by a GET.

The real server runs in this process (tests/test_app.AppServer) with the scripted fakes in tests/fakes, in a Crew
folder of its own. Nothing reaches the network or this computer: downloads, Claude Code updates, opening folders and
starting team projects are replaced by stand-ins.

    python .github/campaign/fuzz_api.py [--quick] [--only substring] [--report path.json]

Exit code 0 when nothing was flagged.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import inspect
import io
import json
import os
import re
import sqlite3
import sys
import textwrap
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[2]
CREW = REPO / "crew"
sys.path[:0] = [str(CREW), str(CREW / "tests")]

import test_app  # noqa: E402  (sets CREW_HOME and the fakes before the app is imported)
from test_app import HOME, AppServer  # noqa: E402

from crewapp import runs as runs_mod  # noqa: E402
from crewapp import skills as skills_mod  # noqa: E402
from crewapp import server, updater  # noqa: E402
from crewlib import claude_cli, connections as conn_mod  # noqa: E402
from crewlib.store import Store  # noqa: E402

SLOW = 30.0

# ------------------------------------------------------------------ values

HUGE = "A" * 1_000_000
URDU = "پنجاب بورڈ آف انویسٹمنٹ کی رپورٹ"
TEXTS = [
    "", " ", "\t\n", "0", "-1", "null", "true", URDU, "🙂" * 50, HUGE,
    "../../../../etc/passwd", "..\\..\\..\\Windows\\win.ini", "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "\\\\server\\share\\file", "/dev/zero", "CON", "nul.txt", "--help", "-rf /",
    "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "'; DROP TABLE chats; --",
    "$(touch /tmp/crew-fuzz-pwned)", "`id`", "%s%s%s%n", "{{7*7}}", "${7*7}", "\r\nX-Injected: 1",
    "a\x00b", "\ud800", "http://169.254.169.254/latest/meta-data/", "javascript:alert(1)", "file:///etc/passwd",
]
WRONG = [None, 0, -1, 1.5, 10 ** 30, 1e308, float("nan"), float("inf"), True, False, [], [1, "a"], {}, {"a": 1}]
VALUES = WRONG + TEXTS
QUICK_VALUES = [None, 0, True, [], {}, "", URDU, "../../x", "\ud800", "<b>x</b>", HUGE, float("nan")]

# A valid body for each route (so a request reaches the code behind the first check), and the fields it reads that
# the handler hands on as a whole (the rest are found in the handler's source).
SETTINGS_FIELDS = {"team": ["mode", "max_hours", "stall_minutes", "chat_budget", "review", "ceo_reviews", "deliver",
                            "workhorse_seats", "head_to_head", "web_port", "prompt_writer"],
                   "models": ["work", "ceo", "ceo_backup", "codex", "allowed", "banned", "effort_work",
                              "effort_light", "effort_ceo"],
                   "app": ["theme", "voice_rate", "auto_update", "phone_enabled", "chat_model", "chat_effort",
                           "owner_name", "dictation_lang", "improve_prompts", "settings_version"]}
EXTRA_FIELDS = {
    "api_workflow_new": ["name", "prompt", "engine", "model", "effort", "schedule", "enabled", "account"],
    "api_workflow_update": ["name", "prompt", "engine", "model", "effort", "schedule", "enabled", "account"],
}
# The device routes: each action, a body that works, and the fields that action reads.
ACTIONS = {
    "browser": {"navigate": ({"url": "http://127.0.0.1:{port}/api/ping"}, ["url"]),
                "click": ({"x": 0.5, "y": 0.5}, ["x", "y", "double", "text"]),
                "type": ({"text": "hello"}, ["text", "submit"]), "press": ({"key": "enter"}, ["key"]),
                "scroll": ({"dy": 200}, ["dy", "direction", "screens"]), "back": ({}, []), "reload": ({}, []),
                "device": ({"device": "phone"}, ["device"]), "read": ({}, []),
                "fill": ({"field": "q", "text": "x"}, ["field", "text"]), "screenshot": ({}, ["full"]),
                "bogus": ({}, [])},
    "phone": {"pair": ({"address": "192.168.1.20:37123", "code": "123456"}, ["address", "code"]),
              "connect": ({"address": "192.168.1.20:41555"}, ["address"]), "reverse": ({}, []), "status": ({}, []),
              "tap": ({"x": 0.5, "y": 0.5}, ["x", "y", "text"]),
              "swipe": ({"x1": 0.5, "y1": 0.8, "x2": 0.5, "y2": 0.2}, ["x1", "y1", "x2", "y2", "ms", "direction"]),
              "type": ({"text": "hello"}, ["text"]), "key": ({"key": "home"}, ["key"]),
              "open": ({"name": "chrome"}, ["name"]), "screen": ({}, []), "screenshot": ({}, []), "bogus": ({}, [])},
    "computer": {"click": ({"xr": 0.5, "yr": 0.5}, ["x", "y", "xr", "yr", "button", "double"]),
                 "move": ({"x": 10, "y": 10}, ["x", "y"]),
                 "drag": ({"x1": 1, "y1": 1, "x2": 20, "y2": 20}, ["x1", "y1", "x2", "y2"]),
                 "scroll": ({"amount": 3}, ["amount", "direction"]), "type": ({"text": "hello"}, ["text"]),
                 "key": ({"keys": "ctrl+c"}, ["keys", "key"]), "open": ({"target": "notepad"}, ["target"]),
                 "screenshot": ({}, []), "bogus": ({}, [])},
}
DEVICE_ROUTES = {"api_browser_action": "browser", "api_phone_action": "phone", "api_computer_action": "computer"}
# Hostile copies of the Claude desktop app's connectors file (Connections → Import).
DESKTOP_FILES = ['{"mcpServers": {"a": {"command": "npx", "args": 5}}}', '{"mcpServers": {"a": {"command": 7}}}',
                 '{"mcpServers": {"a": {"url": "https://x", "headers": [1]}}}', '{"mcpServers": [1]}', "[1]", "{",
                 '{"mcpServers": {"a": {"command": "npx", "env": "X=1"}}}', '{"mcpServers": {"../../x": {"url": 5}}}']
SCHEDULES = [{"kind": "daily", "time": "25:99"}, {"kind": "hourly", "every": 0}, {"kind": "hourly", "every": -5},
             {"kind": "hourly", "every": 10 ** 9}, {"kind": "weekly", "days": [7, -1, "x"]}, {"kind": "once", "at": 5},
             {"kind": "once", "at": "yesterday"}, {"kind": "once", "at": "9999-12-31T23:59"},
             {"kind": "daily", "time": None}, {"kind": "daily", "days": "0,1"}, {"kind": ["daily"]}]


def baseline(name: str, ids: dict) -> dict:
    return {
        "api_phone_access": {"enabled": False}, "api_improve": {"text": "Make me a short plan.", "reader": "claude"},
        "api_run_start": {"request": "Build a small page about Lahore."},
        "api_run_say": {"text": "Please keep it short."}, "api_run_interrupt": {"seat": "ada"},
        "api_chat_new": {"engine": "claude"}, "api_chat_update": {"title": "Renamed"},
        "api_chat_mode": {"mode": "plan"}, "api_chat_approve": {"note": "Go ahead."},
        "api_chat_send": {"text": "hello"}, "api_chat_attach_capture": {"name": ids.get("capture", "x.png")},
        "api_chat_project": {"mode": "solo"},
        "api_skill_create": {"name": "Fuzz skill", "when": "when the fuzzer runs a check",
                             "steps": "Do the first step, then the second step, then stop."},
        "api_skill_toggle": {"enabled": True}, "api_settings_save": {"app": {"theme": "dark"}},
        "api_rules_save": {"text": "# Rules\n\n1. Be brief."}, "api_assistant_save": {"text": "Be kind."},
        "api_secret_save": {"name": "FUZZ_API_KEY", "value": "abcd1234efgh5678"},
        "api_connection_add": {"name": "fuzz", "type": "http", "url": "https://example.com/mcp"},
        "api_connection_toggle": {"enabled": False},
        "api_workflow_new": {"name": "Fuzz", "prompt": "Say hello briefly.", "engine": "claude"},
        "api_workflow_update": {"name": "Fuzz 2"}, "api_workflow_run": {"extra": "Also mention Lahore."},
        "api_startup": {"enabled": False},
    }.get(name, {})


# ------------------------------------------------------------------ the owner's data, before and after

VOLATILE = re.compile(r"(\.log$|^runs/.*/(logs|worktrees)/|^chats/|^captures/|^skills-active/|^\.skills-active-new|"
                      r"^writer/|^accounts/|claude-cli\.json$|update\.json$|^app\.json$|-journal$|-wal$|-shm$|"
                      r"^anthropic-skills\.json$|^team_rules\.md$|"  # written by background work / on first read
                      r"^memory\.db|^usage\.db|^browser-profile/|^\.crew\.toml\.check)")


def snapshot() -> dict[str, str]:
    """A fingerprint of every file in the Crew folder (databases by their contents)."""
    out = {}
    for p in sorted(HOME.rglob("*")):
        rel = p.relative_to(HOME).as_posix()
        if not p.is_file() or VOLATILE.search(rel):
            continue
        try:
            if p.suffix == ".db":
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
                try:
                    data = "\n".join(con.iterdump()).encode("utf-8", "surrogatepass")
                finally:
                    con.close()
            else:
                data = p.read_bytes()
        except (OSError, sqlite3.Error) as exc:
            data = f"unreadable: {exc}".encode()
        out[rel] = hashlib.sha256(data).hexdigest()[:16]
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


# ------------------------------------------------------------------ what went wrong, as it happens

class Stderr(io.TextIOBase):
    def __init__(self, real):
        self.real, self.buf, self.lock = real, [], threading.Lock()

    def write(self, s):
        with self.lock:
            self.buf.append(s)
        return len(s)

    def flush(self):
        pass

    def take(self) -> str:
        with self.lock:
            text, self.buf = "".join(self.buf), []
        return text


THREAD_ERRORS: list[str] = []


def thread_hook(args):
    THREAD_ERRORS.append(f"{args.thread.name if args.thread else '?'}: "
                         + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


# ------------------------------------------------------------------ the routes

def handler_fields(name: str) -> tuple[list[str], list[str]]:
    """Body fields and query names a handler reads, found in its source."""
    src = textwrap.dedent(inspect.getsource(getattr(server.Handler, name)))
    body, query = set(EXTRA_FIELDS.get(name, [])), set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            owner = ast.unparse(node.func.value)
            if owner == "self.query":
                query.add(node.args[0].value)
            elif owner != "self.headers":
                body.add(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str) and ast.unparse(node.value) == "b":
            body.add(node.slice.value)
    return sorted(body), sorted(query)


def path_ids(rx: re.Pattern, ids: dict, name: str) -> list[tuple[str, str]]:
    """(label, path) for a route: its real object, one that does not exist, and odd ids its pattern lets through."""
    pattern = rx.pattern[1:-1]
    groups = re.findall(r"\([^)]*\)", pattern)
    if not groups:
        return [("", pattern)]
    kind = next((k for k in ("runs", "chats", "skills", "captures", "workflows", "connections/mcp", "accounts",
                             "browser", "phone", "computer") if f"/api/{k}/" in pattern), "")
    real = {"runs": ids["run"], "chats": ids["chat"], "skills": ids["skill"], "captures": ids["capture"],
            "workflows": ids["workflow"], "connections/mcp": ids["connection"], "accounts": "claude-1",
            "browser": "navigate", "phone": "tap", "computer": "click"}.get(kind, "x")
    actions = {"browser": ["navigate", "click", "type", "press", "scroll", "back", "device", "read", "fill",
                           "screenshot", "bogus"],
               "phone": ["pair", "connect", "reverse", "status", "tap", "swipe", "type", "key", "open", "screen",
                         "screenshot", "bogus"],
               "computer": ["click", "move", "drag", "scroll", "type", "key", "open", "screenshot", "bogus"]}
    firsts = [real] + actions.get(kind, []) + ["no-such-thing", "..", ".", "a" * 300, "CON", "رپورٹ", "x.y", "_"]
    out = []
    for value in dict.fromkeys(firsts):
        concrete = pattern
        for g in groups:
            concrete = concrete.replace(g, value, 1)
        if rx.match(concrete):
            quoted = "/".join(quote(part, safe="") if i >= 3 else part for i, part in enumerate(concrete.split("/")))
            out.append((value if len(value) < 40 else value[:20] + "…", quoted))
    return out


# ------------------------------------------------------------------ the run

class Fuzzer:
    def __init__(self, quick: bool, only: str | None):
        self.quick, self.only = quick, only
        self.findings: list[dict] = []
        self.count = 0
        self.stderr = Stderr(sys.stderr)

    def flag(self, kind: str, method: str, path: str, detail: str, body=None) -> None:
        shown = json.dumps(body, default=str)[:300] if body is not None else ""  # escaped: a lone surrogate prints
        detail = detail.encode("utf-8", "backslashreplace").decode("utf-8")
        self.findings.append({"kind": kind, "method": method, "path": path[:200], "body": shown,
                              "detail": detail[:3000]})
        print(f"  !! {kind}: {method} {path[:120]} {shown[:160]}\n     {detail[:400]}", file=sys.__stdout__)

    def send(self, method: str, path: str, body=None, raw: bytes | None = None, headers: dict | None = None,
             expect_change: bool = True) -> tuple[int, str]:
        self.count += 1
        hdrs = {"X-Crew": "1"} if method != "GET" else {}
        hdrs.update(headers or {})
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode()  # \\uXXXX escapes, as a browser sends them (a lone surrogate too)
            hdrs.setdefault("Content-Type", "application/json")
        before = snapshot()
        started = time.time()
        conn = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=SLOW + 5)
        status, text = 0, ""
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            status, text = resp.status, resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 — a dropped or silent server is a finding
            self.flag("no answer", method, path, f"{type(exc).__name__}: {exc}", body)
        finally:
            conn.close()
        took = time.time() - started
        self.quiet()
        err = self.stderr.take()
        if took > SLOW:
            self.flag("slow", method, path, f"{took:.1f}s", body)
        if status >= 500:
            self.flag("500", method, path, text, body)
        if "Traceback" in err:
            self.flag("traceback", method, path, err, body)
        while THREAD_ERRORS:
            self.flag("thread died", method, path, THREAD_ERRORS.pop(0), body)
        if (400 <= status < 500 or method == "GET") and status:
            diff = changed(before, snapshot())
            if diff:
                self.flag("data changed", method, path, f"{status}: {', '.join(diff)} — {text[:200]}", body)
        return status, text

    def quiet(self, limit: float = 60) -> None:
        """Wait until no chat is answering and no workflow runs (their writes are not this request's doing)."""
        end = time.time() + limit
        while time.time() < end:
            busy = any(getattr(x, "busy", False) for x in list(self.s.app.chats.sessions.values()))
            if not busy and not self.s.app.workflows._running:
                break
            time.sleep(0.1)
        time.sleep(0.02)

    # -------------------------------------------------------------- fixtures

    def fixtures(self) -> dict:
        s = self.s
        chat = s.api("POST", "/api/chats", {})["id"]
        skill = s.api("POST", "/api/skills", baseline("api_skill_create", {}))["id"]
        workflow = s.api("POST", "/api/workflows", baseline("api_workflow_new", {}))["id"]
        s.api("POST", "/api/connections/mcp", baseline("api_connection_add", {}))
        status, _, body = s.request("POST", "/api/captures?ext=png&label=fuzz", headers={"X-Crew": "1"},
                                    raw=test_app.PNG_1PX)
        capture = json.loads(body)["name"]
        run = "20260928-120000-fuzz-project"
        run_dir = runs_mod.runs_dir() / run
        run_dir.mkdir(parents=True, exist_ok=True)
        project = HOME / "fuzz-project"
        project.mkdir(exist_ok=True)
        (project / "index.html").write_text("<h1>Fuzz</h1>", encoding="utf-8")
        st = Store(run_dir / "team.db")
        st.set("phase", "stopped")
        st.set("goal", "Build a small page about Lahore.")
        st.set("repo", str(project))
        st.set("started_at", time.time() - 600)
        st.close()
        return {"chat": chat, "skill": skill, "workflow": workflow, "connection": "fuzz", "capture": capture,
                "run": run, "port": s.port}

    # -------------------------------------------------------------- the sweep

    def run(self) -> int:
        sys.stderr = self.stderr
        threading.excepthook = thread_hook
        self.spawned: list = []
        updater._get = lambda url, timeout=30: (_ for _ in ()).throw(OSError("offline (the fuzzer)"))
        claude_cli.update = lambda path=None: (True, "Claude Code is up to date (the fuzzer).")
        server.open_path = lambda path: None
        runs_mod.RunManager._spawn = lambda mgr, run_id, args: self.spawned.append((run_id, args))
        self.s = AppServer()
        try:
            ids = self.fixtures()
            self.sweep(ids)
        finally:
            sys.stderr = sys.__stderr__
            try:
                self.s.stop()
            except Exception:  # noqa: BLE001
                pass
        return 0 if not self.findings else 1

    def bodies(self, name: str, ids: dict, fields: list[str]):
        base = baseline(name, ids)
        values = QUICK_VALUES if self.quick else VALUES
        yield "base", base
        yield "no body", None
        yield "not json", b"{not json"
        for shape in ([1, 2], "text", 5, None):
            yield f"json {shape!r}", json.dumps(shape).encode()
        if name == "api_settings_save":
            for section, keys in SETTINGS_FIELDS.items():
                yield f"{section} as list", {section: ["x"]}
                for key in keys:
                    for v in values:
                        yield f"{section}.{key}={v!r:.30}", {section: {key: v}}
            for accounts in ([{"name": "a"}], [{"name": URDU, "vendor": "claude"}], [{"name": "x", "vendor": "gpt"}],
                             [{"name": "claude-1", "vendor": "claude", "profile": "../../etc"}], "claude-1", [None],
                             [{"name": "claude-1", "vendor": "claude"}] * 2, []):
                yield f"accounts={accounts!r:.40}", {"accounts": accounts}
            return
        for field in fields:
            yield f"{field} missing", {k: v for k, v in base.items() if k != field}
            for v in values + (SCHEDULES if field == "schedule" else []):
                yield f"{field}={v!r:.30}", {**base, field: v}

    def sweep(self, ids: dict) -> None:
        seen = set()
        for method, rx, name in server.ROUTES:
            if (method, rx.pattern) in seen:
                continue
            seen.add((method, rx.pattern))
            if self.only and self.only not in rx.pattern and self.only != name:
                continue
            if name in ("api_events", "api_chat_events", "api_browser_events", "api_phone_events",
                        "api_computer_events"):
                self.stream_check(rx, ids, name)
                continue
            fields, queries = handler_fields(name)
            print(f"{method} {rx.pattern[1:-1]}  ({name}; fields {fields}; query {queries})", file=sys.__stdout__)
            if name in DEVICE_ROUTES:
                self.devices(DEVICE_ROUTES[name], ids)
                continue
            if name == "api_connection_import":
                self.desktop_files()
            paths = path_ids(rx, ids, name)
            for label, path in paths:
                if method == "GET":
                    self.send("GET", path)
                    for q in queries:
                        for v in ("", "abc", "-1", "99999999999999999999", "%00", URDU, "../../x", "1e999"):
                            self.send("GET", f"{path}?{q}={quote(v)}")
                    continue
                if name in ("api_chat_upload", "api_capture_upload"):
                    self.uploads(path)
                    continue
                for _what, body in self.bodies(name, ids, fields if label == paths[0][0] else []):
                    raw = body if isinstance(body, bytes) else None
                    try:
                        self.send(method, path, None if raw is not None else body, raw=raw)
                    except Exception:  # noqa: BLE001 — a fault in the fuzzer itself: note it, carry on
                        self.flag("fuzzer error", method, path, traceback.format_exc(), body)
                    self.restore(ids, name)
                    if method == "DELETE" or name in ("api_chat_delete",):
                        ids.update(self.refresh(ids))

    def devices(self, kind: str, ids: dict) -> None:
        values = [v for v in (QUICK_VALUES if self.quick else VALUES) if v is not HUGE]
        for action, (template, fields) in ACTIONS[kind].items():
            base = json.loads(json.dumps(template).replace("{port}", str(ids["port"])))
            path = f"/api/{kind}/{action}"
            self.send("POST", path, base)
            self.send("POST", path, raw=b"[1]")
            for field in fields:
                self.send("POST", path, {k: v for k, v in base.items() if k != field})
                for v in values:
                    self.send("POST", path, {**base, field: v})
        if kind == "browser":  # a long text typed into the shared browser (once: it may keep it busy)
            self.send("POST", "/api/browser/type", {"text": "x" * 20000})

    def desktop_files(self) -> None:
        tmp = HOME / "fuzz-claude-desktop.json"
        saved = conn_mod.claude_desktop_config
        conn_mod.claude_desktop_config = lambda: tmp
        try:
            for text in DESKTOP_FILES:
                tmp.write_text(text, encoding="utf-8")
                self.send("POST", "/api/connections/import-claude", {})
                self.send("GET", "/api/connections")
        finally:
            conn_mod.claude_desktop_config = saved
            tmp.unlink(missing_ok=True)

    def uploads(self, path: str) -> None:
        for q in ("", "?name=CON.txt", "?name=" + quote("../../evil.py"), "?name=" + quote(URDU + ".pdf"),
                  "?ext=exe&label=x", "?ext=png&label=" + quote("../../x"), "?ext=&label="):
            self.send("POST", path + q, raw=b"hello")
        for length in ("-1", "abc", "0", str(10 ** 12)):
            self.send("POST", path, raw=b"", headers={"Content-Length": length})

    def stream_check(self, rx, ids: dict, name: str) -> None:
        """A live stream: it must open, and an odd id must not break it."""
        for _label, path in path_ids(rx, ids, name)[:3]:
            conn = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                if resp.status >= 500:
                    self.flag("500", "GET", path, resp.read(2000).decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                self.flag("no answer", "GET", path, f"{type(exc).__name__}: {exc}")
            finally:
                conn.close()
            self.count += 1

    def restore(self, ids: dict, name: str) -> None:
        """Put the settings back after each request, so one accepted change does not steer the next ones."""
        if name in ("api_settings_save", "api_secret_save", "api_rules_save", "api_assistant_save",
                    "api_phone_access", "api_startup"):
            if not hasattr(self, "_saved_files"):
                self._saved_files = {}
            for rel in ("crew.toml", "secrets.env", "team_rules.md", "assistant.md"):
                p = HOME / rel
                if rel not in self._saved_files:
                    self._saved_files[rel] = p.read_bytes() if p.is_file() else None
                saved = self._saved_files[rel]
                if saved is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(saved)

    def refresh(self, ids: dict) -> dict:
        """Objects a DELETE removed are made again, so the next requests still find them."""
        s, out = self.s, {}
        if not s.app.chats.get(ids["chat"]):
            out["chat"] = s.api("POST", "/api/chats", {})["id"]
        if not s.app.workflows.get(ids["workflow"]):
            out["workflow"] = s.api("POST", "/api/workflows", baseline("api_workflow_new", {}))["id"]
        if not (HOME / "captures" / ids["capture"]).is_file():
            status, _, body = s.request("POST", "/api/captures?ext=png&label=fuzz", headers={"X-Crew": "1"},
                                        raw=test_app.PNG_1PX)
            out["capture"] = json.loads(body)["name"]
        if "fuzz" not in conn_mod.load()["mcp"]:
            s.api("POST", "/api/connections/mcp", baseline("api_connection_add", {}))
        if not skills_mod.get(ids["skill"]):
            out["skill"] = s.api("POST", "/api/skills", {**baseline("api_skill_create", {}),
                                                         "name": f"Fuzz skill {time.time_ns()}"})["id"]
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer values per field")
    ap.add_argument("--only", help="only routes whose pattern contains this (or this handler name)")
    ap.add_argument("--report", help="write the findings to this JSON file")
    a = ap.parse_args()
    f = Fuzzer(a.quick, a.only)
    started = time.time()
    code = f.run()
    kinds: dict[str, int] = {}
    for x in f.findings:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print(f"\n{f.count} requests in {time.time() - started:.0f}s; {len(f.findings)} findings {kinds}",
          file=sys.__stdout__)
    if a.report:
        Path(a.report).write_text(json.dumps(f.findings, indent=1, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    status = main()
    from crewapp import browser  # the app's own browser (the browser route started it): stopped before leaving
    browser.service.stop()
    if browser.service.thread is not None:
        browser.service.thread.join(20)
    sys.stdout.flush()
    sys.__stdout__.flush()
    os._exit(status)  # a Playwright instance can crash while Python shuts down (a segfault after the report)
