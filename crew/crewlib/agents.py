"""Drive the real coding agents: Claude Code and Codex, headless.

A *seat* runner keeps one conversation alive across many turns:
  * Claude Code: one long-lived `claude -p --input-format stream-json` process;
    each orchestrator message is a new user turn on stdin.
  * Codex: one `codex exec --json` per turn, continued with `codex exec resume`.

Both report the same events to the orchestrator's queue: init, activity,
rate, result, exit. One-shot runs (refiner, reviewer, CEO) use run_once().

Nothing here decides anything; it only runs agents and reports what happened.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import usage as usage_log
from .config import Account
from .util import Redactor, clip, dumps, now

CREW_ROOT = Path(__file__).resolve().parent.parent  # directory that contains crewlib/
_GENERATION = itertools.count(1)  # every runner gets a unique id so stale events can be ignored
SKILLS_PACK = CREW_ROOT / "skills_pack"
LIMIT_RE = re.compile(
    r"(usage limit|rate limit|limit reached|hit your (?:\w+ )?limit|reached your \w+ limit|quota|"
    r"usage_limit_reached|rate_limit_reached|resets? (?:at|in)|out of credits|429)", re.I)

AUTH_RE = re.compile(
    r"(401 unauthori[sz]ed|unauthori[sz]ed|invalid api key|please run /login|not logged in|missing bearer|"
    r"token has expired|login required|authentication_error|invalid x-api-key)", re.I)

# Variables that belong to *this* process's own Claude/Codex session (e.g. when
# Crew itself runs inside Claude Code). Children must not inherit them.
_KEEP_CLAUDE_VARS = {
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_HOST_CREDS_FILE", "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_PROXY_RESOLVES_HOSTS", "CLAUDE_CODE_GZIP_REQUEST_BODIES", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
}


def child_env(extra: dict[str, str] | None = None, drop_api_key: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key == "CLAUDECODE" or key == "CLAUDE_CONFIG_DIR" or key == "CODEX_HOME":
            env.pop(key)
        elif (key.startswith("CLAUDE_") or key.startswith("CLAUDE_CODE_")) and key not in _KEEP_CLAUDE_VARS:
            env.pop(key)
    if drop_api_key:
        # A subscription seat must not silently switch to pay-as-you-go billing.
        env.pop("ANTHROPIC_API_KEY", None)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        env["IS_SANDBOX"] = "1"  # Claude Code refuses auto-approval as root unless IS_SANDBOX is exactly "1"
    pypath = str(CREW_ROOT)
    env["PYTHONPATH"] = pypath + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra or {})
    return env


def default_claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def which(name: str) -> str | None:
    override = os.environ.get(f"CREW_{name.upper()}_BIN")
    if override:
        return override
    if name == "claude":
        from .claude_cli import best
        newest, _ = best()
        if newest:
            return newest
    found = shutil.which(name)
    if os.name == "nt":
        found = _windows_program(name, found)
    return found


def _windows_program(name: str, found: str | None) -> str | None:
    """Find the real program on Windows: installers may not have refreshed PATH yet, and npm's
    codex.cmd wrapper would pass arguments through cmd.exe (which mangles quotes and newlines)."""
    home = Path.home()
    if name == "claude" and not found:
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        for candidate in (home / ".local" / "bin" / "claude.exe", local / "Programs" / "claude" / "claude.exe"):
            if candidate.is_file():
                return str(candidate)
    if name == "codex":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        shim = Path(found) if found else appdata / "npm" / "codex.cmd"
        scope = shim.parent / "node_modules" / "@openai"
        if scope.is_dir():
            exes = sorted(scope.rglob("codex.exe")) or sorted(p for p in scope.rglob("codex-*.exe"))
            if exes:
                return str(exes[-1])
        if shim.is_file():
            return str(shim)
    return found


@dataclass
class Event:
    kind: str  # init | activity | rate | result | exit | repeat
    seat: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=now)


@dataclass
class RunResult:
    text: str = ""
    structured: dict | None = None
    is_error: bool = False
    limit_hit: bool = False
    resets_at: int | None = None
    session_id: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    rate: dict | None = None
    timed_out: bool = False
    auth_error: bool = False
    stderr: str = ""


def _usage_tokens(usage: dict | None) -> int:
    """Fresh tokens of a turn (input + cache writes + output). Cache reads are excluded: they are cheap."""
    if not usage:
        return 0
    return int((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0))


def _kill_tree(proc: subprocess.Popen, grace: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _popen(cmd: list[str], cwd: Path, env: dict, stdin=subprocess.PIPE) -> subprocess.Popen:
    kwargs = dict(cwd=str(cwd), env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, encoding="utf-8", errors="replace", bufsize=1)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


# ======================================================================= Claude


@dataclass
class ClaudeSetup:
    """Everything a Claude Code process needs besides its prompt."""

    model: str
    effort: str
    work_model: str  # every sub-agent and background call is forced onto this model
    permission_mode: str
    run_dir: Path
    extra_env: dict[str, str]


def _claude_common_args(setup: ClaudeSetup, seat: str, role: str, system_file: Path | None,
                        task_id: int | None = None, hooks: bool = True) -> tuple[list[str], dict[str, str]]:
    files = setup.run_dir / "agents" / seat
    files.mkdir(parents=True, exist_ok=True)
    py = Path(sys.executable).as_posix()  # forward slashes: safe in both cmd.exe and the bash that runs hooks
    team_env = {"CREW_DB": str(setup.run_dir / "team.db"), "CREW_SEAT": seat, "CREW_ROLE": role,
                "PYTHONPATH": str(CREW_ROOT)}
    if task_id is not None:
        team_env["CREW_TASK"] = str(task_id)
    mcp = {"mcpServers": {"crew_team": {"type": "stdio", "command": py, "args": ["-m", "crewlib.mcp_server"],
                                         "env": team_env}}}
    if os.environ.get("CREW_APP_URL") and os.environ.get("CREW_APP_TOKEN"):
        # Started from the Crew app: agents may also use its browser (and the owner's phone), visibly.
        mcp["mcpServers"]["crew_devices"] = {
            "type": "stdio", "command": py, "args": ["-m", "crewapp.devices_mcp"],
            "env": {"CREW_APP_URL": os.environ["CREW_APP_URL"], "CREW_APP_TOKEN": os.environ["CREW_APP_TOKEN"],
                    "PYTHONPATH": str(CREW_ROOT)}}
    from .connections import mcp_servers as owner_connections
    for name, srv in owner_connections().items():  # the owner's own connections (Hunter, Notion, …)
        mcp["mcpServers"].setdefault(name, srv)
    (files / "mcp.json").write_text(json.dumps(mcp, indent=1), encoding="utf-8")
    hook_cmd = f'"{py}" -m crewlib.hook'
    settings: dict = {"model": setup.model}
    if hooks:  # long-lived seats only: urgent chat reaches them mid-task
        settings["hooks"] = {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": hook_cmd, "timeout": 15}]}]}
    (files / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    subagents = {
        "researcher": {
            "description": "Reads docs, code and the web to answer a focused question; returns a short, sourced answer.",
            "prompt": "Research the question you are given. Read only what you need. Answer in under 200 words with "
                      "file paths or URLs as sources. Do not edit files.",
            "model": setup.work_model,
        },
        "tester": {
            "description": "Runs the project's tests/build and returns a concise pass/fail summary with the key failures.",
            "prompt": "Run the commands you are given. Write full output to a temporary log file and report only: "
                      "pass/fail counts, each failing test with its one-line cause, and the log path.",
            "model": setup.work_model,
        },
        "critic": {
            "description": "Reviews a change with fresh eyes before submission; lists concrete defects only.",
            "prompt": "Review the change you are pointed at (git diff) against the task spec. List concrete defects "
                      "(file, problem, fix). No style nits. Do not edit files.",
            "model": setup.work_model,
        },
    }
    (files / "agents.json").write_text(json.dumps(subagents, indent=1), encoding="utf-8")
    args = ["--model", setup.model]
    if setup.effort and setup.effort != "auto":
        args += ["--effort", setup.effort]
    args += ["--mcp-config", str(files / "mcp.json"), "--strict-mcp-config",
            "--settings", str(files / "settings.json"),
            "--agents", str(files / "agents.json"),
            "--permission-mode", setup.permission_mode]
    if system_file is not None:
        args += ["--append-system-prompt-file", str(system_file)]
    active = setup.run_dir.parent.parent / "skills-active"  # built by the app: only switched-on skills
    packs = (active,) if (active / ".claude-plugin" / "plugin.json").is_file() else \
        (SKILLS_PACK, setup.run_dir.parent.parent / "skills")
    for pack in packs:
        if (pack / ".claude-plugin" / "plugin.json").is_file():
            args += ["--plugin-dir", str(pack)]
    env = {
        **setup.extra_env, **team_env,
        "CLAUDE_CODE_SUBAGENT_MODEL": setup.work_model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": setup.work_model,  # no Haiku, even for background work
        "DISABLE_AUTOUPDATER": "1",
    }
    return args, env


class ClaudeSeat:
    """A long-lived Claude Code conversation driven over stream-json stdin/stdout."""

    vendor = "claude"

    def __init__(self, seat: str, role: str, account: Account, workdir: Path, setup: ClaudeSetup,
                 system_prompt: str, events: "queue.Queue[Event]", redact: Redactor):
        self.seat, self.role, self.account, self.workdir = seat, role, account, workdir
        self.setup, self.events, self.redact = setup, events, redact
        self.system_file = setup.run_dir / "agents" / seat / "system.md"
        self.system_file.parent.mkdir(parents=True, exist_ok=True)
        self.system_file.write_text(system_prompt, encoding="utf-8")
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.busy = False
        self._lock = threading.Lock()
        self._recent_tools: list[str] = []
        self._helpers: set[str] = set()  # sub-agents (Task/Agent tool) still at work
        self.log_path = setup.run_dir / "logs" / f"{seat}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.gen = next(_GENERATION)

    # -- lifecycle

    def start(self, first_message: str | None = None, resume: str | None = None) -> None:
        exe = which("claude")
        if not exe:
            raise RuntimeError("Claude Code is not installed (the `claude` command was not found)")
        common, env = _claude_common_args(self.setup, self.seat, self.role, self.system_file)
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", *common]
        if resume:
            cmd += ["--resume", resume]
        prof = self.account.profile_dir()
        extra = dict(env)
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            extra["CLAUDE_CONFIG_DIR"] = str(prof)
        self.proc = _popen(cmd, self.workdir, child_env(extra))
        self.session_id = resume or self.session_id
        threading.Thread(target=self._read_stdout, daemon=True, name=f"{self.seat}-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"{self.seat}-err").start()
        if first_message:
            self.send(first_message)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"{self.seat} is not running")
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
               "parent_tool_use_id": None, "session_id": self.session_id or ""}
        with self._lock:
            self.busy = True
            assert self.proc and self.proc.stdin
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def interrupt(self) -> None:
        if self.alive():
            req = {"type": "control_request", "request_id": f"int-{int(now() * 1000)}",
                   "request": {"subtype": "interrupt"}}
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def stop(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            _kill_tree(self.proc, grace=3)

    # -- output

    def _log(self, line: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(self.redact(line.rstrip("\n")) + "\n")
        except OSError:
            pass

    def _emit(self, kind: str, **data) -> None:
        self.events.put(Event(kind, self.seat, {**data, "gen": self.gen}))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        buf = []
        for line in self.proc.stderr:
            buf.append(line)
            if len(buf) > 200:
                buf = buf[-100:]
        self._stderr_tail = "".join(buf[-30:])

    def _read_stdout(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        for line in proc.stdout:
            if not line.strip():
                continue
            self._log(line)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._handle(msg)
        code = proc.wait()
        self.busy = False
        time.sleep(0.05)
        self._emit("exit", code=code, stderr=clip(getattr(self, "_stderr_tail", ""), 2000))

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            self.session_id = msg.get("session_id") or self.session_id
            servers = {s.get("name"): s.get("status") for s in msg.get("mcp_servers") or []}
            self._emit("init", session_id=self.session_id, model=msg.get("model"), mcp=servers)
        elif kind == "assistant":
            parent = msg.get("parent_tool_use_id")
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name in ("Task", "Agent") and not parent and block.get("id"):
                        inp = block.get("input") or {}
                        self._helpers.add(block["id"])
                        self._emit("helper", id=block["id"], state="start", what=clip(inp.get("description") or "", 80),
                                   type=inp.get("subagent_type") or "helper")
                    elif parent in self._helpers:
                        self._emit("helper", id=parent, state="step", what=name)
                    sig = name + ":" + hashlib.sha1(dumps(block.get("input")).encode()).hexdigest()[:10]
                    self._recent_tools = (self._recent_tools + [sig])[-8:]
                    if name.startswith("mcp__crew_team__"):
                        label = name.replace("mcp__crew_team__", "")
                    else:
                        inp = block.get("input") or {}
                        label = f"{name}: {clip(str(inp.get('command') or inp.get('file_path') or inp.get('pattern') or ''), 80)}"
                    self._emit("activity", label=label)
                    if self._recent_tools.count(sig) >= 4 and not name.startswith("mcp__crew_team__"):
                        self._emit("repeat", label=label)
                        self._recent_tools.clear()
                elif block.get("type") == "text" and block.get("text"):
                    self._emit("activity", label="writing", text=clip(block["text"], 500))
        elif kind == "user":
            content = (msg.get("message") or {}).get("content")
            if not msg.get("parent_tool_use_id") and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("tool_use_id") in self._helpers:
                        self._helpers.discard(block["tool_use_id"])
                        self._emit("helper", id=block["tool_use_id"], state="error" if block.get("is_error") else "done")
            self._emit("activity", label="tool result")
        elif kind == "rate_limit_event":
            usage_log.record_rate(self.account.name, msg.get("rate_limit_info") or {})
            self._emit("rate", info=msg.get("rate_limit_info") or {})
        elif kind == "result":
            text = msg.get("result") or ""
            is_error = bool(msg.get("is_error")) or msg.get("subtype") not in (None, "success")
            usage = msg.get("usage") or {}
            usage_log.record_tokens(self.account.name, usage)
            self.busy = False
            self._emit("result", text=clip(text, 4000), is_error=is_error, subtype=msg.get("subtype"),
                       limit_hit=bool(is_error and LIMIT_RE.search(text or "")),
                       auth_error=bool(is_error and AUTH_RE.search(text or "")),
                       tokens=_usage_tokens(usage), output_tokens=int(usage.get("output_tokens") or 0),
                       cost=float(msg.get("total_cost_usd") or 0), duration_ms=msg.get("duration_ms"),
                       session_id=msg.get("session_id") or self.session_id)


def copy_claude_session(session_id: str, src: Account, dst: Account) -> bool:
    """Make a conversation resumable under another Claude account (same machine, same folder)."""
    src_home = src.profile_dir() or default_claude_home()
    dst_home = dst.profile_dir() or default_claude_home()
    if src_home == dst_home:
        return True
    matches = list((src_home / "projects").glob(f"*/{session_id}.jsonl"))
    if not matches:
        return False
    source = matches[0]
    target_dir = dst_home / "projects" / source.parent.name
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_dir / source.name)
    sidecar = source.parent / session_id  # sub-agent transcripts, if any
    if sidecar.is_dir():
        shutil.copytree(sidecar, target_dir / session_id, dirs_exist_ok=True)
    return True


# ======================================================================== Codex


@dataclass
class CodexSetup:
    model: str  # "" = Codex default
    effort: str
    run_dir: Path
    extra_env: dict[str, str]
    bypass_sandbox: bool = False


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def codex_effort(effort: str | None) -> str | None:
    """OpenAI's own effort names; a Claude level maps to the nearest one. None: Codex decides (auto)."""
    if not effort or effort == "auto":
        return None
    if effort in CODEX_EFFORTS:
        return effort
    return {"max": "xhigh"}.get(effort, effort if effort in CODEX_EFFORTS else "high")


