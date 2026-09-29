"""The team tools every agent gets (served over MCP by mcp_server.py).

Design rules (see ARCHITECTURE.md):
  * one group chat, no private messages;
  * authority is enforced here, not trusted to prompts (only the lead plans
    and decides, only the owner submits, only the reviewer reviews);
  * chat is budgeted so agents work instead of arguing;
  * every answer ends with a short digest of unread chat, urgent items inline.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import lessons as lessons_mod
from . import scorecard
from .store import KINDS, SIZES, Store, StoreError
from .tiers import TIERS, model_label, seat_tier
from .util import clip, hhmm, now

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CHAT_KINDS = ("update", "question", "answer", "blocker", "concern")
ROLES_ALL = ("lead", "member", "reviewer", "ceo")


class ToolError(Exception):
    """A refusal the agent should read and adapt to (returned with isError)."""


@dataclass
class Ctx:
    store: Store
    seat: str
    role: str
    task_id: int | None = None  # set for reviewer runs

    @property
    def settings(self) -> dict:
        return self.store.get("settings", {}) or {}

    @classmethod
    def from_env(cls, store: Store) -> "Ctx":
        task = os.environ.get("CREW_TASK")
        return cls(store=store, seat=os.environ.get("CREW_SEAT", "unknown"),
                   role=os.environ.get("CREW_ROLE", "member"), task_id=int(task) if task else None)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Ctx, dict], str]
    roles: tuple[str, ...] = ROLES_ALL
    footer: bool = True

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
LIST_S = {"type": "array", "items": {"type": "string"}}
LIST_I = {"type": "array", "items": {"type": "integer"}}


# ------------------------------------------------------------------ helpers


def owner_words(m: dict, limit: int = 1500) -> str:
    """An owner's message as the agents read it: the prompt writer's clear version, plus the owner's own words
    (for intent) when the writer changed them."""
    text = clip(m["text"], limit)
    own = (m.get("original") or "").strip()
    if own and own != (m["text"] or "").strip():
        text += f'\n  (the owner\'s own words, for intent: "{clip(own, 800)}")'
    return text


def _fmt_msg(m: dict, limit: int = 900) -> str:
    task = f" (task #{m['task_id']})" if m.get("task_id") else ""
    flag = " URGENT" if m.get("urgent") else ""
    to = f" → {m['recipient']}, directly" if m.get("recipient") else ""
    who = "the owner" if m["sender"] == "you" else m["sender"]
    text = owner_words(m, limit) if m["sender"] == "you" else clip(m["text"], limit)
    return f"#{m['id']} {hhmm(m['ts'])} {who}{to} [{m['kind']}{flag}]{task}: {text}"


def _mentions(text: str, seat: str) -> bool:
    return f"@{seat}".lower() in (text or "").lower() or "@all" in (text or "").lower()


MENTION = re.compile(r"(?<![\w.@/])@([A-Za-z][\w-]*)")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)
ACK = re.compile(r"(?i)^\W*(?:(?:ok(?:ay)?|k|thanks?|thank you|thx|ty|got it|noted|agreed|agree|sounds good|will do|"
                 r"understood|acknowledged|ack|roger|perfect|great|cool|nice|sure|yes|yep|done|on it|lgtm|"
                 r"\+1|👍|🙏)[\s,.!]*)+\W*$")


def _lead(ctx: Ctx) -> str:
    return next((s["name"] for s in ctx.store.seats() if s["role"] == "lead"), "")


def _address(ctx: Ctx, text: str) -> tuple[str, list[str], list[str]]:
    """Resolve @lead to the lead's name; return (text, the teammates named, names that are not on the team).
    Mentions inside `code` are not names."""
    lead = _lead(ctx)
    if lead:
        text = re.sub(r"(?<![\w.@/])@lead\b", f"@{lead}", text, flags=re.I)
    team = {s["name"].lower() for s in ctx.store.seats()}
    named, unknown = [], []
    for name in MENTION.findall(CODE_SPAN.sub(" ", text)):
        low = name.lower()
        if low in team:
            named.append(low)
        elif low not in ("all", "owner", "you", "team", "everyone") and low not in unknown:
            unknown.append(low)
    return text, named, unknown


def _roster(ctx: Ctx) -> str:
    return ", ".join(f"@{s['name']} ({s['role']})" for s in ctx.store.seats())


def _owner_asks(ctx: Ctx, unread: list[dict]) -> list[dict]:
    """The owner's unanswered direct messages to this seat; noted so that the owner always gets an answer."""
    asks = ctx.store.owner_asks(ctx.seat, unread)
    if asks:
        ctx.store.set(f"owner_q:{ctx.seat}", asks[-1]["id"])
    return asks


def _is_owner_direct(m: dict, seat: str) -> bool:
    return m["sender"] == "you" and m.get("recipient") == seat


def digest(ctx: Ctx) -> str:
    """One-line unread summary with urgent messages inline (appended to tool answers)."""
    unread = ctx.store.unread(ctx.seat)
    if not unread:
        return ""
    asks = _owner_asks(ctx, unread)
    team = [m for m in unread if not _is_owner_direct(m, ctx.seat)]
    urgent = [m for m in team if m["urgent"] or _mentions(m["text"], ctx.seat)]
    lines = []
    if team:
        lines.append(f"[team chat: {len(team)} unread message(s)" + (f", {len(urgent)} for you" if urgent else "")
                     + " — call team_chat_read when you reach a stopping point]")
    for m in asks[-3:]:
        lines.append("  ! THE OWNER ASKS YOU DIRECTLY (only you see this; answer now with team_reply_owner, in plain "
                     "words, then carry on): " + owner_words(m, 600))
    for m in urgent[-3:]:
        lines.append("  ! " + _fmt_msg(m, 400))
    return "\n".join(lines)


