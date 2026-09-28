"""The Assistant: a conversation with Claude (through Claude Code) or ChatGPT (through Codex), on your own
subscriptions.

Everything the model does is streamed to the app as it happens: the words, a "thinking" indicator, each
step it takes (searching, reading, using the browser …), its to-do list, helpers it starts (sub-agents),
its plan in plan mode, how full its context is, and your subscription limits.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from crewlib import claude_cli, config as cfgmod, connections, usage as usage_log
from crewlib.tiers import vendor_of
from crewlib.agents import (AUTH_RE, CREW_ROOT, LIMIT_RE, _kill_tree, _popen, _toml_str, child_env, codex_effort,
                            copy_claude_session, default_claude_home, default_codex_home, which)
from crewlib.util import atomic_write, clip, crew_home, load_env_file, now

from . import settings as settings_mod
from .sse import hub

ENGINES = {"claude": "Claude", "codex": "ChatGPT"}
EFFORTS = {"claude": ("auto", "low", "medium", "high", "xhigh", "max"),
           "codex": ("auto", "low", "medium", "high", "xhigh", "max", "ultra")}  # GPT-6's own names
LEGACY_EFFORTS = {"minimal": "low"}  # chats started before GPT-6 (it has no "minimal")
MODES = ("auto", "plan")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def other_product(engine: str, model: str | None) -> str:
    """The other product, when a model plainly belongs to it (a GPT model in a Claude chat, a Claude model in a
    ChatGPT chat); '' otherwise. Other names are left alone: the owner may use a model Crew does not know yet."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    if engine == "claude" and vendor_of(m) == "codex":
        return "codex"
    if engine == "codex" and (m.startswith("claude") or m in ("opus", "sonnet", "haiku", "fable")):
        return "claude"
    return ""

DEFAULT_INSTRUCTIONS = """You are the owner's personal assistant inside the Crew app.

The owner is not technical. Answer in plain, warm, precise language. Keep code out of your answers unless asked;
if you build something, describe what it does and how to use it.

- Keep answers short unless depth is asked for. Use headings and bullet points for anything longer than a paragraph.
- Describe what you are about to do before anything that sends messages, spends money or deletes something, and
  ask first.
"""

# How to use what Crew offers. Always added (the owner's editable instructions above are about tone and manner).
CREW_GUIDE = """How to work inside Crew:
- When you make something the owner will look at (a web page, a document, a chart, a table), save it as a file in
  the current folder (for example page.html, summary.md, budget.xlsx, letter.docx). The app shows new files in a
  panel beside the conversation. For Word, Excel, PowerPoint and PDF files use your docx, xlsx, pptx and pdf skills
  when you have them.
- You can browse the web in the app's own browser with the browser_* tools; the owner watches it live beside the
  conversation. Prefer it for tasks that need clicking, forms or logging in. Use web search for quick facts.
- You can operate the owner's Android phone with the phone_* tools, and their Windows computer (screen, mouse,
  keyboard, apps) with the computer_* tools, when they ask. Take a screenshot before acting and check the result
  after each step. If the owner pauses computer control, stop and ask.
- For multi-step work (three steps or more), keep a short to-do list with the todo_write tool (or your own task
  tools) so the owner can follow your progress: the whole list each time, one item in progress at a time.
- For a big build job (an app, a website with several parts, a larger program), suggest handing it to the team
  ("Build this with the team" in the chat's menu)."""

# Earlier default instructions: replaced by the new default when the owner never changed them.
_OLD_DEFAULTS = ('You are the owner\'s personal assistant inside the Crew app.\n\nThe owner is not technical. Answer in plain, warm, precise language. Keep code out of your answers unless asked;\nif you build something, describe what it does and how to use it.\n\n- When you make something the owner will look at (a web page, a document, a chart, a table), save it as a file in\n  the current folder (for example page.html or summary.md). The app shows new files in a preview panel.\n- You can browse the web in the app\'s own browser with the browser_* tools; the owner watches it live. Prefer it\n  for tasks that need clicking, forms or logging in. Use web search for quick facts.\n- You can operate the owner\'s Android phone with the phone_* tools, and their Windows computer (screen, mouse,\n  keyboard, apps) with the computer_* tools, when they ask. Take a screenshot before acting and check the result\n  after each step. Describe what you are about to do before anything that sends messages, spends money or deletes\n  something, and ask first. If the owner pauses computer control, stop and ask.\n- For a big build job (an app, a website with several parts, a larger program), suggest turning the conversation into\n  a team project with the "Build this with the team" button.\n- Keep answers short unless depth is asked for. Use headings and bullet points for anything longer than a paragraph.\n', 'You are the owner\'s personal assistant inside the Crew app.\n\nThe owner is not technical. Answer in plain, warm, precise language. Keep code out of your answers unless asked;\nif you build something, describe what it does and how to use it.\n\n- When you make something the owner will look at (a web page, a document, a chart, a table), save it as a file in\n  the current folder (for example page.html or summary.md). The app shows new files in a preview panel.\n- You can browse the web in the app\'s own browser with the browser_* tools; the owner watches it live beside the\n  conversation. Prefer it for tasks that need clicking, forms or logging in. Use web search for quick facts.\n- You can operate the owner\'s Android phone with the phone_* tools, and their Windows computer (screen, mouse,\n  keyboard, apps) with the computer_* tools, when they ask. Take a screenshot before acting and check the result\n  after each step. Describe what you are about to do before anything that sends messages, spends money or deletes\n  something, and ask first. If the owner pauses computer control, stop and ask.\n- For multi-step work (three steps or more), keep a short to-do list with the todo_write tool (or your own task\n  tools) so the owner can follow your progress: the whole list each time, one item in progress at a time.\n- For a big build job (an app, a website with several parts, a larger program), suggest turning the conversation into\n  a team project with the "Build with the team" button.\n- Keep answers short unless depth is asked for. Use headings and bullet points for anything longer than a paragraph.\n')

PLAN_FILE_RE = re.compile(r"[\\/]plans[\\/][^\\/]+\.md$", re.I)

PLAN_NOTE = """Plan mode: the owner reviews your plan before anything is changed. Investigate as needed, write the
plan, then end your turn with a short summary of it. The owner approves plans with a button in the app, so do not
look for an ExitPlanMode tool."""


# ================================================================== describing steps


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url
    except ValueError:
        return url


