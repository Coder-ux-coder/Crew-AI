#!/usr/bin/env python3
"""A scripted stand-in for Claude Code (`claude -p ...`) and Codex (`codex exec ...`).

It speaks the same wire formats (stream-json / exec JSONL), launches the team
tools exactly like the real CLIs do (from --mcp-config or -c mcp_servers.*),
keeps resumable session files in the account's profile folder, and follows a
simple "brain" so the orchestrator can be tested end to end. Faults are
injected through CREW_FAKE_SCENARIO (JSON):

  hang_on_task: [ids]        first time given this task: go silent forever
  crash_on_task: [ids]       first time: exit(1)
  limit_on_task: [ids]       first time: report the account's usage limit
  reject_task: [ids]         reviewer requests changes the first time
  outside_scope_task: [ids]  owner also edits shared.txt (forces a merge conflict)
  plan_changes: true         CEO requires plan changes once
  tasks: N                   number of feature tasks the lead creates (default 3)
  builder_tier: workhorse    the brief's choice of builder for one-builder jobs (default workhorse)
  codex_ceo_fails: true      the CEO on Codex (GPT-6 Astra) errors, so its Claude backup must take over
  review_limit_task: [ids]   the reviewer of these tasks hits its usage limit once on every Claude account
  limit_seconds: N           how long a usage limit lasts (default 3600)
  reject_twice: [ids]        the reviewer requests changes the first two times (a workhorse task moves up)
  contest_winner: tier       the head-to-head judge prefers this tier's version (default "workhorse")
  contest_fail: tier         the judge finds problems in this tier's version
  contest_borrow: text       what the judge says the losing version does better (default: nothing)
  no_reply_tool: true        an agent answers the owner's private message without team_reply_owner
  leak_task: [ids]           the first version of these tasks contains an API key (Crew's scan must send it back)
  codex_delay: N             Codex waits N seconds before it answers (time for the owner to press Stop)
  broken_checks: true        the lead sets a check that can never pass (a broken command, a missing tool)

The Assistant (one-to-one chats) also reads STATE/live-scenario.json before every message, so a test can change
these while a conversation's program keeps running:

  assistant_limit: [accounts]         Claude: the subscription is at its usage limit before answering
  assistant_limit_midway: [accounts]  Claude: it starts (some words, a file) and then reaches its limit
  codex_limit: [accounts]             ChatGPT: the same, for Codex
  codex_limit_midway: [accounts]
  codex_limit_text: text              ChatGPT: the words of its limit message

An account is the name of its profile folder, or "default" for the CLI's own. Each conversation remembers what was
said in its session file (Claude: projects/<folder>/<id>.jsonl; ChatGPT: sessions/…/rollout-…-<id>.jsonl), so asking
"what is the code word?" shows whether a conversation kept its memory across subscriptions. Every message received
is logged to STATE/assistant-prompts.jsonl (Claude) or STATE/codex-prompts.jsonl (ChatGPT).

The owner's private messages are answered with team_reply_owner; when the owner says "tell the team", the agent
also passes it on (share_with_team). The CEO answers the owner's questions, and the prompt writer writes the
owner's words up as "Clarified: <words>".

With a Codex account in the run, the lead makes odd-numbered features workhorse tasks (GPT-6 Sol) and even ones
manager tasks. A CEO asked for a JSON answer (--output-schema / --json-schema) on Codex answers in JSON only,
so the orchestrator records the verdict itself; on Claude it also calls its team tool, as before.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCEN = json.loads(os.environ.get("CREW_FAKE_SCENARIO") or "{}")
STATE = Path(os.environ.get("CREW_FAKE_STATE") or "/tmp/crew-fake-state")
STATE.mkdir(parents=True, exist_ok=True)


def live(key: str, default=None):
    """A scenario value that a test may change while the program runs (STATE/live-scenario.json wins)."""
    try:
        values = json.loads((STATE / "live-scenario.json").read_text())
    except (OSError, ValueError):
        values = {}
    return values[key] if key in values else SCEN.get(key, default)


def code_word(texts: list[str]) -> str:
    """The Assistant's memory test: the last code word the owner gave in what this conversation holds."""
    words = [w for t in texts for w in re.findall(r"code word is (?:now )?([A-Z][A-Z]+)", t)]
    return f"The code word is {words[-1]}." if words else "I don't know the code word."


def once(key: str) -> bool:
    """True the first time a key is seen across all fake processes."""
    marker = STATE / re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


# ------------------------------------------------------------------ MCP client


class Mcp:
    def __init__(self, command: str, args: list[str], env: dict):
        self.p = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env={**os.environ, **env}, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def call(self, name: str, **args) -> tuple[str, bool]:
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return res["content"][0]["text"], res["isError"]


# ------------------------------------------------------------------ git helpers


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def write_and_commit(files: dict[str, str], message: str) -> None:
    for path, text in files.items():
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh("git", "add", "-A")
    sh("git", "commit", "-q", "-m", message)


# ----------------------------------------------------------------------- brain