def _require_task(ctx: Ctx, task_id, owner_only: bool = True) -> dict:
    task = ctx.store.task(int(task_id))
    if not task:
        raise ToolError(f"There is no task #{task_id}. Call team_tasks to see the board.")
    if owner_only and task["owner"] != ctx.seat and ctx.role != "lead":
        raise ToolError(f"Task #{task_id} belongs to {task['owner'] or 'nobody'}, not you.")
    return task


def _phase(ctx: Ctx) -> str:
    return ctx.store.get("phase", "plan")


def _budget(ctx: Ctx) -> int:
    base = int(ctx.settings.get("chat_budget", 8))
    return base * 3 if ctx.role == "lead" else base


# -------------------------------------------------------------------- chat


def chat_post(ctx: Ctx, a: dict) -> str:
    text = (a.get("text") or "").strip()
    kind = a.get("kind") or "update"
    if not text:
        raise ToolError("Empty message.")
    if kind not in CHAT_KINDS:
        raise ToolError(f"kind must be one of {CHAT_KINDS} (decisions go through team_decide).")
    if len(text) > 2500:
        raise ToolError("Too long for chat (2500 chars). Put details in team_task_note or a file and summarise here.")
    if kind != "blocker" and ACK.match(MENTION.sub(" ", text)):
        raise ToolError("No need to acknowledge: silence means agreement, and every message costs the team tokens. "
                        "Carry on with your work.")
    text, named, unknown = _address(ctx, text)
    lead = _lead(ctx)
    routed = ""
    if kind in ("question", "blocker", "concern") and not named and "@all" not in text.lower() \
            and lead and lead != ctx.seat:
        text += f" @{lead}"  # a question nobody is named in reaches nobody: the lead gets it
        routed = f" You named no one, so it went to the lead (@{lead}); name who you need with @name."
    warn = (f" Note: there is no {', '.join('@' + n for n in unknown)} on this team, so nobody was notified for that "
            f"name. The team: {_roster(ctx)}." if unknown else "")
    seat = ctx.store.seat(ctx.seat) or {}
    if kind == "concern":
        if _phase(ctx) != "plan":
            raise ToolError("Concerns are for the planning round. During the build use 'question' or 'blocker'.")
        since = int(ctx.store.get("plan_msg_id", 0) or 0)
        mine = [m for m in ctx.store.messages_after(since) if m["sender"] == ctx.seat and m["kind"] == "concern"]
        if mine:
            raise ToolError("You already raised your concern for this plan. The lead decides; get on with your work.")
    if kind != "blocker":
        used = int(seat.get("chat_used") or 0)
        if used >= _budget(ctx):
            raise ToolError(
                "Chat budget used up for your current task. Only 'blocker' messages are allowed now. "
                "Record progress with team_task_note, settle disagreements with a test or team_escalate, and keep working."
            )
        ctx.store.update_seat(ctx.seat, chat_used=used + 1)
    task_id = a.get("task_id") or seat.get("current_task")
    msg_id = ctx.store.post(ctx.seat, kind, text, task_id=task_id, urgent=(kind == "blocker"))
    return f"Posted to the team chat as #{msg_id}.{routed}{warn}"


def chat_read(ctx: Ctx, a: dict) -> str:
    limit = max(5, min(int(a.get("max") or 40), 100))
    unread = ctx.store.unread(ctx.seat, limit=500)
    if not unread:
        return "No unread messages."
    asks = _owner_asks(ctx, unread)
    team = [m for m in unread if not _is_owner_direct(m, ctx.seat)]
    skipped = max(0, len(team) - limit)
    shown = team[-limit:]
    ctx.store.mark_read(ctx.seat, unread[-1]["id"])
    head = f"({skipped} older unread messages skipped; team_status has the overview)\n" if skipped else ""
    owner = ("THE OWNER ASKS YOU DIRECTLY (only you see this; answer now with team_reply_owner, in plain words):\n"
             + "\n".join(f"- {owner_words(m)}" for m in asks[-3:]) + "\n\n") if asks else ""
    return owner + head + ("\n".join(_fmt_msg(m) for m in shown) or "No other unread messages.")


# ------------------------------------------------------------------- board


def _task_line(t: dict) -> str:
    deps = f" after #{','.join(map(str, t['depends_on']))}" if t["depends_on"] else ""
    owner = f" [{t['owner']}]" if t["owner"] else (f" (suggested: {t['suggested_owner']})" if t.get("suggested_owner") else "")
    scope = ", ".join(t["scope"][:4]) + (" …" if len(t["scope"]) > 4 else "")
    twin = f" (head-to-head with #{t['twin']})" if t.get("twin") else ""
    return (f"#{t['id']} {t['status']:<11} {t['size']} {t['kind']:<10} {t.get('tier') or '-':<9} {t['title']}{owner}{deps}"
            f"{twin}  files: {scope or '-'}")