def describe(name: str, inp: dict | None) -> tuple[str, str, str]:
    """(kind, label, detail) for a tool call, in plain words."""
    inp = inp or {}
    short = lambda v, n=90: clip(" ".join(str(v or "").split()), n)  # noqa: E731
    fname = lambda: Path(str(inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "")).name  # noqa: E731
    if name == "mcp__crew_devices__todo_write":
        return "todo", "Updated the to-do list", ""
    if name.startswith("mcp__crew_devices__"):
        action = name.replace("mcp__crew_devices__", "")
        group, _, verb = action.partition("_")
        where = {"browser": "the browser", "phone": "your phone", "computer": "your computer"}.get(group, group)
        detail = inp.get("url") or inp.get("text") or inp.get("name") or inp.get("target") or inp.get("keys") or \
            inp.get("field") or inp.get("direction") or ""
        labels = {"open": f"Opened {_host(str(detail)) if group == 'browser' else detail}", "read": f"Read the page in {where}",
                  "screenshot": f"Looked at {where}", "click": f"Clicked in {where}", "fill": "Filled in a form",
                  "type": f"Typed in {where}", "press": "Pressed a key", "scroll": f"Scrolled {where}",
                  "back": "Went back", "device": "Switched the browser size", "tap": "Tapped on your phone",
                  "swipe": "Swiped on your phone", "key": f"Pressed a key on {where}", "open_app": "Opened an app",
                  "screen": "Read your phone's screen", "status": f"Checked {where}", "drag": "Dragged on your computer"}
        return group, labels.get(verb, f"Used {where}"), short(detail)
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return "connection", f"Used {server.replace('_', ' ')}", tool.replace("_", " ")
    table = {
        "WebSearch": ("web", "Searched the web", short(inp.get("query"))),
        "WebFetch": ("web", f"Read {_host(str(inp.get('url', '')))}", short(inp.get("url"), 120)),
        "Read": ("file", f"Read {fname() or 'a file'}", ""),
        "Write": ("file", f"Wrote {fname() or 'a file'}", ""),
        "Edit": ("file", f"Edited {fname() or 'a file'}", ""),
        "MultiEdit": ("file", f"Edited {fname() or 'a file'}", ""),
        "NotebookEdit": ("file", f"Edited {fname() or 'a notebook'}", ""),
        "Glob": ("search", "Looked through files", short(inp.get("pattern"))),
        "Grep": ("search", "Searched in files", short(inp.get("pattern"))),
        "Bash": ("run", short(inp.get("description"), 70) or "Ran a command", short(inp.get("command"), 140)),
        "PowerShell": ("run", short(inp.get("description"), 70) or "Ran a command", short(inp.get("command"), 140)),
        "Task": ("agent", f"Started a helper: {short(inp.get('description'), 60)}", short(inp.get("subagent_type"))),
        "Agent": ("agent", f"Started a helper: {short(inp.get('description'), 60)}", short(inp.get("subagent_type"))),
        "Skill": ("skill", f"Used the {short(inp.get('skill') or inp.get('name') or 'skill', 40)} skill", ""),
        "ToolSearch": ("setup", "Got tools ready", ""),
        "TodoWrite": ("todo", "Updated the to-do list", ""),
        "TaskCreate": ("todo", "Added to the to-do list", short(inp.get("subject"))),
        "TaskUpdate": ("todo", "Updated the to-do list", ""),
        "TaskList": ("todo", "Checked the to-do list", ""),
        "CronCreate": ("schedule", "Scheduled a task", short(inp.get("prompt") or inp.get("name"))),
    }
    return table.get(name, ("tool", name.replace("_", " "), ""))


def file_kind(name: str) -> str:
    return {".html": "web", ".htm": "web", ".md": "doc", ".txt": "doc", ".png": "image", ".jpg": "image",
            ".jpeg": "image", ".gif": "image", ".svg": "image", ".webp": "image", ".pdf": "pdf",
            ".csv": "table", ".docx": "file", ".xlsx": "file", ".pptx": "file"}.get(Path(name).suffix.lower(), "file")


# Folders of tools and packages, not of things made for the owner (a web app's node_modules holds thousands).
NOT_MADE = {".crew", ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".next", "dist-cache"}


def workspace_files(root: Path):
    """(relative path, stat) of the files in a chat's folder that were made for the owner. Files come and go while
    an assistant works (temporary files, a build): one that vanishes mid-scan is simply skipped."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in NOT_MADE:
                        stack.append(Path(entry.path))
                elif entry.is_file():
                    yield Path(entry.path).relative_to(root).as_posix(), entry.stat()
            except OSError:
                continue


# ================================================================== storage


def instructions_path() -> Path:
    return crew_home() / "assistant.md"


def instructions() -> str:
    p = instructions_path()
    if not p.is_file() or p.read_text(encoding="utf-8").strip() in {x.strip() for x in _OLD_DEFAULTS}:
        atomic_write(p, DEFAULT_INSTRUCTIONS)
    return p.read_text(encoding="utf-8")


def save_instructions(text: str) -> None:
    if text is not None and not isinstance(text, str):
        raise ValueError("Write the instructions as text.")
    atomic_write(instructions_path(), (text or DEFAULT_INSTRUCTIONS).rstrip() + "\n")


class ChatDB:
    COLUMNS = {"engine": "TEXT DEFAULT 'claude'", "mode": "TEXT DEFAULT 'auto'", "account": "TEXT",
               "context_tokens": "INTEGER", "context_window": "INTEGER", "pinned": "INTEGER DEFAULT 0",
               "kind": "TEXT DEFAULT 'chat'"}

    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL,
                session_id TEXT, model TEXT, effort TEXT);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, role TEXT,
                text TEXT, ts REAL, meta TEXT);
            CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id);
        """)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(chats)")}
        for col, decl in self.COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE chats ADD COLUMN {col} {decl}")

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self._lock:
            return int(self.db.execute(sql, args).lastrowid or 0)

    def add_message(self, chat_id: str, role: str, text: str, meta: dict) -> int:
        """A message for a chat that still exists (checked in the same step as the writing, so an answer that ends
        just as its chat is deleted is not kept on its own); its id, or 0 when the chat is gone."""
        with self._lock:
            cur = self.db.execute("INSERT INTO messages(chat_id,role,text,ts,meta) SELECT ?,?,?,?,? "
                                  "WHERE EXISTS (SELECT 1 FROM chats WHERE id=?)",
                                  (chat_id, role, text, now(), json.dumps(meta), chat_id))
            return int(cur.lastrowid or 0) if cur.rowcount == 1 else 0


# ================================================================== one turn's state


class Turn:
    def __init__(self):
        self.started = now()
        self.text = ""
        self.steps: dict[str, dict] = {}
        self.todos: list[dict] = []
        self.todo_ids: dict[str, str] = {}  # tool_use id → task number
        self.agents: dict[str, dict] = {}
        self.plan = ""
        self.plan_files: dict[str, str] = {}
        self.thinking_tokens = 0
        self.compacted = None
        self.error_notice = ""

    def meta(self) -> dict:
        return {"steps": list(self.steps.values())[-80:], "todos": self.todos,
                "agents": list(self.agents.values()), "plan": self.plan, "thinking": self.thinking_tokens,
                "compacted": self.compacted}


# ================================================================== sessions