def _codex_config_args(setup: CodexSetup, seat: str, role: str, task_id: int | None, read_only: bool) -> list[str]:
    team_env = {"CREW_DB": str(setup.run_dir / "team.db"), "CREW_SEAT": seat, "CREW_ROLE": role,
                "PYTHONPATH": str(CREW_ROOT)}
    if task_id is not None:
        team_env["CREW_TASK"] = str(task_id)
    env_table = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in team_env.items()) + "}"
    effort = codex_effort(setup.effort)
    args = [
        "-c", f"mcp_servers.crew_team.command={_toml_str(sys.executable)}",
        "-c", 'mcp_servers.crew_team.args=["-m", "crewlib.mcp_server"]',
        "-c", f"mcp_servers.crew_team.env={env_table}",
        "-c", "mcp_servers.crew_team.startup_timeout_sec=30",
        "-c", 'approval_policy="never"',
    ]
    if effort:
        args += ["-c", f"model_reasoning_effort={_toml_str(effort)}"]
    if setup.bypass_sandbox and not read_only:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args += ["-c", f'sandbox_mode="{"read-only" if read_only else "workspace-write"}"',
                 "-c", "sandbox_workspace_write.network_access=true"]
    if setup.model:
        args += ["-m", setup.model]
    return args


class CodexSeat:
    """A Codex conversation: one `codex exec` process per turn, resumed by thread id."""

    vendor = "codex"

    def __init__(self, seat: str, role: str, account: Account, workdir: Path, setup: CodexSetup,
                 system_prompt: str, events: "queue.Queue[Event]", redact: Redactor):
        self.seat, self.role, self.account, self.workdir = seat, role, account, workdir
        self.setup, self.events, self.redact = setup, events, redact
        self.system_prompt = system_prompt
        self.session_id: str | None = None
        self.proc: subprocess.Popen | None = None
        self.busy = False
        self._pending: list[str] = []
        self._started = False
        self._recent_tools: list[str] = []
        self.log_path = setup.run_dir / "logs" / f"{seat}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.gen = next(_GENERATION)

    def start(self, first_message: str | None = None, resume: str | None = None) -> None:
        self._started = True
        self.session_id = resume or self.session_id
        self._emit("init", session_id=self.session_id, model=self.setup.model or "codex-default", mcp={})
        if first_message:
            self.send(first_message)

    def alive(self) -> bool:
        return self._started

    def send(self, text: str) -> None:
        if self.busy:
            self._pending.append(text)
            return
        self.busy = True
        threading.Thread(target=self._turn, args=(text,), daemon=True, name=f"{self.seat}-turn").start()

    def interrupt(self) -> None:
        if self.proc is not None:
            _kill_tree(self.proc, grace=2)

    def stop(self) -> None:
        self._started = False
        self._pending.clear()
        if self.proc is not None:
            _kill_tree(self.proc, grace=2)

    def _emit(self, kind: str, **data) -> None:
        self.events.put(Event(kind, self.seat, {**data, "gen": self.gen}))

    def _turn(self, text: str) -> None:
        exe = which("codex")
        if not exe:
            self.busy = False
            self._emit("exit", code=127, stderr="Codex is not installed (the `codex` command was not found)")
            return
        config = _codex_config_args(self.setup, self.seat, self.role, None, read_only=False)
        if self.session_id:
            cmd = [exe, "exec", "resume", "--json", "--skip-git-repo-check", *config, self.session_id, "-"]
            prompt = text
        else:
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", *config, "-C", str(self.workdir), "-"]
            prompt = f"{self.system_prompt}\n\n---\n\n{text}"
        env = dict(self.setup.extra_env)
        prof = self.account.profile_dir()
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            env["CODEX_HOME"] = str(prof)
        res = _drive_codex(cmd, prompt, self.workdir, child_env(env), self.log_path, self.redact,
                           on_event=self._on_item, timeout=None)
        self.proc = None
        if res.session_id:
            self.session_id = res.session_id
        rate = read_codex_rate(self.account, self.session_id) if self.session_id else None
        if rate:
            usage_log.record_rate(self.account.name, rate)
            self._emit("rate", info=rate)
        usage_log.record_tokens(self.account.name, {"input_tokens": res.tokens})
        self.busy = False
        self._emit("result", text=clip(res.text, 4000), is_error=res.is_error, subtype="error" if res.is_error else "success",
                   limit_hit=res.limit_hit, auth_error=res.auth_error, tokens=res.tokens, output_tokens=0, cost=0.0,
                   duration_ms=int(res.duration_s * 1000), session_id=self.session_id)
        if self._pending and self._started:
            nxt = "\n\n".join(self._pending)
            self._pending.clear()
            self.send(nxt)

    def _on_item(self, proc: subprocess.Popen, ev: dict) -> None:
        self.proc = proc
        if ev.get("type") == "thread.started" and not self.session_id:
            self.session_id = ev.get("thread_id")
            self._emit("init", session_id=self.session_id, model=self.setup.model or "codex-default", mcp={})
        item = ev.get("item") or {}
        if ev.get("type") in ("item.started", "item.completed") and item:
            itype = item.get("type", "")
            if itype == "command_execution":
                label = f"Bash: {clip(item.get('command', ''), 80)}"
            elif itype == "mcp_tool_call":
                label = str(item.get("tool") or item.get("name") or "team tool")
            elif itype == "file_change":
                label = "Edit: " + ", ".join(c.get("path", "") for c in item.get("changes") or [])[:80]
            else:
                label = itype or "working"
            self._emit("activity", label=label)
            if ev["type"] == "item.started" and itype == "command_execution":
                sig = hashlib.sha1(str(item.get("command")).encode()).hexdigest()[:10]
                self._recent_tools = (self._recent_tools + [sig])[-8:]
                if self._recent_tools.count(sig) >= 4:
                    self._emit("repeat", label=label)
                    self._recent_tools.clear()