def checks() -> list[str]:
    """The project's checks the fake lead sets: the tests, or (broken_checks) a command that can never pass."""
    if SCEN.get("broken_checks"):
        return [f"{sys.executable} -c \"import sys; print('the check tool is missing'); sys.exit(1)\""]
    return [f"{sys.executable} -m unittest discover -s tests -q"]



class Brain:
    def __init__(self, emit, mcp: Mcp | None, seat: str, role: str, schema: dict | None = None, vendor: str = "claude"):
        self.emit, self.mcp, self.seat, self.role = emit, mcp, seat, role
        self.schema, self.vendor = schema, vendor
        self.structured = None  # a structured answer this turn produced (the prompt writer's)

    def json_only(self) -> bool:
        """The CEO on Codex with an output schema answers in JSON and leaves the recording to the orchestrator."""
        return self.vendor == "codex" and self.schema is not None

    def tool(self, name: str, **args) -> tuple[str, bool]:
        self.emit("tool_use", f"mcp__crew_team__{name}", args)
        text, err = self.mcp.call(name, **args)
        self.emit("tool_result", name, text)
        # Like a real agent, notice the owner's private question in a tool answer mid-task, and answer it.
        if "THE OWNER ASKS YOU DIRECTLY" in text and not SCEN.get("no_reply_tool") and not getattr(self, "_answering", False):
            self._answering = True
            try:
                self.answer_owner(text)
            finally:
                self._answering = False
        return text, err

    def answer_owner(self, text: str) -> None:
        after = text.split("DIRECTLY", 1)[1]
        asked = re.findall(r"^- (.+)$", after, re.M) or re.findall(r"then carry on\): (.+)$", after, re.M)
        said = asked[0] if asked else ""
        share = "The owner wants every module to have a docstring." if "tell the team" in said.lower() else ""
        self.tool("team_reply_owner", text=f"{self.seat} here: I am on my tasks and nothing blocks me. You asked: "
                                           f"{said[:80]}", share_with_team=share)

    def turn(self, text: str) -> str:
        if "You are the team's prompt writer" in text:
            words = re.search(r'The owner\'s words:\n"""(.*?)"""', text, re.S)
            self.structured = {"message": "Clarified: " + (words.group(1).strip() if words else ""), "changed": True}
            return json.dumps(self.structured)
        if "You are the CEO of this AI team" in text:
            return "CEO here: the plan is sound, and the team is on track to finish today."
        if "THE OWNER MESSAGED YOU DIRECTLY" in text or "THE OWNER ASKS YOU DIRECTLY" in text:
            if SCEN.get("no_reply_tool"):
                return f"{self.seat}: I am working through my tasks; nothing is blocking me."
            self.answer_owner(text)
        if "You are judging a head-to-head" in text:
            return "judged"  # the verdict is the structured answer (see judge_verdict)
        if "Turn the owner's request below into a precise brief" in text:
            return "BRIEF"
        if "senior reviewer with fresh eyes" in text:
            return self.review(text)
        if "CEO-level reviewer: the most capable model" in text:
            want = SCEN.get("plan_changes") and once("ceo-plan-changes")
            if want:
                if self.json_only():
                    return json.dumps({"verdict": "changes", "notes": "1. Split task 2 into smaller pieces.", "tasks": []})
                self.tool("team_verdict", kind="plan", verdict="changes", notes="1. Split task 2 into smaller pieces.")
            else:
                rows = re.findall(r"^#(\d+) .*? tier (\w+)", text, re.M)
                efforts = [{"task_id": int(i), "effort": ("xhigh" if n == 0 else "high"),
                            "tier": tier if tier in ("workhorse", "manager") else "manager"}
                           for n, (i, tier) in enumerate(rows)]
                if self.json_only():
                    return json.dumps({"verdict": "approve", "notes": "Sound plan.", "tasks": efforts})
                self.tool("team_verdict", kind="plan", verdict="approve", notes="", efforts=efforts)
            return "verdict given"
        if "final acceptance review" in text:
            if self.json_only():
                return json.dumps({"verdict": "approve", "notes": "Meets the brief."})
            self.tool("team_verdict", kind="final", verdict="approve", notes="")
            return "final verdict"
        if "CEO-level decision maker" in text:
            if self.json_only():
                return json.dumps({"decision": "Keep the current plan and split the stuck task.",
                                   "reason": "Smaller tasks finish and can be verified."})
            self.tool("team_decide", text="Ruling: keep the current plan, split the stuck task.")
            return "ruled"
        if "You are the lead." in text or "owner's project starts now. You are the lead" in text:
            return self.plan()
        if "The CEO requires changes to the plan" in text:
            self.tool("team_plan_ready", summary="Revised plan after the CEO's review.")
            return "replanned"
        if m := re.search(r"Task #(\d+) is yours", text):
            return self.work(int(m.group(1)), text)
        if "Every task is merged" in text:
            (Path("REPORT_NOTE.txt")).write_text("verified\n")
            sh("git", "add", "-A")
            sh("git", "commit", "-q", "-m", "final touch")
            self.tool("team_project_done", report="We built the feature modules and verified each one with the tests. "
                                                  "Open the project folder to use it.")
            return "done"
        if "Save 2–4 lessons" in text:
            self.tool("team_lesson_add", category="process", lesson="Fake teams finish faster when tasks are small and independent.")
            return "lessons saved"
        if "is planning" in text and self.role == "member":
            if SCEN.get("concern") and once(f"concern-{self.seat}"):
                self.tool("team_chat_post", text="Concern: task sizes look large.", kind="concern")
            return "oriented"
        if "PROGRESS STALLED" in text:
            self.tool("team_decide", text="Replan: continue; reviewers will re-check.")
            return "replanned"
        return "ok"

    def plan(self) -> str:
        n = int(SCEN.get("tasks", 3))
        pairs = list(seat_tiers().items())
        managers = [name for name, tier in pairs if tier == "manager" and name != self.seat] or [self.seat]
        workhorse = [name for name, tier in pairs if tier == "workhorse"]
        t, _ = self.tool("team_task_create", title="Foundation", spec="Create the package skeleton.",
                         acceptance="package imports", scope=["app/__init__.py"], size="S", kind="foundation",
                         suggested_owner=self.seat, tier="manager")
        found = int(re.search(r"#(\d+)", t).group(1))
        for i in range(1, n + 1):
            tier = "workhorse" if workhorse and i % 2 == 1 else "manager"
            owner = workhorse[(i // 2) % len(workhorse)] if tier == "workhorse" else managers[(i - 1) % len(managers)]
            self.tool("team_task_create", title=f"Feature {i}", spec=f"Implement feature {i}.",
                      acceptance=f"feat{i}() returns {i}", scope=[f"app/feat{i}.py", f"tests/test_feat{i}.py"],
                      depends_on=[found], size="S", tier=tier, suggested_owner=owner)
        self.tool("team_set_checks", commands=checks())
        self.tool("team_plan_ready", summary=f"Foundation then {n} independent features.")
        return "planned"

    def work(self, tid: int, text: str) -> str:
        if tid in SCEN.get("hang_on_task", []) and once(f"hang-{tid}"):
            time.sleep(10_000)
        if tid in SCEN.get("crash_on_task", []) and once(f"crash-{tid}"):
            sys.exit(1)
        if tid in SCEN.get("limit_on_task", []) and once(f"limit-{tid}"):
            return "LIMIT"
        detail, _ = self.tool("team_task_detail", task_id=tid)
        files = {}
        if "Foundation" in detail:
            files["app/__init__.py"] = "# package\n"
            files["tests/__init__.py"] = ""
        elif not re.search(r"Feature (\d+)", detail):  # solo mode: the whole project is one task
            files["app/__init__.py"] = "# package\n"
            files["tests/__init__.py"] = ""
            for i in range(1, int(SCEN.get("tasks", 3)) + 1):
                files[f"app/feat{i}.py"] = f"def feat{i}():\n    return {i}\n"
                files[f"tests/test_feat{i}.py"] = (f"import unittest\nfrom app.feat{i} import feat{i}\n\n\n"
                                                   f"class T(unittest.TestCase):\n    def test(self):\n"
                                                   f"        self.assertEqual(feat{i}(), {i})\n")
            self.tool("team_set_checks", commands=checks())
        else:
            i = int(re.search(r"Feature (\d+)", detail).group(1))
            files[f"app/feat{i}.py"] = f"def feat{i}():\n    return {i}\n"
            if tid in SCEN.get("leak_task", []) and once(f"leak-{tid}"):
                files[f"app/feat{i}.py"] = f'API_KEY = "sk-ant-api03-Qw3rTy7UiOp9AsDfGhJkLzXcVbNm1234"\n\n\ndef feat{i}():\n    return {i}\n'
            files[f"tests/test_feat{i}.py"] = (f"import unittest\nfrom app.feat{i} import feat{i}\n\n\n"
                                               f"class T(unittest.TestCase):\n    def test(self):\n"
                                               f"        self.assertEqual(feat{i}(), {i})\n")
            if tid in SCEN.get("outside_scope_task", []):
                files["shared.txt"] = f"edited by {self.seat} for task {tid}\n"
        if "Merge conflict" in text:
            sh("git", "merge", "-X", "theirs", "--no-edit", os.environ.get("CREW_FAKE_INTEGRATION", ""))
            files.pop("shared.txt", None)
        write_and_commit(files, f"task {tid} by {self.seat}")
        self.tool("team_task_note", task_id=tid, note="Done: files written. Next: submit.")
        out = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                             capture_output=True, text=True)
        self.tool("team_task_submit", task_id=tid, summary=f"Implemented task {tid} with a unit test.",
                  evidence=f"unittest exit {out.returncode}")
        return "submitted"

    def review(self, text: str) -> str:
        tid = int(re.search(r"Review task #(\d+)", text).group(1))
        if tid in SCEN.get("reject_twice", []) and (once(f"reject2a-{tid}") or once(f"reject2b-{tid}")):
            self.tool("team_review_submit", task_id=tid, verdict="changes",
                      notes="1. The feature returns the wrong value for the edge case; handle it and add a test.")
            return "reviewed"
        if tid in SCEN.get("review_limit_task", []) and once(f"review-limit-{tid}-{os.environ.get('CREW_FAKE_ACCOUNT')}"):
            return "LIMIT"
        if tid in SCEN.get("reject_task", []) and once(f"reject-{tid}"):
            self.tool("team_review_submit", task_id=tid, verdict="changes",
                      notes="1. app/feat.py: add a docstring and handle the edge case; re-run the tests.")
        else:
            self.tool("team_review_submit", task_id=tid, verdict="approve", notes="Looks correct; tests pass.")
        return "reviewed"


def seat_tiers() -> dict[str, str]:
    """The team's seats and their tiers, from CREW_FAKE_SEATS ("name:tier,…", as the tests set it)."""
    out = {}
    for item in (s.strip() for s in os.environ.get("CREW_FAKE_SEATS", "").split(",")):
        if item:
            name, _, tier = item.partition(":")
            out[name] = tier or "manager"
    return out


def judge_verdict() -> dict:
    """The head-to-head judge's answer: it looks at who committed each version (./a and ./b) to find its tier."""
    tiers = seat_tiers()

    def tier(folder: str) -> str:
        subject = sh("git", "-C", folder, "log", "-1", "--format=%s")
        m = re.search(r"by ([\w-]+)", subject)
        return tiers.get(m.group(1), "manager") if m else "manager"

    v = {k: tier(k) for k in ("a", "b")}
    out = {k: ({"verdict": "changes", "notes": "1. The edge case fails; add a test and handle it."}
               if v[k] == SCEN.get("contest_fail") else {"verdict": "approve", "notes": "Meets the spec."}) for k in v}
    prefer = SCEN.get("contest_winner", "workhorse")
    out["winner"] = next((k for k in ("a", "b") if v[k] == prefer), "a")
    out["reason"] = f"The {prefer} version is simpler and fully tested."
    out["borrow"] = SCEN.get("contest_borrow", "")
    return out


# ------------------------------------------------------------------- claude mode


FAKE_SKILLS = ["xlsx", "docx", "pptx", "pdf", "frontend-design", "canvas-design", "doc-coauthoring", "internal-comms",
               "brand-guidelines", "skill-creator"]
FAKE_COMMANDS = ["compact", "context", "usage", "cost", "clear", "review", "init", "security-review"] + FAKE_SKILLS


def claude_version() -> str:
    f = STATE / "claude-version"
    return f.read_text().strip() if f.is_file() else str(SCEN.get("claude_version", "2.1.290"))


def assistant_main(argv: list[str]) -> int:
    """The app's Assistant: a persistent conversation that behaves like Claude Code's stream-json mode —
    word-by-word text, thinking, tools, to-dos, helpers (sub-agents), plan mode, slash commands, limits."""
    import queue
    import threading

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    model = opt("--model", "claude-opus-5-5")
    sid = opt("--resume") or str(uuid.uuid4())
    state = {"mode": opt("--permission-mode", "default"), "context": 12000 if opt("--resume") else 9000}
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CREW_FAKE_CLAUDE_HOME")
                or Path.home() / ".claude-fake")
    account = home.name if os.environ.get("CLAUDE_CONFIG_DIR") else "default"  # the profile folder's name
    transcript = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()) / f"{sid}.jsonl"
    if opt("--resume") and not transcript.is_file():  # as Claude Code does: this subscription does not hold it
        sys.stderr.write(f"No conversation found with session ID: {sid}\n")
        return 1

    def remember(role: str, said: str) -> None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": role, "text": said}) + "\n")

    def recall() -> list[str]:
        try:
            return [json.loads(line).get("text", "") for line in transcript.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except (OSError, ValueError):
            return []
    devices = None
    cfg = opt("--mcp-config")
    servers = {}
    if cfg and Path(cfg).is_file():
        servers = json.loads(Path(cfg).read_text()).get("mcpServers", {})
        server = servers.get("crew_devices")
        if server:
            devices = Mcp(server["command"], server["args"], server.get("env", {}))

    inbox: "queue.Queue[dict]" = queue.Queue()
    interrupted = threading.Event()

    def reader():
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "control_request":
                req = msg.get("request") or {}
                if req.get("subtype") == "interrupt":
                    interrupted.set()
                elif req.get("subtype") == "set_permission_mode":
                    state["mode"] = req.get("mode")
                out({"type": "control_response", "response": {"subtype": "success", "request_id": msg.get("request_id")}})
            else:
                inbox.put(msg)
        inbox.put({"type": "eof"})

    lock = threading.Lock()

    def out(obj):
        with lock:
            sys.stdout.write(json.dumps({**obj, "session_id": sid} if "session_id" not in obj else obj) + "\n")
            sys.stdout.flush()

    def stream_text(text: str) -> str:
        out({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                                "content_block": {"type": "text", "text": ""}}})
        sent = ""
        for word in re.findall(r"\S+\s*", text):
            if interrupted.is_set():
                break
            out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                                    "delta": {"type": "text_delta", "text": word}}})
            sent += word
            time.sleep(float(SCEN.get("word_delay", 0.03)))
        return sent

    def tool_use(name: str, inp: dict, parent: str | None = None) -> str:
        tid = "toolu_" + uuid.uuid4().hex[:20]
        out({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}, "parent_tool_use_id": parent})
        return tid

    def tool_result(tid: str, content, parent: str | None = None, error: bool = False) -> None:
        out({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}]},
            "parent_tool_use_id": parent})

    def device(name: str, **args) -> str:
        tid = tool_use(f"mcp__crew_devices__{name}", args)
        text = devices.call(name, **args)[0] if devices is not None else "no devices"
        tool_result(tid, text[:300])
        return text

    def think(n: int) -> None:
        out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                "content_block": {"type": "thinking", "thinking": "", "signature": ""}}})
        for est in (50, n):
            out({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": est})
            time.sleep(0.05)

    def finish(result: str, usage: dict | None = None, local: str | None = None, error: bool = False, **extra) -> None:
        payload = {"type": "result", "subtype": "success" if not error else "error_during_execution",
                   "is_error": error, "result": result, "num_turns": 0 if local else 1,
                   "usage": usage or {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0},
                   "modelUsage": {model: {"contextWindow": 1000000, "maxOutputTokens": 128000}},
                   "total_cost_usd": 0.01, "duration_ms": 40, **extra}
        if local:
            payload["local_command"] = local
        out(payload)

    def rate() -> None:
        f = STATE / f"assistant-util-{account}"
        util = float(f.read_text()) if f.is_file() else float(SCEN.get("start_util", 0.12))
        util = min(0.99, util + 0.03)
        f.write_text(str(util))
        now_s = int(time.time())
        out({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "utilization": util,
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": now_s + 3 * 3600},
                               "seven_day": {"utilization": util / 3, "resetsAt": now_s + 4 * 86400}}}})

    out({"type": "autocompact_state", "value": {"enabled": True, "effective_window": 980000, "threshold": 784000}})
    out({"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": os.getcwd(),
         "permissionMode": state["mode"], "claude_code_version": claude_version(),
         "tools": ["Task", "Bash", "Edit", "Read", "Write", "WebSearch", "WebFetch", "TaskCreate", "TaskUpdate", "Skill"]
         + [f"mcp__crew_devices__{n}" for n in ("browser_open", "browser_read")],
         "mcp_servers": [{"name": n, "status": "connected"} for n in servers],
         "slash_commands": FAKE_COMMANDS, "skills": FAKE_SKILLS,
         "agents": ["general-purpose", "Explore", "Plan"],
         "plugins": [{"name": "document-skills", "path": "/fake"}, {"name": "example-skills", "path": "/fake"}]})
    threading.Thread(target=reader, daemon=True).start()

    while True:
        msg = inbox.get()
        if msg.get("type") == "eof":
            return 0
        if msg.get("type") != "user":
            continue
        interrupted.clear()
        content = msg["message"]["content"]
        text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
        low = text.lower().strip()
        started = time.time()
        with (STATE / "assistant-prompts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"account": account, "session": sid, "text": text}) + "\n")
        remember("user", text)  # Claude Code saves the owner's message before it asks the model
        if SCEN.get("assistant_outdated") and claude_version() < "2.1.280":
            finish(f"API Error: 400 Claude Code {claude_version()} does not support this model; version 2.1.280 or "
                   "newer is required. Run 'claude update', or update the Claude desktop app, then try again.",
                   error=True)
            continue
        midway = account in (live("assistant_limit_midway") or [])
        if account in (live("assistant_limit") or []) or midway:
            if midway:  # it starts on the work, and the limit stops it part of the way through
                started_text = stream_text("I have started on it: the first part is written. ")
                draft = Path("draft.txt")
                tid = tool_use("Write", {"file_path": str(draft.resolve()), "content": "first part"})
                draft.write_text("first part\n", encoding="utf-8")
                tool_result(tid, "File created successfully")
                remember("assistant", started_text + "(wrote draft.txt)")
            reset = int(time.time()) + 3600
            out({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "utilization": 1.0, "resetsAt": reset,
                "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": reset}}}})
            remember("assistant", "API Error: Claude AI usage limit reached")
            finish(f"Claude AI usage limit reached|{reset}", error=True)
            continue
        if low.startswith("/context"):
            report = (f"## Context Usage\n\n**Model:** {model}  \n**Tokens:** {state['context'] / 1000:.1f}k / 1m "
                      f"({state['context'] / 10000:.0f}%)\n\n| Category | Tokens |\n|---|---|\n| System prompt | 2.2k |\n"
                      f"| Messages | {(state['context'] - 2200) / 1000:.1f}k |")
            out({"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": report}]}})
            finish(report, local="context")
            continue
        if low.startswith("/usage") or low.startswith("/cost"):
            report = "Total cost: $0.05\nUsage by model:\n  claude-opus-5-5: 2 input, 10 output"
            out({"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": report}]}})
            finish(report, local="usage")
            continue
        if low.startswith("/compact"):
            out({"type": "system", "subtype": "status", "status": "compacting"})
            time.sleep(0.2)
            pre, state["context"] = state["context"], 1500
            out({"type": "system", "subtype": "compact_boundary",
                 "compact_metadata": {"trigger": "manual", "pre_tokens": pre, "post_tokens": 1500}})
            finish("", local="compact")
            continue

        state["context"] += 900 + len(text) // 3
        out({"type": "stream_event", "event": {"type": "message_start", "message": {
            "model": model, "usage": {"input_tokens": 3, "cache_read_input_tokens": state["context"] - 3,
                                      "cache_creation_input_tokens": 0}}}})
        think(int(SCEN.get("thinking_tokens", 240)))
        answer = ""
        if "what is the code word" in low:
            answer = code_word(recall())
        elif state["mode"] == "plan":
            plans = home / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            plan_path = plans / "calm-river.md"
            plan = ("# Plan\n\n1. Look at what is needed.\n2. Make the page `hello.html` with a greeting.\n"
                    "3. Check that it opens correctly.")
            plan_path.write_text(plan)
            tid = tool_use("Write", {"file_path": str(plan_path), "content": plan})
            tool_result(tid, f"File created successfully at: {plan_path}")
            answer = "Here is my plan:\n\n1. Look at what is needed.\n2. Make the page.\n3. Check it.\n\nApprove it and I will start."
        elif "approved" in low and "plan" in low:
            Path("hello.html").write_text("<!doctype html><title>Hello</title><h1>Hello!</h1>", encoding="utf-8")
            tid = tool_use("Write", {"file_path": str(Path("hello.html").resolve()), "content": "<h1>Hello!</h1>"})
            tool_result(tid, "File created successfully")
            answer = "Done — I made **hello.html** as planned."
        elif "research" in low:
            tid = tool_use("Task", {"description": "Research the topic", "subagent_type": "general-purpose",
                                    "prompt": "Find three facts."})
            for q in ("first source", "second source"):
                sub = tool_use("WebSearch", {"query": q}, parent=tid)
                time.sleep(0.1)
                tool_result(sub, "results", parent=tid)
            tool_result(tid, [{"type": "text", "text": "Three facts found."}])
            answer = "My helper found three facts:\n\n1. One.\n2. Two.\n3. Three."
        elif "checklist" in low:  # Crew's own to-do tool (headless Claude Code may have none)
            items = [{"content": "Read the brief", "status": "in_progress"}, {"content": "Write the note", "status": "pending"}]
            device("todo_write", todos=items)
            device("todo_write", todos=[{**x, "status": "completed"} for x in items])
            answer = "Checklist complete."
        elif "steps" in low or "to-do" in low or "todo" in low:
            ids = []
            for n, subject in enumerate(("Gather the numbers", "Write the summary", "Check the result"), start=1):
                tid = tool_use("TaskCreate", {"subject": subject, "description": subject})
                tool_result(tid, f"Task #{n} created successfully: {subject}")
                ids.append(str(n))
            for n in ids:
                tid = tool_use("TaskUpdate", {"taskId": n, "status": "in_progress"})
                tool_result(tid, f"Updated task #{n} status")
                time.sleep(0.05)
                tid = tool_use("TaskUpdate", {"taskId": n, "status": "completed"})
                tool_result(tid, f"Updated task #{n} status")
            answer = "All three steps are done."
        elif "open" in low and ("website" in low or "http" in low or "page" in low and "browser" in low):
            url = (re.findall(r"https?://\S+", text) or ["https://example.com"])[0]
            device("browser_open", url=url)
            seen = device("browser_read")
            title = (re.findall(r'"title": "([^"]*)"', seen) or ["the page"])[0]
            answer = f"I opened **{title}** in the browser.\n\n- It loaded without problems.\n- You can see it in the side panel."
        elif "make" in low and "page" in low:
            tid = tool_use("Write", {"file_path": str(Path("page.html").resolve()), "content": "<h1>Hello</h1>"})
            Path("page.html").write_text("<!doctype html><title>Demo</title><h1>Hello from the assistant</h1>",
                                         encoding="utf-8")
            tool_result(tid, "File created successfully")
            answer = "I made a small web page for you. Open it from the card below."
        elif "attached" in low:
            names = re.findall(r"attachments/[\w.-]+", text)
            answer = f"I looked at {', '.join(names) or 'your file'}. It arrived safely."
        elif "slow" in low:
            answer = " ".join(["word"] * 400)
        elif "which keys" in low:
            keys = sorted(k for k in os.environ if k.endswith("_API_KEY"))
            answer = "I can use: " + (", ".join(keys) or "none")
        else:
            answer = ("Here is a short answer.\n\n## Summary\n\n- **First**, the main point.\n- **Second**, a supporting point.\n\n"
                      "| Item | Value |\n|---|---|\n| Speed | Fast |\n\nThat is all.")
        said = stream_text(answer)
        if interrupted.is_set():
            finish(said, error=True, subtype="error_during_execution", terminal_reason="aborted")
            continue
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}})
        remember("assistant", answer)
        rate()
        out_tokens = len(answer.split()) * 2
        state["context"] += out_tokens
        finish(answer, usage={"input_tokens": 3, "output_tokens": out_tokens, "cache_read_input_tokens": state["context"],
                              "cache_creation_input_tokens": 800}, duration_ms=int((time.time() - started) * 1000))