class Session:
    engine = "claude"

    def __init__(self, manager: "ChatManager", chat: dict):
        self.m, self.chat_id = manager, chat["id"]
        self.session_id = chat.get("session_id")
        self.model, self.effort = chat.get("model"), chat.get("effort") or "auto"
        self.mode = chat.get("mode") or "auto"
        self.account = None
        self.chosen = chat.get("account") or ""  # the subscription the owner chose for this chat ("" = automatic)
        self.holder = next((m["meta"].get("account") for m in reversed(chat.get("messages") or [])
                            if m.get("role") == "assistant" and (m.get("meta") or {}).get("account")), "")
        self.proc = None
        self.busy = False
        self.interrupted = False
        self.turn: Turn | None = None
        self.last_prompt = ""
        self.context = {"used": chat.get("context_tokens") or 0, "window": chat.get("context_window") or 0}
        self._lock = threading.Lock()

    @property
    def topic(self) -> str:
        return f"chat:{self.chat_id}"

    def publish(self, event: str, data: dict) -> None:
        hub.publish(self.topic, event, data)

    def workspace(self) -> Path:
        path = crew_home() / "chats" / self.chat_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop_process(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            _kill_tree(self.proc, grace=2)
        self.proc = None

    # ------------------------------------------------------------ common helpers

    def _secret_env(self) -> dict:
        return {k: v for k, v in load_env_file(settings_mod.secrets_path()).items() if k != "ANTHROPIC_API_KEY"}

    def _system_text(self) -> str:
        text = instructions().rstrip() + "\n\n" + CREW_GUIDE
        keys = sorted(self._secret_env())
        if keys:
            names = ", ".join(f"{k}" + (f" ({connections.key_label(k)})" if connections.key_label(k) else "") for k in keys)
            text += ("\n\nAPI keys the owner added are available to you as environment variables: " + names +
                     ". Use them in commands and code when useful; never show their values.")
        return text

    def _step(self, sid: str, name: str, inp: dict, status: str = "running") -> dict:
        kind, label, detail = describe(name, inp)
        if self.mode == "plan" and name in ("Write", "Edit", "MultiEdit") and PLAN_FILE_RE.search(str(inp.get("file_path", ""))):
            kind, label, detail = "plan", "Wrote the plan" if name == "Write" else "Revised the plan", ""
        step = {"id": sid, "kind": kind, "label": label, "detail": detail, "status": status, "tool": name}
        self.turn.steps[sid] = step
        self.publish("step", step)
        return step

    def _step_done(self, sid: str, ok: bool) -> None:
        step = self.turn.steps.get(sid) if self.turn else None
        if step and step["status"] == "running":
            step["status"] = "done" if ok else "error"
            self.publish("step", step)

    def _record(self, text: str, meta: dict) -> int:
        mid = self.m.db.add_message(self.chat_id, "assistant", text, meta)
        self.m.db.x("UPDATE chats SET updated=? WHERE id=?", (now(), self.chat_id))
        return mid

    def _new_files(self, since: float) -> list[dict]:
        out = []
        for rel, st in workspace_files(self.workspace()):
            if not rel.startswith("attachments/") and st.st_mtime >= since - 1:
                out.append({"name": rel, "kind": file_kind(rel), "url": f"/files/chat/{self.chat_id}/{rel}"})
        return sorted(out, key=lambda f: f["name"])[:20]

    def _context_event(self) -> None:
        used, window = self.context.get("used") or 0, self.context.get("window") or 0
        percent = round(100 * used / window, 1) if window else None
        self.publish("context", {"used": used, "window": window, "percent": percent,
                                 "auto_at": self.context.get("auto_at")})
        self.m.db.x("UPDATE chats SET context_tokens=?, context_window=? WHERE id=?", (used, window or None, self.chat_id))

    def _rate_event(self, info: dict) -> None:
        if not self.account:
            return
        usage_log.record_rate(self.account.name, info)
        windows = info.get("unifiedWindows") or {}
        self.publish("rate", {"account": self.account.name, "status": info.get("status"),
                              "five": windows.get("five_hour"), "week": windows.get("seven_day")})

    def _give_up(self, exc: Exception) -> None:
        """Close the turn with what was said so far and a plain note, when finishing it properly failed."""
        t = self.turn or Turn()
        text = ((t.text.strip() + "\n\n") if t.text.strip() else "") + f"(Crew could not finish showing this answer: {exc})"
        meta = {"error": True, "engine": self.engine, "model": self.model or "", "effort": self.effort, "mode": self.mode,
                "account": self.account.name if self.account else ""}
        try:
            mid = self._record(text, meta)
        except Exception:  # noqa: BLE001 — even the record failed: the answer is still shown
            mid = 0
        self.proc, self.busy, self.turn = (self.proc if self.engine == "claude" else None), False, None
        self.publish("done", {"id": mid, "text": text, "meta": meta})


class ClaudeSession(Session):
    """One long-lived Claude Code process per conversation (stream-json in and out)."""

    engine = "claude"

    def _start(self, account=None) -> None:
        exe = which("claude")
        if not exe:
            raise RuntimeError("Claude Code is not installed on this computer yet. Run the Crew installer.")
        app = settings_mod.load()
        cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
        cfg.models.check(self.model)
        before = self.account.name if self.account else self.holder
        self.account = account or self.m.pick_account(cfg, "claude", prefer=self.chosen)
        self._bring_conversation(cfg)
        if self.chosen and self.account and before and self.account.name != before and account is None:
            self.publish("notice", {"text": f"Now using {self.account.name}.", "kind": "account"})
        files = self.workspace() / ".crew"
        files.mkdir(exist_ok=True)
        (files / "system.md").write_text(self._system_text() + "\n\n" + PLAN_NOTE, encoding="utf-8")
        servers = dict(connections.mcp_servers())
        if self.m.app_url:
            import sys
            servers["crew_devices"] = {
                "type": "stdio", "command": sys.executable, "args": ["-m", "crewapp.devices_mcp"],
                "env": {"CREW_APP_URL": self.m.app_url, "CREW_APP_TOKEN": self.m.app_token,
                        "PYTHONPATH": str(CREW_ROOT)}}
        (files / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        (files / "settings.json").write_text(json.dumps({"model": self.model}), encoding="utf-8")
        act = "bypassPermissions" if app["team"].get("permission_mode", "bypassPermissions") == "bypassPermissions" \
            else "auto"
        self.act_mode = act
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", self.model,
               "--append-system-prompt-file", str(files / "system.md"),
               "--mcp-config", str(files / "mcp.json"),
               "--settings", str(files / "settings.json"),
               "--allow-dangerously-skip-permissions",
               "--permission-mode", "plan" if self.mode == "plan" else act]
        if self.effort and self.effort != "auto":
            cmd += ["--effort", self.effort]
        pack = crew_home() / "skills-active"
        if (pack / ".claude-plugin" / "plugin.json").is_file():
            cmd += ["--plugin-dir", str(pack)]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        env = {"CLAUDE_CODE_SUBAGENT_MODEL": cfg.models.work, "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": cfg.models.work, "DISABLE_AUTOUPDATER": "1"}
        prof = self.account.profile_dir() if self.account else None
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = str(prof)
        self.proc = _popen(cmd, self.workspace(), child_env({**self._secret_env(), **env}))
        self.running_mode = self.mode
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=lambda p=self.proc: [None for _ in p.stderr], daemon=True).start()

    def _bring_conversation(self, cfg) -> None:
        """Before resuming, make sure the chosen subscription holds this conversation: bring it from whichever
        Claude subscription has it (the owner switched, or Crew restarted and picked another one)."""
        if not self.session_id or self.account is None:
            return
        home = self.account.profile_dir() or default_claude_home()
        if list((home / "projects").glob(f"*/{self.session_id}.jsonl")):
            return
        for other in cfg.accounts_for("claude"):
            if other.name != self.account.name and copy_claude_session(self.session_id, other, self.account):
                return

    def send(self, prompt: str, model: str, effort: str, mode: str, account=None) -> None:
        with self._lock:
            if self.busy:
                raise RuntimeError("Still answering the last message.")
            switched = bool(self.chosen and self.account and self.chosen != self.account.name)
            if self.alive() and (model != self.model or effort != self.effort or switched or
                                 (account is not None and self.account and account.name != self.account.name)):
                self.stop_process()  # a new model, effort or subscription: resume the conversation in a fresh process
            self.model, self.effort, self.mode = model, effort, mode
            if not self.alive():
                self._start(account)
            elif self.running_mode != mode:
                self._control({"subtype": "set_permission_mode", "mode": "plan" if mode == "plan" else self.act_mode})
                self.running_mode = mode
            self.busy, self.turn, self.last_prompt = True, Turn(), prompt
            msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
                   "parent_tool_use_id": None, "session_id": self.session_id or ""}
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        self.publish("start", {"engine": "claude", "model": model, "effort": effort, "mode": mode,
                               "account": self.account.name if self.account else ""})

    def _control(self, request: dict) -> None:
        if self.alive():
            try:
                self.proc.stdin.write(json.dumps({"type": "control_request", "request_id": uuid.uuid4().hex,
                                                  "request": request}) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def interrupt(self) -> None:
        if self.busy:
            self.interrupted = True
        self._control({"subtype": "interrupt"})

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.alive() and not self.busy:
            self._control({"subtype": "set_permission_mode", "mode": "plan" if mode == "plan" else self.act_mode})
            self.running_mode = mode

    # ------------------------------------------------------------ reading the stream

    def _read(self, proc) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                self._handle(msg)
            except Exception as exc:  # one odd event must not end the conversation
                self.publish("notice", {"text": f"(display problem: {exc})", "kind": "debug"})
        if self.busy and proc is self.proc:  # the process died mid-turn
            self._finish({"is_error": True, "result": "The assistant stopped unexpectedly. Please send that again."})

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        parent = msg.get("parent_tool_use_id")
        t = self.turn
        if kind == "system":
            sub = msg.get("subtype")
            if sub == "init" and not parent:
                self.session_id = msg.get("session_id") or self.session_id
                self.m.db.x("UPDATE chats SET session_id=? WHERE id=?", (self.session_id, self.chat_id))
                self.m.remember_init(msg)
            elif sub == "thinking_tokens" and t is not None and not parent:
                t.thinking_tokens = int(msg.get("estimated_tokens") or t.thinking_tokens)
                self.publish("thinking", {"tokens": t.thinking_tokens})
            elif sub == "status" and msg.get("status") == "compacting":
                self.publish("status", {"text": "Compacting the conversation…"})
            elif sub == "compact_boundary":
                meta = msg.get("compact_metadata") or {}
                if t is not None:
                    t.compacted = {"pre": meta.get("pre_tokens"), "post": meta.get("post_tokens")}
                self.context["used"] = meta.get("post_tokens") or self.context.get("used")
                self.publish("compacted", {"pre": meta.get("pre_tokens"), "post": meta.get("post_tokens"),
                                           "trigger": meta.get("trigger")})
                self._context_event()
        elif kind == "autocompact_state":
            value = msg.get("value") or {}
            self.context["auto_at"] = value.get("threshold")
        elif kind == "stream_event" and t is not None:
            ev = msg.get("event") or {}
            if parent:
                return
            etype = ev.get("type")
            if etype == "message_start":
                usage = (ev.get("message") or {}).get("usage") or {}
                used = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                             "cache_creation_input_tokens"))
                if used:
                    self.context["used"] = used
                    if self.context.get("window"):
                        self._context_event()
            elif etype == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "thinking":
                    self.publish("thinking", {"tokens": t.thinking_tokens, "active": True})
                elif block.get("type") == "text" and t.text and not t.text.endswith("\n"):
                    t.text += "\n\n"
                    self.publish("delta", {"text": "\n\n"})
            elif etype == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    piece = delta.get("text") or ""
                    t.text += piece
                    self.publish("delta", {"text": piece})
        elif kind == "assistant" and t is not None:
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                name, inp, bid = block.get("name", ""), block.get("input") or {}, block.get("id") or uuid.uuid4().hex
                if parent:  # a helper (sub-agent) at work
                    agent = t.agents.get(parent)
                    if agent:
                        agent["steps"] += 1
                        agent["last"] = describe(name, inp)[1]
                        self.publish("agent", agent)
                    continue
                self._step(bid, name, inp)
                if name in ("Task", "Agent"):
                    t.agents[bid] = {"id": bid, "type": inp.get("subagent_type") or "helper",
                                     "description": clip(inp.get("description") or "", 80), "status": "running",
                                     "steps": 0, "last": "", "started": now()}
                    self.publish("agent", t.agents[bid])
                elif name in ("TodoWrite", "mcp__crew_devices__todo_write"):
                    t.todos = [{"text": x.get("content") or x.get("activeForm") or "", "status": x.get("status", "pending")}
                               for x in inp.get("todos") or []]
                    self.publish("todos", {"items": t.todos})
                elif name == "TaskCreate":
                    t.todos.append({"text": inp.get("subject") or inp.get("description") or "", "status": "pending",
                                    "key": bid})
                    self.publish("todos", {"items": t.todos})
                elif name == "TaskUpdate":
                    key = str(inp.get("taskId") or inp.get("id") or "")
                    for todo in t.todos:
                        if todo.get("num") == key or todo.get("key") == key:
                            if inp.get("status"):
                                todo["status"] = {"completed": "completed", "in_progress": "in_progress"}.get(
                                    inp["status"], inp["status"])
                            if inp.get("subject"):
                                todo["text"] = inp["subject"]
                    self.publish("todos", {"items": t.todos})
                elif name in ("Write", "Edit", "MultiEdit") and self.mode == "plan" \
                        and PLAN_FILE_RE.search(str(inp.get("file_path", ""))):
                    t.plan_files[bid] = str(inp["file_path"])
                    if name == "Write" and inp.get("content"):
                        t.plan = inp["content"]
                        self.publish("plan", {"text": t.plan})
        elif kind == "user" and t is not None:
            content = (msg.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                bid = block.get("tool_use_id") or ""
                ok = not block.get("is_error")
                if parent:
                    continue
                self._step_done(bid, ok)
                if bid in t.agents:
                    t.agents[bid]["status"] = "done" if ok else "error"
                    t.agents[bid]["seconds"] = round(now() - t.agents[bid]["started"])
                    self.publish("agent", t.agents[bid])
                if bid in t.plan_files:  # read the plan as saved (an edit only carries the changed lines)
                    try:
                        t.plan = Path(t.plan_files[bid]).read_text(encoding="utf-8")
                        self.publish("plan", {"text": t.plan})
                    except OSError:
                        pass
                step = t.steps.get(bid)
                if step and step.get("tool") == "TaskCreate":
                    text = json.dumps(block.get("content"))
                    num = re.search(r"#(\d+)", text)
                    for todo in t.todos:
                        if todo.get("key") == bid and num:
                            todo["num"] = num.group(1)
        elif kind == "rate_limit_event":
            self._rate_event(msg.get("rate_limit_info") or {})
        elif kind == "result":
            if not parent:
                self._finish(msg)

    def _finish(self, msg: dict) -> None:
        try:
            self._finish_turn(msg)
        except Exception as exc:  # whatever went wrong, the conversation must not wait for ever
            self._give_up(exc)

    def _finish_turn(self, msg: dict) -> None:
        t = self.turn or Turn()
        raw = (msg.get("result") or "").strip()
        error = bool(msg.get("is_error"))
        for model_usage in (msg.get("modelUsage") or {}).values():
            if model_usage.get("contextWindow"):
                self.context["window"] = int(model_usage["contextWindow"])
        usage = msg.get("usage") or {}
        command = msg.get("local_command") or ""
        if self.account and not command:
            usage_log.record_tokens(self.account.name, usage)
        if self.context.get("window"):
            self._context_event()
        text = t.text.strip() or raw
        if command == "compact" and not text:
            c = t.compacted or {}
            text = ("Compacted the conversation" + (f": {c['pre']:,} → {c['post']:,} tokens." if c.get("pre") and c.get("post")
                                                    else "."))
        retry = None
        stopped, self.interrupted = self.interrupted, False
        if stopped:
            error = False
            text = (t.text.strip() + "\n\n*Stopped.*") if t.text.strip() else "*Stopped.*"
        if error:
            needed = claude_cli.required_version(raw)
            if needed:
                text = ("Claude Code on this computer is older than this model needs (version "
                        f"{needed} or newer). Crew is updating it now and will answer again by itself.")
                retry = "update"
            elif LIMIT_RE.search(raw) and self.m.other_account(self.account, "claude"):
                text = f"{self.account.name} has reached its usage limit. Continuing on another subscription…"
                retry = "failover"
            elif not t.text.strip():
                text = self.m.explain_error(raw)
        for step in t.steps.values():
            if step["status"] == "running":
                step["status"] = "done"
        meta = {**t.meta(), "files": self._new_files(t.started), "error": error and not retry, "notice": bool(retry),
                "engine": "claude", "model": self.model, "effort": self.effort, "mode": self.mode,
                "account": self.account.name if self.account else "", "command": command, "stopped": stopped,
                "seconds": round(now() - t.started, 1),
                "usage": {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0),
                          "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                          "cache_write": int(usage.get("cache_creation_input_tokens") or 0)},
                "context": dict(self.context)}
        mid = self._record(text, meta)
        self.busy, self.turn = False, None
        self.publish("done", {"id": mid, "text": text, "meta": meta})
        if retry == "update":
            self.m.fix_outdated(self)
        elif retry == "failover":
            self.m.failover(self)


class CodexSession(Session):
    """ChatGPT through Codex: one `codex exec --json` per turn, continued with `codex exec resume`."""

    engine = "codex"

    def send(self, prompt: str, model: str, effort: str, mode: str, account=None) -> None:
        with self._lock:
            if self.busy:
                raise RuntimeError("Still answering the last message.")
            exe = which("codex")
            if not exe:
                raise RuntimeError("Codex (for ChatGPT) is not installed. Run the Crew installer and answer Yes to "
                                   "ChatGPT.")
            cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
            holder = self.account or next((a for a in cfg.accounts_for("codex") if a.name == self.holder), None)
            if account is None and self.session_id and holder is not None and (not self.chosen or self.chosen == holder.name):
                account = holder  # a ChatGPT conversation continues on the subscription that holds it
            new = account or self.m.pick_account(cfg, "codex", prefer=self.chosen)
            if new is None:
                raise RuntimeError("Add your ChatGPT subscription first: Settings → Subscriptions → Add → ChatGPT.")
            if self.session_id and holder is not None and new.name != holder.name:
                # ChatGPT conversations cannot move between subscriptions: continue from a summary of this one.
                self.session_id = None
                self.m.db.x("UPDATE chats SET session_id=NULL WHERE id=?", (self.chat_id,))
                prompt = self._recap() + prompt
                self.publish("notice", {"text": f"Now using {new.name}. A ChatGPT conversation cannot move to "
                                                "another subscription, so it continues from a summary of this one.",
                                        "kind": "account"})
            self.account = new
            self.model, self.effort, self.mode = model, effort, mode
            self.busy, self.turn, self.last_prompt = True, Turn(), prompt
        self.publish("start", {"engine": "codex", "model": model or "", "effort": effort, "mode": mode,
                               "account": self.account.name})
        threading.Thread(target=self._turn, args=(exe, prompt), daemon=True).start()

    def _recap(self) -> str:
        chat = self.m.get(self.chat_id) or {}
        lines = [f"{'Owner' if m['role'] == 'user' else 'You'}: {clip(m['text'], 700)}"
                 for m in (chat.get("messages") or [])[-13:-1] if m.get("text")]  # the newest is sent as the prompt
        return ("The conversation so far (it moved to another ChatGPT subscription, so here is a recap; continue "
                "from it):\n" + "\n".join(lines) + "\n\n---\n\n") if lines else ""

    def _args(self) -> list[str]:
        import sys
        args = ["-c", 'approval_policy="never"']
        effort = codex_effort(self.effort, self.model)
        if effort:
            args += ["-c", f"model_reasoning_effort={_toml_str(effort)}"]
        if self.model:
            args += ["-m", self.model]
        if self.m.app_url:
            env_table = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in {
                "CREW_APP_URL": self.m.app_url, "CREW_APP_TOKEN": self.m.app_token,
                "PYTHONPATH": str(CREW_ROOT)}.items()) + "}"
            args += ["-c", f"mcp_servers.crew_devices.command={_toml_str(sys.executable)}",
                     "-c", 'mcp_servers.crew_devices.args=["-m", "crewapp.devices_mcp"]',
                     "-c", f"mcp_servers.crew_devices.env={env_table}"]
        if self.mode == "plan":
            args += ["-c", 'sandbox_mode="read-only"']
        else:
            app = settings_mod.load()
            if app["team"].get("permission_mode", "bypassPermissions") == "bypassPermissions":
                args.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                args += ["-c", 'sandbox_mode="workspace-write"', "-c", "sandbox_workspace_write.network_access=true"]
        return args

    def _turn(self, exe: str, prompt: str) -> None:
        try:
            self._run_turn(exe, prompt)
        except Exception as exc:  # the conversation must never be left "still answering"
            self._give_up(exc)

    def _run_turn(self, exe: str, prompt: str) -> None:
        t = self.turn
        if self.session_id:
            cmd = [exe, "exec", "resume", "--json", "--skip-git-repo-check", *self._args(), self.session_id, "-"]
            text_in = prompt
        else:
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", *self._args(), "-C", str(self.workspace()), "-"]
            text_in = self._system_text() + "\n\n---\n\n" + prompt
        if self.mode == "plan":
            text_in = ("PLAN FIRST: do not change anything yet. Investigate if needed, then reply with a clear, "
                       "numbered plan and wait for the owner's approval.\n\n" + text_in)
        env = dict(self._secret_env())
        prof = self.account.profile_dir()
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            env["CODEX_HOME"] = str(prof)
        error_text, usage, code = "", {}, 1
        try:
            proc = self.proc = _popen(cmd, self.workspace(), child_env(env))
            proc.stdin.write(text_in)
            proc.stdin.close()
            threading.Thread(target=lambda p=proc: [None for _ in p.stderr], daemon=True).start()
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                etype = ev.get("type")
                item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
                itype = item.get("type")
                if etype == "thread.started":
                    self.session_id = ev.get("thread_id")
                    self.m.db.x("UPDATE chats SET session_id=? WHERE id=?", (self.session_id, self.chat_id))
                elif etype in ("item.started", "item.updated", "item.completed") and item:
                    iid = item.get("id") or uuid.uuid4().hex
                    if itype == "agent_message":
                        full = item.get("text") or ""
                        if full.startswith(t.text.rstrip()) and len(full) > len(t.text.rstrip()):
                            piece = full[len(t.text.rstrip()):]
                        elif etype == "item.completed" and full and full not in t.text:
                            piece = ("\n\n" if t.text else "") + full
                        else:
                            piece = ""
                        if piece:
                            t.text += piece
                            self.publish("delta", {"text": piece})
                    elif itype == "reasoning":
                        t.thinking_tokens += len((item.get("text") or "").split())
                        self.publish("thinking", {"tokens": t.thinking_tokens, "text": clip(item.get("text") or "", 300)})
                    elif itype == "todo_list":
                        t.todos = [{"text": x.get("text", ""), "status": "completed" if x.get("completed") else "pending"}
                                   for x in item.get("items") or []]
                        self.publish("todos", {"items": t.todos})
                    elif itype in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                        if etype == "item.started" or iid not in t.steps:
                            if itype == "command_execution":
                                self._raw_step(iid, "run", "Ran a command", clip(item.get("command") or "", 140))
                            elif itype == "file_change":
                                names = ", ".join(Path(c.get("path", "")).name for c in item.get("changes") or [])
                                self._raw_step(iid, "file", "Changed files", clip(names, 120))
                            elif itype == "web_search":
                                self._raw_step(iid, "web", "Searched the web", clip(item.get("query") or "", 90))
                            else:
                                name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
                                self._step(iid, name, item.get("arguments") or {})
                        if etype == "item.completed":
                            failed = item.get("status") == "failed" or (itype == "command_execution"
                                                                         and item.get("exit_code") not in (0, None))
                            self._step_done(iid, not failed)
                    elif itype == "error" and etype == "item.completed":
                        error_text = item.get("message") or error_text
                elif etype == "turn.completed":
                    usage = ev.get("usage") or {}
                elif etype == "turn.failed":
                    err = ev.get("error") or {}
                    error_text = (err.get("message") if isinstance(err, dict) else str(err)) or error_text
                elif etype == "error":
                    error_text = ev.get("message") or error_text
            code = proc.wait()
        except OSError as exc:
            code, error_text = 1, str(exc)
        if not self.m.db.q("SELECT id FROM chats WHERE id=?", (self.chat_id,)):
            self.proc, self.busy, self.turn = None, False, None  # deleted while it answered: nothing to keep
            return
        stopped, self.interrupted = self.interrupted, False
        failed = code != 0 and not t.text.strip() and not stopped
        usage_log.record_tokens(self.account.name, {
            "input_tokens": int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0)})
        status = read_codex_status(self.account, self.session_id)
        if status.get("rate"):
            self._rate_event(status["rate"])
        if status.get("window"):
            self.context.update(used=status.get("used") or 0, window=status["window"])
            self._context_event()
        text = t.text.strip() or (self.m.explain_error(error_text) if failed else "")
        if stopped:  # the owner pressed Stop: what was said so far stays, as with Claude
            text = (t.text.strip() + "\n\n*Stopped.*") if t.text.strip() else "*Stopped.*"
        for step in t.steps.values():
            if step["status"] == "running":
                step["status"] = "done"
        meta = {**t.meta(), "files": self._new_files(t.started), "error": failed, "engine": "codex",
                "model": self.model or "", "effort": self.effort, "mode": self.mode, "account": self.account.name,
                "stopped": stopped, "seconds": round(now() - t.started, 1),
                "usage": {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0),
                          "cache_read": int(usage.get("cached_input_tokens") or 0), "cache_write": 0},
                "context": dict(self.context)}
        mid = self._record(text or "(no answer)", meta)
        self.proc, self.busy, self.turn = None, False, None
        self.publish("done", {"id": mid, "text": text or "(no answer)", "meta": meta})

    def _raw_step(self, sid: str, kind: str, label: str, detail: str) -> None:
        step = {"id": sid, "kind": kind, "label": label, "detail": detail, "status": "running", "tool": kind}
        self.turn.steps[sid] = step
        self.publish("step", step)

    def interrupt(self) -> None:
        if self.busy:
            self.interrupted = True
        if self.proc is not None:
            _kill_tree(self.proc, grace=1)

    def set_mode(self, mode: str) -> None:
        self.mode = mode