def _tier_models(ctx: Ctx) -> tuple[str, str]:
    """The models the run's workhorse and manager seats use (for the scorecard)."""
    seats = ctx.store.seats()
    workhorse = next((s["model"] for s in seats if seat_tier(s) == "workhorse" and s.get("model")), "")
    manager = next((s["model"] for s in seats if seat_tier(s) == "manager" and s.get("model")), "")
    return workhorse, manager


def _check_owner_tier(ctx: Ctx, owner: str | None, tier: str | None) -> None:
    """A suggested owner must be able to do the task: workhorse seats take workhorse tasks, manager seats the rest."""
    if not owner or not tier:
        return
    seat = ctx.store.seat(owner) or {}
    if not seat:
        return
    workhorses = [s["name"] for s in ctx.store.seats() if seat_tier(s) == "workhorse"]
    if tier == "manager" and seat_tier(seat) == "workhorse":
        raise ToolError(f"{owner} is a workhorse seat ({model_label(seat.get('model') or '')}); manager tasks need a "
                        "manager seat. Suggest a manager seat, or make the task tier=workhorse if it is routine.")
    if tier == "workhorse" and seat_tier(seat) != "workhorse" and workhorses:
        raise ToolError(f"{owner} is a manager seat. Workhorse tasks go to the workhorse seats "
                        f"({', '.join(workhorses)}); leave "
                        "suggested_owner empty or pick one of them, or make the task tier=manager if it needs judgement.")


def tasks_view(ctx: Ctx, a: dict) -> str:
    rows = ctx.store.tasks()
    if a.get("status"):
        rows = [t for t in rows if t["status"] == a["status"]]
    if not rows:
        return "The board is empty." + (" You are the lead: create the plan with team_task_create." if ctx.role == "lead" else "")
    mine = [t for t in rows if t["owner"] == ctx.seat and t["status"] in ("in_progress", "changes")]
    out = [_task_line(t) for t in rows]
    if mine:
        out.append(f"\nYour open task: #{mine[0]['id']} ({mine[0]['status']}).")
    return "\n".join(out)


def task_detail(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"], owner_only=False)
    parts = [
        _task_line(t),
        f"Spec:\n{t['spec']}",
        f"Acceptance criteria:\n{t['acceptance'] or '-'}",
    ]
    if t["notes"]:
        parts.append(f"Handover notes:\n{clip(t['notes'], 3000)}")
    if t["review_notes"]:
        parts.append(f"Latest review:\n{clip(t['review_notes'], 3000)}")
    if t["block_reason"]:
        parts.append(f"Blocked because: {t['block_reason']}")
    return "\n\n".join(parts)


def status_view(ctx: Ctx, a: dict) -> str:
    st = ctx.store
    lines = [f"Phase: {_phase(ctx)}.  Goal: {clip(st.get('goal', ''), 200)}"]
    counts: dict[str, int] = {}
    for t in st.tasks():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    lines.append("Tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none yet"))
    lines.append("Seats:")
    for s in st.seats():
        task = f" on #{s['current_task']}" if s.get("current_task") else ""
        lines.append(f"  {s['name']} ({s['role']}, {seat_tier(s)}, {s['vendor']}/{s['account']}): {s['status']}{task}")
    lines.append("Accounts:")
    for acc in st.accounts():
        util = "" if acc["util_5h"] is None else f" 5h {acc['util_5h'] * 100:.0f}% (resets {hhmm(acc['reset_5h'])})"
        week = "" if acc["util_7d"] is None else f", week {acc['util_7d'] * 100:.0f}%"
        lines.append(f"  {acc['name']}: {acc['mode']}{util}{week}")
    checks = st.get("checks", [])
    lines.append("Checks: " + ("; ".join(checks) if checks else "none set (lead: team_set_checks)"))
    decisions = [m for m in st.recent_messages(200, public_only=True) if m["kind"] == "decision"][-3:]
    if decisions:
        lines.append("Recent decisions:")
        lines += ["  " + _fmt_msg(m, 300) for m in decisions]
    items = st.shared_list(8)
    if items:
        lines.append("Shared with the team (team_shared id=N): " + "; ".join(
            f"#{x['id']} {x['title']} ({x['seat']})" for x in items))
    return "\n".join(lines)


# ---------------------------------------------------------------- planning