def claude_main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"{claude_version()} (Claude Code)")
        return 0
    if argv[:1] == ["update"]:
        before = claude_version()
        (STATE / "claude-version").write_text("2.1.290")
        print(f"Successfully updated from {before} to version 2.1.290" if before != "2.1.290"
              else "Claude Code is up to date (2.1.290)")
        return 0
    if argv[:2] == ["auth", "status"]:  # as the real CLI: it reports, and starts no conversation
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CREW_FAKE_CLAUDE_HOME")
                    or Path.home() / ".claude-fake")
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": f"{home.name}@example.com"}))
        return 0
    if argv[:2] == ["auth", "login"]:
        print("Opening your browser to sign in…")
        return 0
    if argv[:2] == ["plugin", "marketplace"] or argv[:2] == ["plugin", "install"]:
        (STATE / ("plugin-" + "-".join(a.replace("/", "_") for a in argv[1:4]))).write_text("ok")
        print("done")
        return 0
    if "--include-partial-messages" in argv:
        return assistant_main(argv)

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    stream_in = opt("--input-format") == "stream-json"
    model = opt("--model", "claude-opus-5-5")
    resume = opt("--resume")
    schema = opt("--json-schema")
    seat, role = os.environ.get("CREW_SEAT", "?"), os.environ.get("CREW_ROLE", "member")
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CREW_FAKE_CLAUDE_HOME")
                or Path.home() / ".claude-fake")
    sessdir = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    mcp = None
    cfg = opt("--mcp-config")
    if cfg and Path(cfg).is_file():
        server = json.loads(Path(cfg).read_text())["mcpServers"]["crew_team"]
        mcp = Mcp(server["command"], server["args"], server.get("env", {}))

    def out(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    sid = resume or str(uuid.uuid4())
    if resume and not (sessdir / f"{sid}.jsonl").is_file():
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": f"No conversation found with session ID: {sid}", "session_id": sid})
        return 1
    out({"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": os.getcwd(),
         "mcp_servers": [{"name": "crew_team", "status": "connected"}] if mcp else []})

    def emit(kind, name, payload):
        if kind == "tool_use":
            out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": payload}]},
                 "session_id": sid})
        else:
            out({"type": "user", "message": {"content": [{"type": "tool_result", "content": str(payload)[:200]}]},
                 "session_id": sid})

    brain = Brain(emit, mcp, seat, role)
    account = home.name
    os.environ["CREW_FAKE_ACCOUNT"] = account
    util_file = STATE / f"util-{account}"

    def run_turn(text: str) -> None:
        sessdir.mkdir(parents=True, exist_ok=True)
        with (sessdir / f"{sid}.jsonl").open("a") as fh:
            fh.write(json.dumps({"type": "user", "text": text[:500]}) + "\n")
        util = float(util_file.read_text()) if util_file.exists() else 0.1
        result = brain.turn(text)
        if result == "LIMIT":
            resets = int(time.time()) + int(SCEN.get("limit_seconds", 3600))
            out({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour",
                "utilization": 1.0, "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": resets}}},
                "session_id": sid})
            out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "result": "Claude AI usage limit reached|" + str(resets), "session_id": sid})
            return
        util = min(0.95, util + 0.02)
        util_file.write_text(str(util))
        out({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "utilization": util,
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": int(time.time()) + 4 * 3600},
                               "seven_day": {"utilization": util / 4, "resetsAt": int(time.time()) + 5 * 86400}}},
            "session_id": sid})
        payload = {"type": "result", "subtype": "success", "is_error": False, "result": result,
                   "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 500},
                   "total_cost_usd": 0.02, "num_turns": 1, "duration_ms": 50, "session_id": sid}
        if schema and '"winner"' in schema:
            payload["structured_output"] = judge_verdict()
        elif schema and '"changed"' in schema:
            payload["structured_output"] = brain.structured or {"message": result, "changed": False}
        elif schema and '"builder_tier"' in schema:
            payload["structured_output"] = {
                "title": "Feature pack", "goal": "Build a small package of features with tests.",
                "deliverables": ["app package"], "acceptance_criteria": ["all tests pass"],
                "constraints": [], "assumptions": ["Python standard library only"],
                "size": SCEN.get("size", "medium"), "independent_parts": int(SCEN.get("parts", 3)),
                "builder_tier": SCEN.get("builder_tier", "workhorse"), "builder_effort": SCEN.get("solo_effort", "medium")}
        elif schema and '"verdict"' in schema:
            payload["structured_output"] = {"verdict": "approve", "notes": "Recorded with the team tool.", "tasks": []}
        elif schema and '"decision"' in schema:
            payload["structured_output"] = {"decision": "Recorded with the team tool.", "reason": "-"}
        out(payload)

    if stream_in:
        for line in sys.stdin:
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("type") != "user":
                continue
            content = msg["message"]["content"]
            text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
            run_turn(text)
    else:
        run_turn(sys.stdin.read())
    return 0