def read_codex_status(account, thread_id: str | None) -> dict:
    """From Codex's own session file: the latest limits and how full the context is."""
    if not account or not thread_id:
        return {}
    from crewlib.agents import read_codex_rate
    out: dict = {}
    try:
        out["rate"] = read_codex_rate(account, thread_id)
    except Exception:
        out["rate"] = None
    home = account.profile_dir() or default_codex_home()
    try:  # Codex may be writing or rotating its files at this very moment
        files = sorted((home / "sessions").glob(f"**/rollout-*{thread_id}*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        files = []
    if files:
        latest = None
        try:
            with files[-1].open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' in line:
                        latest = line
        except OSError:
            latest = None
        if latest:
            try:
                info = ((json.loads(latest).get("payload") or {}).get("info") or {})
                out["window"] = int(info.get("model_context_window") or 0) or None
                last = info.get("last_token_usage") or {}
                out["used"] = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
            except (ValueError, TypeError, AttributeError):
                pass
    return out


# ================================================================== the manager


class ChatManager:
    def __init__(self, app_url: str = "", app_token: str = ""):
        self.db = ChatDB(crew_home() / "app.db")
        self.sessions: dict[str, Session] = {}
        self.app_url, self.app_token = app_url, app_token
        self._update_lock = threading.Lock()
        self.updating = False

    # ------------------------------------------------------------ accounts

    def pick_account(self, cfg, vendor: str, avoid: str | None = None, prefer: str | None = None):
        """The subscription for a chat: the one the owner chose for it (unless it is at its limit), else the
        default chosen in Settings, else the one with the most room (from the limits Claude and Codex report)."""
        accounts = [a for a in cfg.accounts_for(vendor) if a.name != avoid]
        if not accounts:
            return None
        preferred = settings_mod.load()["app"].get("chat_account") if vendor == "claude" else ""
        limits = usage_log.snapshot(1)["limits"]

        def load(a):
            lim = limits.get(a.name) or {}
            if lim.get("status") == "rejected" and (lim.get("five_reset") or 0) > time.time():
                return 2.0
            return max(float(lim.get("five_util") or 0), float(lim.get("week_util") or 0) * 0.8)

        if prefer:
            chosen = next((a for a in accounts if a.name == prefer and load(a) < 2.0), None)  # 2.0: at its limit
            if chosen:
                return chosen
        chosen = next((a for a in accounts if a.name == preferred and load(a) < 0.9), None)
        return chosen or min(accounts, key=load)

    def other_account(self, current, vendor: str):
        if current is None:
            return None
        cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
        other = self.pick_account(cfg, vendor, avoid=current.name)
        return other

    def failover(self, session: ClaudeSession) -> None:
        """Continue the same conversation on another Claude subscription."""
        def work():
            old = session.account
            new = self.other_account(old, "claude")
            if new is None or not session.session_id:
                return
            session.stop_process()
            copy_claude_session(session.session_id, old, new)
            session.publish("notice", {"text": f"Continuing on {new.name}.", "kind": "failover"})
            try:
                session.send(session.last_prompt, session.model, session.effort, session.mode, account=new)
            except Exception as exc:
                session.publish("notice", {"text": str(exc), "kind": "error"})
        threading.Thread(target=work, daemon=True).start()

    def fix_outdated(self, session: Session) -> None:
        """Update Claude Code, then ask again."""
        with self._update_lock:
            if self.updating:
                return
            self.updating = True

        def work():
            try:
                for s in self.sessions.values():
                    if s.engine == "claude":
                        s.stop_process()
                ok, message = claude_cli.update()
                session.publish("notice", {"text": message, "kind": "updated" if ok else "error"})
                if ok:
                    session.send(session.last_prompt, session.model, session.effort, session.mode)
            except Exception as exc:
                session.publish("notice", {"text": f"The update did not finish: {exc}", "kind": "error"})
            finally:
                self.updating = False
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def explain_error(raw: str) -> str:
        low = (raw or "").lower()
        if claude_cli.required_version(raw):
            return ("Claude Code on this computer is too old for this model. Open Settings → Check-up and press "
                    "Update Claude Code, then try again.")
        if "usage limit" in low or "rate limit" in low or "limit reached" in low or "limit will reset" in low:
            return ("This subscription has reached its usage limit for now. Crew switches to another subscription "
                    "when you have one; otherwise try again after it resets (see Usage).")
        if AUTH_RE.search(raw or "") or "not logged in" in low or "please run /login" in low:
            return "This subscription isn't signed in. Open Settings → Subscriptions and press Sign in."
        return "Something went wrong: " + clip(raw, 400)

    # ------------------------------------------------------------ what Claude Code offers

    def remember_init(self, msg: dict) -> None:
        keep = {k: msg.get(k) for k in ("skills", "slash_commands", "tools", "agents", "plugins", "mcp_servers",
                                        "model", "claude_code_version", "output_style", "permissionMode")}
        keep["at"] = now()
        try:
            atomic_write(crew_home() / "claude-init.json", json.dumps(keep))
        except OSError:
            pass

    @staticmethod
    def info() -> dict:
        try:
            return json.loads((crew_home() / "claude-init.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------ conversations

    def list(self, q: str = "", limit: int = 300) -> list[dict]:
        """The newest chats (pinned first); with `q`, every chat whose title contains those words, however old."""
        cols = "id,title,created,updated,engine,model,effort,mode,pinned,kind"
        words = " ".join(str(q or "").split())
        if words:
            like = "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return self.db.q(f"SELECT {cols} FROM chats WHERE title LIKE ? ESCAPE '\\' "
                             "ORDER BY pinned DESC, updated DESC LIMIT ?", (like, limit))
        return self.db.q(f"SELECT {cols} FROM chats ORDER BY pinned DESC, updated DESC LIMIT ?", (limit,))

    def create(self, engine: str | None = None, model: str | None = None, effort: str | None = None,
               mode: str | None = None, account: str | None = None) -> dict:
        for value in (engine, model, effort, mode, account):
            if value is not None and not isinstance(value, str):
                raise ValueError("Choose the product, model and effort from the lists.")
        app = settings_mod.load()["app"]
        engine = engine if engine in ENGINES else app.get("chat_engine", "claude")
        if engine == "codex":
            model = model if model is not None else app.get("codex_model", "")
            effort = effort or app.get("codex_effort", "auto")
        else:
            model = model or app["chat_model"]
            effort = effort or app["chat_effort"]
        mode = mode if mode in MODES else "auto"
        cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.db.x("INSERT INTO chats(id,title,created,updated,model,effort,engine,mode,account) VALUES(?,?,?,?,?,?,?,?,?)",
                  (cid, "New chat", now(), now(), model, effort, engine, mode, self._valid_account(engine, account)))
        return self.get(cid)

    @staticmethod
    def _valid_account(engine: str, name: str | None) -> str:
        """The owner's choice of subscription for a chat: one of theirs for that product, or '' (automatic)."""
        if not name:
            return ""
        cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
        if not any(a.name == name for a in cfg.accounts_for(engine)):
            raise ValueError(f"There is no {ENGINES.get(engine, engine)} subscription called {name}.")
        return name

    def get(self, cid: str) -> dict | None:
        rows = self.db.q("SELECT * FROM chats WHERE id=?", (cid,))
        if not rows:
            return None
        chat = rows[0]
        msgs = self.db.q("SELECT id,role,text,ts,meta FROM messages WHERE chat_id=? ORDER BY id", (cid,))
        for m in msgs:
            m["meta"] = json.loads(m["meta"] or "{}")
        s = self.sessions.get(cid)
        live = s.turn if s and s.busy else None
        chat.update(messages=msgs, busy=bool(live), partial=live.text if live else "",
                    live=live.meta() if live else None, context={
                        "used": (s.context.get("used") if s else chat.get("context_tokens")) or 0,
                        "window": (s.context.get("window") if s else chat.get("context_window")) or 0})
        return chat

    def session(self, chat: dict) -> Session:
        s = self.sessions.get(chat["id"])
        if s is None or s.engine != (chat.get("engine") or "claude"):
            if s is not None:
                s.stop_process()
            s = (CodexSession if chat.get("engine") == "codex" else ClaudeSession)(self, chat)
            self.sessions[chat["id"]] = s
        return s

    def send(self, cid: str, text: str, model: str | None = None, effort: str | None = None,
             attachments: list[str] | None = None, mode: str | None = None, engine: str | None = None,
             account: str | None = None) -> dict:
        chat = self.get(cid)
        if chat is None:
            raise KeyError(cid)
        if text is not None and not isinstance(text, str):
            raise ValueError("Type or say something first.")
        for value in (model, effort, mode, engine, account):
            if value is not None and not isinstance(value, str):
                raise ValueError("Choose the product, model and effort from the lists.")
        text = (text or "").strip()
        attachments = [a for a in (attachments if isinstance(attachments, list) else [])
                       if isinstance(a, str) and re.fullmatch(r"attachments/[\w.-]+", a)]
        if not text and attachments:
            text = "Please look at what I attached."
        if not text:
            raise ValueError("Type or say something first.")
        engine = engine if engine in ENGINES else (chat.get("engine") or "claude")
        if engine != (chat.get("engine") or "claude") and chat["messages"]:
            raise ValueError("This conversation is with " + ENGINES[chat.get("engine") or "claude"] +
                             ". Start a new conversation to use " + ENGINES[engine] + ".")
        model = chat["model"] if model is None else model
        if engine == "claude" and not model:  # Claude always runs a named model: the chat's, else the default
            model = chat.get("model") or settings_mod.load()["app"].get("chat_model") or "claude-opus-5-5"
        effort = effort or chat.get("effort") or "auto"
        effort = LEGACY_EFFORTS.get(effort, effort) if engine == "codex" else effort
        mode = mode if mode in MODES else (chat.get("mode") or "auto")
        if effort not in EFFORTS[engine]:
            raise ValueError(f"Unknown effort level for {ENGINES[engine]}: {effort}.")
        other = other_product(engine, model)
        if other:
            raise ValueError(f"{model} is a {ENGINES[other]} model, and this conversation is with {ENGINES[engine]}. "
                             f"Choose one of {ENGINES[engine]}'s models, or start a new chat with {ENGINES[other]}.")
        if engine == "claude" or model:
            cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
            cfg.models.check(model)  # a banned or unknown model is refused before anything is recorded
        chat["engine"] = engine
        if account is not None:
            chat["account"] = self._valid_account(engine, account)
            self.db.x("UPDATE chats SET account=? WHERE id=?", (chat["account"], cid))
        session = self.session(chat)
        if session.busy:
            raise ValueError("Still answering the last message. Press stop first, or wait a moment.")
        session.chosen = chat.get("account") or ""
        self.db.add_message(cid, "user", text, {"attachments": attachments or [], "mode": mode})
        words = " ".join(text.split())
        untitled = chat["title"] in ("New chat", "New conversation")
        title = chat["title"] if not untitled or text.startswith("/") else \
            (words if len(words) <= 60 else words[:59].rstrip() + "…")
        self.db.x("UPDATE chats SET updated=?, title=?, model=?, effort=?, mode=?, engine=? WHERE id=?",
                  (now(), title, model, effort, mode, engine, cid))
        prompt = text
        if attachments:
            prompt += "\n\n(The owner attached: " + ", ".join(attachments) + " — in this folder. Open them to answer.)"
        try:
            session.send(prompt, model, effort, mode)
        except Exception as exc:  # noqa: BLE001 — could not start: say so in the conversation, never leave it unanswered
            session.busy = False
            mid = self.db.add_message(cid, "assistant", str(exc), {"error": True})
            hub.publish(session.topic, "done", {"id": mid, "text": str(exc), "meta": {"error": True}})
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def set_mode(self, cid: str, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError("Unknown mode.")
        self.db.x("UPDATE chats SET mode=? WHERE id=?", (mode, cid))
        s = self.sessions.get(cid)
        if s:
            s.set_mode(mode)
        return {"mode": mode}

    def approve_plan(self, cid: str, note: str = "") -> dict:
        """Leave plan mode and carry out the plan."""
        note = note if isinstance(note, str) else ""
        self.set_mode(cid, "auto")
        text = "The plan is approved. Carry it out now." + (f" Also: {note.strip()}" if note.strip() else "")
        return self.send(cid, text, mode="auto")

    def rename(self, cid: str, title: str) -> None:
        if not isinstance(title, str):
            raise ValueError("Give the chat a name.")
        title = " ".join(title.split())[:120]
        if title:
            self.db.x("UPDATE chats SET title=? WHERE id=?", (title, cid))

    def pin(self, cid: str, pinned: bool) -> None:
        self.db.x("UPDATE chats SET pinned=? WHERE id=?", (1 if pinned else 0, cid))

    def stop(self, cid: str) -> None:
        s = self.sessions.get(cid)
        if s:
            s.interrupt()

    def delete(self, cid: str) -> None:
        s = self.sessions.pop(cid, None)
        if s:
            s.stop_process()
        # The chat first: from then on no answer can be written for it (add_message), and what was written
        # before goes with the messages.
        self.db.x("DELETE FROM chats WHERE id=?", (cid,))
        self.db.x("DELETE FROM messages WHERE chat_id=?", (cid,))

    def workspace(self, cid: str) -> Path | None:
        if not self.db.q("SELECT id FROM chats WHERE id=?", (cid,)):
            return None
        path = crew_home() / "chats" / cid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_attachment(self, cid: str, name: str, data: bytes) -> dict:
        root = self.workspace(cid)
        if root is None:
            raise KeyError(cid)
        stem, dot, ext = (name or "file").rpartition(".")
        safe = re.sub(r"[^\w.-]+", "-", (stem if dot else ext) or "file").strip("-.")[:60] or "file"
        if safe.split(".")[0].lower() in WINDOWS_RESERVED:  # CON, NUL, COM1 … are devices on Windows, not files
            safe = "file-" + safe
        ext = re.sub(r"[^\w]", "", ext)[:8] if dot else ""
        folder = root / "attachments"
        folder.mkdir(exist_ok=True)
        target = folder / (safe + ("." + ext if ext else ""))
        n = 1
        while target.exists():
            target = folder / (f"{safe}-{n}" + ("." + ext if ext else ""))
            n += 1
        target.write_bytes(data)
        rel = target.relative_to(root).as_posix()
        return {"path": rel, "name": target.name, "url": f"/files/chat/{cid}/{rel}", "size": len(data)}

    def files(self, cid: str) -> list[dict]:
        root = self.workspace(cid)
        if root is None:
            return []
        out = [{"name": rel, "size": st.st_size, "url": f"/files/chat/{cid}/{rel}", "modified": st.st_mtime,
                "kind": file_kind(rel)} for rel, st in workspace_files(root)]
        return sorted(out, key=lambda f: f["name"])[:200]

    def as_project_request(self, cid: str) -> str:
        chat = self.get(cid) or {}
        parts = []
        for m in chat.get("messages", [])[-12:]:
            who = "Owner" if m["role"] == "user" else "Assistant"
            parts.append(f"{who}: {clip(m['text'], 1500)}")
        return ("Build what the owner asked for in this conversation (the latest request matters most):\n\n"
                + "\n\n".join(parts))

    def shutdown(self) -> None:
        for s in self.sessions.values():
            s.stop_process()