def task_create(ctx: Ctx, a: dict) -> str:
    if _phase(ctx) in ("deliver", "done"):
        raise ToolError("The project is being delivered; no new tasks.")
    owner = a.get("suggested_owner")
    if owner and not ctx.store.seat(owner):
        raise ToolError(f"suggested_owner '{owner}' is not a seat. Seats: {[s['name'] for s in ctx.store.seats()]}")
    tier = a.get("tier") or None
    if tier and tier not in TIERS:
        raise ToolError(f"tier must be one of {TIERS}")
    moved = ""
    if tier == "workhorse":  # the record decides: a kind of work the workhorse keeps failing goes to the manager
        workhorse, manager = _tier_models(ctx)
        tier, moved = scorecard.route_tier(a.get("kind", "build"), a.get("size", "M"), tier, workhorse, manager)
        if moved and owner and seat_tier(ctx.store.seat(owner)) == "workhorse":
            owner = None
    _check_owner_tier(ctx, owner, tier)
    try:
        task_id = ctx.store.create_task(
            title=a.get("title", ""), spec=a.get("spec", ""), acceptance=a.get("acceptance", ""),
            scope=a.get("scope") or [], depends_on=a.get("depends_on") or [], size=a.get("size", "M"),
            kind=a.get("kind", "build"), suggested_owner=owner, created_by=ctx.seat, tier=tier,
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    ctx.store.event("task_created", seat=ctx.seat, task_id=task_id)
    task = ctx.store.task(task_id) or {}
    if not tier and owner:  # the default tier must not strand a task with an owner who cannot take it
        seat = ctx.store.seat(owner) or {}
        ctx.store.update_task(task_id, tier=seat_tier(seat))
        task = ctx.store.task(task_id) or {}
    return f"Created task #{task_id} ({task.get('tier', 'manager')} tier)." + (
        f" It goes to the manager tier: {moved}." if moved else "")


def task_edit(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"], owner_only=False)
    if t["status"] not in ("todo", "blocked", "changes"):
        raise ToolError(f"Task #{t['id']} is {t['status']}; only to-do, blocked or returned tasks can be edited.")
    fields = {k: a[k] for k in ("spec", "acceptance", "size", "suggested_owner", "tier") if a.get(k)}
    if "scope" in a:
        from .store import normalize_glob
        fields["scope"] = [normalize_glob(p) for p in a["scope"]]
    if "size" in fields and fields["size"] not in SIZES:
        raise ToolError(f"size must be one of {SIZES}")
    if "tier" in fields and fields["tier"] not in TIERS:
        raise ToolError(f"tier must be one of {TIERS}")
    _check_owner_tier(ctx, fields.get("suggested_owner", t.get("suggested_owner")), fields.get("tier", t.get("tier")))
    if t["status"] == "blocked" and a.get("unblock"):
        fields.update(status="todo" if not t["owner"] else "in_progress", block_reason=None)
    if not fields:
        raise ToolError("Nothing to change.")
    ctx.store.update_task(t["id"], **fields)
    return f"Task #{t['id']} updated."


def task_cancel(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"], owner_only=False)
    if t["status"] in ("merged", "cancelled"):
        raise ToolError(f"Task #{t['id']} is already {t['status']}.")
    if t["status"] in ("in_progress", "review", "approved"):
        raise ToolError(f"Task #{t['id']} is {t['status']} with {t['owner']}; ask them to release it first.")
    ctx.store.update_task(t["id"], status="cancelled", finished_at=now())
    ctx.store.post(ctx.seat, "update", f"Cancelled task #{t['id']}: {a.get('reason', '').strip()}", task_id=t["id"])
    return f"Task #{t['id']} cancelled."


def set_checks(ctx: Ctx, a: dict) -> str:
    if ctx.role != "lead" and ctx.store.get("solo_builder") != ctx.seat:
        raise ToolError("Only the lead (or the builder of a one-builder job) sets the checks.")
    cmds = [c.strip() for c in (a.get("commands") or []) if c.strip()]
    if not cmds:
        raise ToolError("Give at least one shell command (for example: 'python -m pytest -q').")
    ctx.store.set("checks", cmds)
    return "Checks saved. They run before every review and after every merge: " + "; ".join(cmds)


def plan_ready(ctx: Ctx, a: dict) -> str:
    todo = ctx.store.tasks(("todo",))
    if not todo:
        raise ToolError("Create the tasks first (team_task_create).")
    if not ctx.store.get("checks"):
        raise ToolError("Set the automated checks first (team_set_checks): the tests — for any backend, a test for "
                        "every route or handler including wrong input and error paths — plus a security check and a "
                        "lint that fit the stack. If the code does not exist yet, set the commands the foundation "
                        "task will make pass; for work without code (documents, research), a check that each "
                        "deliverable exists is enough (for example: test -s report.md). They run on every "
                        "submission and every merge.")
    summary = (a.get("summary") or "").strip()
    if not summary:
        raise ToolError("Give a short plan summary for the team and the user.")
    ctx.store.set("plan_summary", summary)
    ctx.store.set("plan_ready_at", now())
    ctx.store.post(ctx.seat, "decision", "PLAN: " + summary, urgent=True)
    return "Plan recorded. The orchestrator starts the build (after the CEO's plan review, if enabled)."


def decide(ctx: Ctx, a: dict) -> str:
    text = (a.get("text") or "").strip()
    if not text:
        raise ToolError("Empty decision.")
    msg_id = ctx.store.post(ctx.seat, "decision", text, task_id=a.get("task_id"), urgent=True)
    ctx.store.event("decision", seat=ctx.seat, task_id=a.get("task_id"), text=text)
    return f"Decision #{msg_id} recorded and sent to everyone. It is binding unless new evidence appears."


def project_done(ctx: Ctx, a: dict) -> str:
    open_tasks = ctx.store.tasks(("todo", "in_progress", "review", "approved", "changes", "blocked"))
    if open_tasks:
        ids = ", ".join(f"#{t['id']}" for t in open_tasks)
        raise ToolError(f"Tasks still open: {ids}. Finish, reassign or cancel them first.")
    report = (a.get("report") or "").strip()
    if len(report) < 40:
        raise ToolError("Write the report for the user: what was built, how to use it, what was verified, any limits.")
    ctx.store.set("done_report", report)
    ctx.store.set("done_requested_at", now())
    ctx.store.post(ctx.seat, "update", "All work is merged and verified. Handing over for the final checks.")
    return "Recorded. The orchestrator now runs the final checks and review, then delivers."


# ---------------------------------------------------------------- owner work


def task_note(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"])
    note = (a.get("note") or "").strip()
    if not note:
        raise ToolError("Empty note.")
    ctx.store.append_note(t["id"], ctx.seat, clip(note, 1500))
    ctx.store.update_seat(ctx.seat, last_progress_at=now())
    return f"Noted on task #{t['id']}."


def task_submit(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"])
    if t["status"] != "in_progress":
        raise ToolError(f"Task #{t['id']} is {t['status']}; only in-progress tasks can be submitted.")
    summary, evidence = (a.get("summary") or "").strip(), (a.get("evidence") or "").strip()
    if len(summary) < 20:
        raise ToolError("Summarise what you changed and why (at least a sentence).")
    if len(evidence) < 10:
        raise ToolError("Evidence is required: the commands you ran and what they showed (tests, a run, a screenshot path).")
    ctx.store.update_task(t["id"], status="review", summary=summary, evidence=evidence, submitted_at=now())
    ctx.store.post(ctx.seat, "update", f"Submitted task #{t['id']} for review: {clip(summary, 300)}", task_id=t["id"])
    ctx.store.event("task_submitted", seat=ctx.seat, task_id=t["id"])
    return (f"Task #{t['id']} submitted. The orchestrator commits your work, runs the checks and a fresh reviewer. "
            "End your turn now; your next assignment will arrive as a message.")


def task_block(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"])
    reason = (a.get("reason") or "").strip()
    if not reason:
        raise ToolError("Say exactly what blocks you and what would unblock you.")
    ctx.store.update_task(t["id"], status="blocked", block_reason=reason)
    ctx.store.post(ctx.seat, "blocker", f"Task #{t['id']} blocked: {reason}", task_id=t["id"], urgent=True)
    return "Recorded. The lead is notified. End your turn; you will get other work meanwhile."


def task_release(ctx: Ctx, a: dict) -> str:
    t = _require_task(ctx, a["task_id"])
    if t["status"] not in ("in_progress", "changes", "blocked"):
        raise ToolError(f"Task #{t['id']} is {t['status']}; nothing to release.")
    reason = (a.get("reason") or "").strip() or "no reason given"
    ctx.store.append_note(t["id"], ctx.seat, f"Released: {reason}")
    ctx.store.update_task(t["id"], status="todo", owner=None, suggested_owner=None)
    ctx.store.post(ctx.seat, "update", f"Released task #{t['id']}: {reason}", task_id=t["id"])
    return f"Task #{t['id']} released (your branch keeps the work so far)."


# ------------------------------------------------------------------ review


def review_submit(ctx: Ctx, a: dict) -> str:
    task_id = int(a["task_id"])
    if ctx.task_id is not None and task_id != ctx.task_id:
        raise ToolError(f"You are reviewing task #{ctx.task_id}, not #{task_id}.")
    t = _require_task(ctx, task_id, owner_only=False)
    if t["status"] != "review":
        raise ToolError(f"Task #{task_id} is {t['status']}, not awaiting review.")
    verdict = a.get("verdict")
    notes = (a.get("notes") or "").strip()
    if verdict not in ("approve", "changes"):
        raise ToolError("verdict must be 'approve' or 'changes'.")
    if verdict == "changes" and len(notes) < 20:
        raise ToolError("List the concrete problems (file, what is wrong, how to see it) so the owner can fix them.")
    ctx.store.set(f"review:{task_id}", {"verdict": verdict, "notes": notes, "by": ctx.seat, "at": now()})
    return "Review recorded. Thank you; end your turn."


def verdict(ctx: Ctx, a: dict) -> str:
    kind, value = a.get("kind"), a.get("verdict")
    notes = (a.get("notes") or "").strip()
    if kind not in ("plan", "final"):
        raise ToolError("kind must be 'plan' or 'final'.")
    if value not in ("approve", "changes"):
        raise ToolError("verdict must be 'approve' or 'changes'.")
    if value == "changes" and len(notes) < 20:
        raise ToolError("List the must-fix items, numbered and concrete.")
    set_efforts = []
    if kind == "plan":
        for item in a.get("efforts") or []:
            try:
                tid, effort = int(item.get("task_id")), str(item.get("effort", "")).lower()
                tier = str(item.get("tier") or "").lower()
            except (TypeError, ValueError, AttributeError):
                continue
            task = ctx.store.task(tid)
            if task is None or task["status"] in ("merged", "cancelled"):
                continue
            fields = {}
            if effort in EFFORT_LEVELS:
                fields["effort"] = effort
            if tier in TIERS and tier != task.get("tier"):
                fields["tier"] = tier
                owner = ctx.store.seat(task.get("suggested_owner") or "") or {}
                if owner and seat_tier(owner) != tier:
                    fields["suggested_owner"] = None  # the old suggestion cannot take the task any more
            if fields:
                ctx.store.update_task(tid, **fields)
                set_efforts.append(f"#{tid} {fields.get('effort', task.get('effort') or 'auto')} "
                                   f"({fields.get('tier', task.get('tier'))})")
    ctx.store.set(f"verdict:{kind}", {"verdict": value, "notes": notes, "by": ctx.seat, "at": now()})
    label = {"plan": "Plan review", "final": "Final review"}[kind]
    extra = f" Effort per task: {', '.join(set_efforts)}." if set_efforts else ""
    ctx.store.post(ctx.seat, "decision", f"{label}: {value.upper()}. {notes}{extra}".strip(), urgent=True)
    return "Verdict recorded and posted. End your turn."


def reply_owner(ctx: Ctx, a: dict) -> str:
    text = (a.get("text") or "").strip()
    if not text:
        raise ToolError("Write your answer to the owner.")
    if len(text) > 4000:
        raise ToolError("Keep it under 4000 characters: the owner reads it on a phone too.")
    share = (a.get("share_with_team") or "").strip()
    names = [str(n).strip().lstrip("@").lower() for n in (a.get("mention") or []) if str(n).strip()]
    team = {s["name"].lower() for s in ctx.store.seats()}
    unknown = [n for n in names if n not in team and n != "lead"]
    if unknown:
        raise ToolError(f"There is no {', '.join('@' + n for n in unknown)} on this team. The team: {_roster(ctx)}.")
    if len(share) > 2500:
        raise ToolError("Keep what you pass on to the team under 2500 characters; details go in a task note.")
    ctx.store.post(ctx.seat, "direct", text, recipient="you")
    ctx.store.event("owner_reply", seat=ctx.seat, shared=bool(share))
    if not share:
        return "Sent to the owner. Carry on with your work."
    lead = _lead(ctx)
    tags = []
    for n in ([lead] if lead else []) + [lead if n == "lead" else n for n in names]:
        if n and n != ctx.seat and n not in tags:
            tags.append(n)
    body, _, _ = _address(ctx, share)
    missing = [n for n in tags if f"@{n}" not in body.lower()]
    body += (" " + " ".join(f"@{n}" for n in missing)) if missing else ""
    seat = ctx.store.seat(ctx.seat) or {}
    ctx.store.post(ctx.seat, "update", f"From the owner (told to me directly): {body}", urgent=True,
                   task_id=seat.get("current_task"))
    return ("Sent to the owner, and passed on to the team" + (f" ({', '.join('@' + t for t in tags)})" if tags else "")
            + ". Carry on with your work.")


# -------------------------------------------------------------- sharing

SHARE_LIMIT = 60_000


def share(ctx: Ctx, a: dict) -> str:
    title = (a.get("title") or "").strip()
    note = (a.get("text") or "").strip()
    rel = (a.get("path") or "").strip()
    if not title:
        raise ToolError("Give what you share a short title (what it is and why it matters).")
    if not note and not rel:
        raise ToolError("Share a note (text) or a file from your worktree (path), or both.")
    content, path = note, None
    if rel:
        seat = ctx.store.seat(ctx.seat) or {}
        base = Path(seat.get("worktree") or os.getcwd()).resolve()
        target = (base / rel).resolve()
        if base != target and base not in target.parents:
            raise ToolError("Share files from your own worktree only (a path inside it).")
        if not target.is_file():
            raise ToolError(f"There is no file {rel} in your worktree.")
        raw = target.read_bytes()
        if len(raw) > SHARE_LIMIT:
            raise ToolError(f"{rel} is {len(raw) // 1000} KB; share files up to {SHARE_LIMIT // 1000} KB, or the "
                            "part that matters as text.")
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{rel} is not a text file; describe it in a note instead.") from None
        path = target.relative_to(base).as_posix()
        content = (note + "\n\n" if note else "") + f"--- {path} ---\n" + body
    names = [str(n).strip().lstrip("@").lower() for n in (a.get("for") or []) if str(n).strip()]
    team = {s["name"].lower() for s in ctx.store.seats()}
    lead = _lead(ctx)
    names = [lead if n == "lead" else n for n in names]
    unknown = [n for n in names if n not in team and n != "all"]
    if unknown:
        raise ToolError(f"There is no {', '.join('@' + n for n in unknown)} on this team. The team: {_roster(ctx)}.")
    sid = ctx.store.add_shared(ctx.seat, title, content, path=path, audience=names)
    first = next((line.strip() for line in (note or content).splitlines() if line.strip() and not line.startswith("---")), "")
    what = f"{path}, {content.count(chr(10)) + 1} lines" if path else "a note"
    tags = " ".join(f"@{n}" for n in names if n != ctx.seat)
    seat = ctx.store.seat(ctx.seat) or {}
    ctx.store.post(ctx.seat, "share", f"Shared #{sid} “{title}” ({what}): {clip(first, 200)} — read it with "
                   f"team_shared id={sid}. {tags}".strip(), task_id=seat.get("current_task"))
    return f"Shared as #{sid}" + (f" and {tags} notified" if tags else "") + ". Anyone can read it with team_shared."


def shared_view(ctx: Ctx, a: dict) -> str:
    sid = a.get("id")
    if sid:
        item = ctx.store.shared(int(sid))
        if item is None:
            raise ToolError(f"Nothing is shared as #{sid}. team_shared without an id lists what is shared.")
        head = f"#{item['id']} “{item['title']}” shared by {item['seat']} at {hhmm(item['ts'])}"
        return head + (f" ({item['path']})" if item["path"] else "") + ":\n\n" + clip(item["content"], 20_000)
    items = ctx.store.shared_list(40)
    if not items:
        return "Nothing is shared yet. Share interfaces, commands that work, pitfalls and useful files with team_share."
    return "Shared with the team (read one with team_shared id=N):\n" + "\n".join(
        f"#{x['id']} {x['title']} — {x['seat']}, {hhmm(x['ts'])}" + (f", {x['path']}" if x["path"] else "")
        + (f", for {', '.join('@' + n for n in x['audience'].split(','))}" if x["audience"] else "") for x in items)


# ---------------------------------------------------------- escalate/lessons


def escalate(ctx: Ctx, a: dict) -> str:
    question = (a.get("question") or "").strip()
    if len(question) < 20:
        raise ToolError("State the question, the options, and the evidence for each.")
    used = len(ctx.store.events("escalation"))
    cap = int(ctx.settings.get("max_escalations", 4))
    if used >= cap:
        raise ToolError("The CEO's ruling budget for this run is used up. The lead decides (team_decide).")
    ctx.store.event("escalation", seat=ctx.seat, task_id=a.get("task_id"), question=question)
    ctx.store.post(ctx.seat, "question", f"Escalated to the CEO: {clip(question, 600)}", task_id=a.get("task_id"))
    return "Escalated. A binding ruling will be posted to the chat. Continue with anything not affected by it."


def lesson_add(ctx: Ctx, a: dict) -> str:
    text = (a.get("lesson") or "").strip()
    if len(text) < 15:
        raise ToolError("Write the lesson as a reusable rule: 'When X, do Y, because Z'.")
    project = ctx.store.get("project_name", "")
    status = lessons_mod.add(a.get("category") or "process", text, evidence=a.get("evidence") or "",
                             source=f"agent:{ctx.seat}", project=project)
    ctx.store.post(ctx.seat, "lesson", clip(text, 1500))
    return f"Lesson {status}. Future teams will see it."


def lessons_view(ctx: Ctx, a: dict) -> str:
    items = lessons_mod.search(a.get("query") or "", limit=int(a.get("max") or 10))
    if not items:
        return "No lessons match."
    return "\n".join(f"- [{x['category']}, seen {x['weight']}x] {x['text']}" for x in items)


# ------------------------------------------------------------------ registry

TOOLS: list[Tool] = [
    Tool("team_chat_post",
         "Post to the team chat (the whole team and the owner see it). Start with @name for the person you need "
         "(@lead for the lead, @all only when everyone must act): only named agents are woken. Short and "
         "self-contained: what, where (file, function, command), why, what you need. No acknowledgements — silence "
         "means agreement. Kinds: update, question, answer, blocker (always allowed), concern (planning round only, "
         "once). Messages are budgeted: work first, talk second.",
         _obj({"text": S, "kind": {"type": "string", "enum": list(CHAT_KINDS)}, "task_id": I}, ["text"]),
         chat_post),
    Tool("team_chat_read", "Read unread group-chat messages (marks them read).",
         _obj({"max": I}), chat_read, footer=False),
    Tool("team_tasks", "Show the task board (optionally filtered by status).",
         _obj({"status": S}), tasks_view),
    Tool("team_task_detail", "Show one task in full: spec, acceptance criteria, handover notes, latest review.",
         _obj({"task_id": I}, ["task_id"]), task_detail),
    Tool("team_status", "Team overview: phase, seats, account usage modes, checks, recent decisions.",
         _obj({}), status_view),
    Tool("team_task_create",
         "LEAD ONLY. Create a task. Give a precise spec, acceptance criteria, the file scope it may edit "
         "(paths/globs; tasks with overlapping scopes never run at the same time), dependencies, size "
         "(S ≈ <15 min, M ≈ <45 min, L = split it if you can), the tier (workhorse = routine, fully specified work "
         "for the workhorse seats; manager = work that needs high intelligence, for the manager seats) and "
         "optionally a suggested owner of that tier.",
         _obj({"title": S, "spec": S, "acceptance": S, "scope": LIST_S, "depends_on": LIST_I,
               "size": {"type": "string", "enum": list(SIZES)}, "kind": {"type": "string", "enum": list(KINDS)},
               "tier": {"type": "string", "enum": list(TIERS)}, "suggested_owner": S},
              ["title", "spec", "acceptance", "scope"]),
         task_create, roles=("lead",)),
    Tool("team_task_edit", "LEAD ONLY. Change a to-do/blocked/returned task (spec, acceptance, scope, size, tier, owner, "
         "unblock).",
         _obj({"task_id": I, "spec": S, "acceptance": S, "scope": LIST_S,
               "size": {"type": "string", "enum": list(SIZES)}, "tier": {"type": "string", "enum": list(TIERS)},
               "suggested_owner": S, "unblock": {"type": "boolean"}},
              ["task_id"]),
         task_edit, roles=("lead",)),
    Tool("team_task_cancel", "LEAD ONLY. Cancel a task that is no longer needed.",
         _obj({"task_id": I, "reason": S}, ["task_id", "reason"]), task_cancel, roles=("lead",)),
    Tool("team_set_checks",
         "LEAD (or the builder of a one-builder job) ONLY. Set the shell commands that prove the project works "
         "(tests, build, lint). The orchestrator runs them before each review and after each merge.",
         _obj({"commands": LIST_S}, ["commands"]), set_checks, roles=("lead", "member")),
    Tool("team_plan_ready", "LEAD ONLY. Declare the plan complete (after creating the tasks) with a short summary.",
         _obj({"summary": S}, ["summary"]), plan_ready, roles=("lead",)),
    Tool("team_decide", "LEAD or CEO ONLY. Record a binding decision and send it to everyone.",
         _obj({"text": S, "task_id": I}, ["text"]), decide, roles=("lead", "ceo")),
    Tool("team_project_done",
         "LEAD ONLY. When every task is merged: declare the project finished with a plain-language report for the "
         "user (what was built, how to use it, what was verified, known limits). No code in the report.",
         _obj({"report": S}, ["report"]), project_done, roles=("lead",)),
    Tool("team_task_note",
         "Record progress on your task: decisions made, what is done, what is next. This is the handover if "
         "someone else must continue, so keep it concrete.",
         _obj({"task_id": I, "note": S}, ["task_id", "note"]), task_note, roles=("lead", "member")),
    Tool("team_task_submit",
         "Hand in your finished task with a summary and EVIDENCE (commands run and their results). "
         "Do not submit untested work.",
         _obj({"task_id": I, "summary": S, "evidence": S}, ["task_id", "summary", "evidence"]),
         task_submit, roles=("lead", "member")),
    Tool("team_task_block", "Mark your task blocked (say what blocks it and what would unblock it).",
         _obj({"task_id": I, "reason": S}, ["task_id", "reason"]), task_block, roles=("lead", "member")),
    Tool("team_task_release", "Give your task back to the board (the work so far stays on its branch).",
         _obj({"task_id": I, "reason": S}, ["task_id", "reason"]), task_release, roles=("lead", "member")),
    Tool("team_review_submit",
         "REVIEWER ONLY. Approve, or request changes with a concrete list of problems.",
         _obj({"task_id": I, "verdict": {"type": "string", "enum": ["approve", "changes"]}, "notes": S},
              ["task_id", "verdict", "notes"]),
         review_submit, roles=("reviewer", "ceo")),
    Tool("team_verdict", "CEO ONLY. Record your plan or final review verdict (posted to the chat). With a plan "
         "verdict, also set each task's tier and how hard its builder should think: "
         "efforts=[{task_id, effort, tier}], effort one of low, medium, high, xhigh, max; tier workhorse or manager.",
         _obj({"kind": {"type": "string", "enum": ["plan", "final"]},
               "verdict": {"type": "string", "enum": ["approve", "changes"]}, "notes": S,
               "efforts": {"type": "array", "items": {"type": "object", "properties": {
                   "task_id": I, "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                   "tier": {"type": "string", "enum": list(TIERS)}},
                   "required": ["task_id", "effort"], "additionalProperties": False}}},
              ["kind", "verdict", "notes"]),
         verdict, roles=("ceo",)),
    Tool("team_reply_owner",
         "Answer the owner's private message to you. text: your answer (only the owner sees it) in plain, "
         "non-technical words — what you are doing, what you found, what you need. share_with_team: REQUIRED "
         "decision — if the owner's message changes the plan, the scope, a decision or anyone else's work, write "
         "exactly what the team must know (the lead and whoever you name in mention are notified); keeping such an "
         "instruction to yourself is a fault. Leave it \"\" only when nothing affects anyone else (a question about "
         "your own work, an opinion). Then carry on with your work.",
         _obj({"text": S, "share_with_team": S, "mention": LIST_S}, ["text", "share_with_team"]), reply_owner,
         roles=("lead", "member")),
    Tool("team_share",
         "Share something a teammate needs: an interface or data format, a command that works, a pitfall, what you "
         "learned, or a file from your worktree (path; text files up to 60 KB, shared as a snapshot). Name who "
         "needs it in for (they are notified). Short title; the note says why it matters.",
         _obj({"title": S, "text": S, "path": S, "for": LIST_S}, ["title"]), share, roles=("lead", "member")),
    Tool("team_shared", "List what teammates have shared, or read one item in full (id).",
         _obj({"id": I}), shared_view, roles=("lead", "member", "reviewer", "ceo")),
    Tool("team_escalate",
         "Ask the CEO model for ONE binding ruling on a disagreement or a hard design choice. Include the options "
         "and the evidence. Use rarely; tests and experiments beat rulings.",
         _obj({"question": S, "task_id": I}, ["question"]), escalate, roles=("lead", "member")),
    Tool("team_lesson_add",
         "Save a lesson for all future teams (usage, speed, quality, models, subagents, tooling, process, errors): "
         "a reusable rule with the evidence behind it.",
         _obj({"category": S, "lesson": S, "evidence": S}, ["lesson"]), lesson_add),
    Tool("team_lessons", "Search the shared experience memory.", _obj({"query": S, "max": I}), lessons_view),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tools_for(role: str) -> list[Tool]:
    return [t for t in TOOLS if role in t.roles]


def call(ctx: Ctx, name: str, args: dict) -> tuple[str, bool]:
    """Run a tool; returns (text, is_error). Never raises for agent mistakes."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None or ctx.role not in tool.roles:
        return f"Tool {name} is not available to the {ctx.role} role.", True
    try:
        text, err = tool.handler(ctx, args or {}), False
    except ToolError as exc:
        text, err = str(exc), True
    except (KeyError, ValueError, TypeError) as exc:
        text, err = f"Bad arguments for {name}: {exc}", True
    ctx.store.update_seat(ctx.seat, last_event_at=now()) if ctx.store.seat(ctx.seat) else None
    if tool.footer:
        extra = digest(ctx)
        if extra:
            text = f"{text}\n\n{extra}"
    return text, err