def _drive_codex(cmd: list[str], prompt: str, cwd: Path, env: dict, log_path: Path, redact: Redactor,
                 on_event=None, timeout: float | None = None) -> RunResult:
    res = RunResult()
    start = now()
    proc = _popen(cmd, cwd, env)
    try:
        assert proc.stdin
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    timer = None
    if timeout:
        def _expire():
            res.timed_out = True
            _kill_tree(proc)
        timer = threading.Timer(timeout, _expire)
        timer.start()
    err_lines: list[str] = []
    threading.Thread(target=lambda: err_lines.extend(proc.stderr or []), daemon=True).start()
    messages: list[str] = []
    last_error = ""
    completed = False
    assert proc.stdout
    with log_path.open("a", encoding="utf-8") as log:
        for line in proc.stdout:
            if not line.strip():
                continue
            log.write(redact(line.rstrip("\n")) + "\n")
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            etype = ev.get("type")
            item = ev.get("item") or {}
            if on_event:
                on_event(proc, ev)
            if etype == "thread.started":
                res.session_id = ev.get("thread_id")
            elif etype == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text") or "")
            elif etype == "item.completed" and item.get("type") == "error":
                last_error = item.get("message") or last_error  # informational, e.g. a transport fallback
            elif etype == "turn.completed":
                completed = True
                usage = ev.get("usage") or {}
                res.tokens += int((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))
            elif etype == "error":
                # Often transient ("Reconnecting... 2/5"); only turn.failed or a failed exit is final.
                last_error = ev.get("message") or last_error
            elif etype == "turn.failed":
                err = ev.get("error") or {}
                text = (err.get("message") if isinstance(err, dict) else None) or last_error or "Codex reported an error"
                res.is_error = True
                messages.append(text)
    code = proc.wait()
    if timer:
        timer.cancel()
    res.duration_s = now() - start
    res.stderr = clip("".join(err_lines[-40:]), 3000)
    if code != 0 and not completed and not res.is_error:
        res.is_error = True
        messages.append(last_error or res.stderr or f"Codex exited with code {code}")
    res.text = messages[-1] if messages else (last_error or res.stderr)
    if res.is_error:
        final = res.text or ""
        res.limit_hit = bool(LIMIT_RE.search(final)) and "Reconnecting" not in final
        res.auth_error = bool(AUTH_RE.search(final))
    return res