# -------------------------------------------------------------------- codex mode


def codex_main(argv: list[str]) -> int:
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    conf = {}
    for c in configs:
        key, _, value = c.partition("=")
        conf[key] = value
    command = json.loads(conf.get("mcp_servers.crew_team.command", "null") or "null")
    args = json.loads(conf.get("mcp_servers.crew_team.args", "[]"))
    env = {}
    table = conf.get("mcp_servers.crew_team.env", "{}").strip()[1:-1]
    for part in re.finditer(r'(\w+) = ("(?:[^"\\]|\\.)*")', table):
        env[part.group(1)] = json.loads(part.group(2))
    mcp = Mcp(command, args, env) if command else None
    seat, role = env.get("CREW_SEAT", "?"), env.get("CREW_ROLE", "member")
    home = Path(os.environ.get("CODEX_HOME") or os.environ.get("CREW_FAKE_CODEX_HOME") or Path.home() / ".codex-fake")
    account = home.name if os.environ.get("CODEX_HOME") else "default"  # the profile folder's name
    resume = "resume" in argv
    if resume:
        rest = [a for a in argv[argv.index("resume") + 1:] if not a.startswith("-")]
        positional = [a for i, a in enumerate(argv[argv.index("resume") + 1:]) if not a.startswith("-")
                      and argv[argv.index("resume") + i] != "-c"]
        tid = next(p for p in positional if re.fullmatch(r"[0-9a-f-]{36}", p))
    else:
        tid = str(uuid.uuid4())
    if "-C" in argv:
        os.chdir(argv[argv.index("-C") + 1])

    def out(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    out({"type": "thread.started", "thread_id": tid})
    out({"type": "turn.started"})

    def emit(kind, name, payload):
        if kind == "tool_use":
            out({"type": "item.started", "item": {"id": uuid.uuid4().hex[:8], "type": "mcp_tool_call", "tool": name}})

    text = sys.stdin.read()
    if SCEN.get("codex_delay"):
        time.sleep(float(SCEN["codex_delay"]))
    day = time.strftime("%Y/%m/%d")
    rollouts = sorted(home.glob(f"sessions/**/rollout-*{tid}.jsonl")) if resume else []
    rollout = rollouts[0] if rollouts else home / "sessions" / day / f"rollout-2026-{tid}.jsonl"

    def remember(role: str, said: str) -> None:
        rollout.parent.mkdir(parents=True, exist_ok=True)
        with rollout.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{"type": "input_text", "text": said}]}}) + "\n")

    def recall() -> list[str]:
        try:
            lines = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return []
        return [c.get("text", "") for x in lines if x.get("type") == "response_item"
                for c in (x.get("payload") or {}).get("content") or []]

    if seat == "?":  # the Assistant (a one-to-one chat), not a team seat
        with (STATE / "codex-prompts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"account": account, "resume": resume, "thread": tid, "text": text}) + "\n")
        remember("user", text)
        midway = account in (live("codex_limit_midway") or [])
        if account in (live("codex_limit") or []) or midway:
            if midway:  # it starts on the work, and the limit stops it part of the way through
                out({"type": "item.completed", "item": {"id": "m0", "type": "agent_message",
                                                        "text": "I have started on it: the first part is written."}})
                Path("draft.txt").write_text("first part\n", encoding="utf-8")
                out({"type": "item.completed", "item": {"id": "f0", "type": "file_change", "status": "completed",
                                                        "changes": [{"path": str(Path("draft.txt").resolve()),
                                                                     "kind": "add"}]}})
                remember("assistant", "I have started on it: the first part is written.")
            message = live("codex_limit_text") or ("You've hit your usage limit. Upgrade to Pro "
                                                   "(https://chatgpt.com/explore/pro), or try again in 2 hours 5 minutes.")
            out({"type": "error", "message": message})
            out({"type": "turn.failed", "error": {"message": message}})
            return 1
        if "what is the code word" in text.lower():
            said = code_word(recall())
            remember("assistant", said)
            out({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": said}})
            out({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 20}})
            return 0
    schema = None
    if "--output-schema" in argv:
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    model = argv[argv.index("-m") + 1] if "-m" in argv else ""
    with (STATE / "codex-calls.jsonl").open("a") as fh:  # what the tests check: model, effort, role, schema
        effort = next((c.split("=", 1)[1].strip('"') for c in configs if c.startswith("model_reasoning_effort=")), "")
        fh.write(json.dumps({"seat": seat, "role": role, "model": model, "effort": effort,
                             "schema": bool(schema)}) + "\n")
    if role == "ceo" and SCEN.get("codex_ceo_fails"):
        out({"type": "turn.failed", "error": {"message": "The model gpt-6-astra is not available on your plan."}})
        return 1
    result = Brain(emit, mcp, seat, role, schema=schema, vendor="codex").turn(text)
    if result == "LIMIT":
        out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})
        return 1
    if seat == "?":
        remember("assistant", result)
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("a") as fh:
        fh.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 30.0, "window_minutes": 300, "resets_in_seconds": 7200},
            "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_in_seconds": 400000}}}}) + "\n")
    out({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": result}})
    out({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 200}})
    return 0


if __name__ == "__main__":
    mode = sys.argv[1]
    sys.exit(claude_main(sys.argv[2:]) if mode == "claude" else codex_main(sys.argv[2:]))