def read_codex_rate(account: Account, thread_id: str) -> dict | None:
    """Codex keeps rate-limit snapshots in its session files; return the latest as a Claude-style info dict."""
    home = account.profile_dir() or default_codex_home()
    files = sorted((home / "sessions").glob(f"**/rollout-*{thread_id}*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    latest = None
    try:
        with files[-1].open(encoding="utf-8") as fh:
            for line in fh:
                if '"rate_limits"' in line:
                    latest = line
    except OSError:
        return None
    if not latest:
        return None
    try:
        payload = json.loads(latest).get("payload") or {}
    except ValueError:
        return None
    limits = payload.get("rate_limits") or {}

    def window(w: dict | None):
        if not w:
            return None
        resets = w.get("resets_at")
        if resets is None and w.get("resets_in_seconds") is not None:
            resets = int(now() + float(w["resets_in_seconds"]))
        return {"utilization": float(w.get("used_percent") or 0) / 100.0, "resetsAt": int(resets or 0)}

    five, week = window(limits.get("primary")), window(limits.get("secondary"))
    worst = max([w for w in (five, week) if w], key=lambda w: w["utilization"], default=None)
    return {
        "status": "rejected" if worst and worst["utilization"] >= 1.0 else "allowed",
        "utilization": worst["utilization"] if worst else None,
        "resetsAt": worst["resetsAt"] if worst else None,
        "unifiedWindows": {k: v for k, v in (("five_hour", five), ("seven_day", week)) if v},
    }


# ===================================================================== one-shot


def run_once_claude(prompt: str, *, seat: str, role: str, account: Account, workdir: Path, setup: ClaudeSetup,
                    redact: Redactor, task_id: int | None = None, json_schema: dict | None = None,
                    read_only: bool = False, timeout: float = 1800, with_team_tools: bool = True) -> RunResult:
    """A fresh, single-purpose Claude Code session (refiner, reviewer, CEO): no shared history by design."""
    exe = which("claude")
    res = RunResult()
    if not exe:
        res.is_error, res.text = True, "Claude Code is not installed"
        return res
    common, env = _claude_common_args(setup, seat, role, None, task_id=task_id, hooks=False)
    if not with_team_tools:
        i = common.index("--mcp-config")
        del common[i:i + 3]  # --mcp-config <file> --strict-mcp-config
        common += ["--strict-mcp-config"]
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence", *common]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if read_only:
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    prof = account.profile_dir()
    if prof is not None:
        prof.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(prof)
    log_path = setup.run_dir / "logs" / f"{seat}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = now()
    proc = _popen(cmd, workdir, child_env(env))
    try:
        assert proc.stdin
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass

    def _expire():
        res.timed_out = True
        _kill_tree(proc)

    timer = threading.Timer(timeout, _expire)
    timer.start()
    err_lines: list[str] = []
    threading.Thread(target=lambda: err_lines.extend(proc.stderr or []), daemon=True).start()
    assert proc.stdout
    with log_path.open("a", encoding="utf-8") as log:
        for line in proc.stdout:
            if not line.strip():
                continue
            log.write(redact(line.rstrip("\n")) + "\n")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "system" and msg.get("subtype") == "init":
                res.session_id = msg.get("session_id")
            elif msg.get("type") == "rate_limit_event":
                res.rate = msg.get("rate_limit_info") or {}
                usage_log.record_rate(account.name, res.rate)
                if res.rate.get("status") == "rejected":
                    res.limit_hit, res.resets_at = True, res.rate.get("resetsAt")
            elif msg.get("type") == "result":
                res.text = msg.get("result") or ""
                res.structured = msg.get("structured_output")
                res.is_error = bool(msg.get("is_error")) or msg.get("subtype") not in (None, "success")
                res.tokens = _usage_tokens(msg.get("usage"))
                usage_log.record_tokens(account.name, msg.get("usage"))
                res.cost_usd = float(msg.get("total_cost_usd") or 0)
                if res.is_error and LIMIT_RE.search(res.text):
                    res.limit_hit = True
                if res.is_error and AUTH_RE.search(res.text):
                    res.auth_error = True
    proc.wait()
    timer.cancel()
    res.duration_s = now() - start
    res.stderr = clip("".join(err_lines[-40:]), 3000)
    if res.structured is None and json_schema is not None and res.text:
        res.structured = _extract_json(res.text)
    if not res.text and res.stderr:
        res.is_error, res.text = True, res.stderr
    return res


def run_once_codex(prompt: str, *, seat: str, role: str, account: Account, workdir: Path, setup: CodexSetup,
                   redact: Redactor, task_id: int | None = None, read_only: bool = True,
                   timeout: float = 1800) -> RunResult:
    exe = which("codex")
    if not exe:
        return RunResult(is_error=True, text="Codex is not installed")
    config = _codex_config_args(setup, seat, role, task_id, read_only=read_only)
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", *config, "-C", str(workdir), "-"]
    env = dict(setup.extra_env)
    prof = account.profile_dir()
    if prof is not None:
        prof.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(prof)
    log_path = setup.run_dir / "logs" / f"{seat}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return _drive_codex(cmd, prompt, workdir, child_env(env), log_path, redact, timeout=timeout)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
