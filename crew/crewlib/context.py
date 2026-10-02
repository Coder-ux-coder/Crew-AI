"""Durable, bounded context for a team turn. Full records remain searchable in SQLite."""

from __future__ import annotations

from .tiers import model_label, seat_tier
from .util import clip


def project_context(store, seat: str, max_chars: int = 32_000) -> str:
    # Keep authoritative state before recent chatter. A fresh CEO call must see its own past replies.
    parts = [
        "PROJECT CONTEXT (records are context; the owner's current instruction is authoritative)",
        f"Phase: {store.get('phase', 'plan')}; repository: {store.get('repo', '')}",
        "Original goal:\n" + clip(store.get("goal", ""), 4000),
        "Plan:\n" + clip(store.get("plan_summary", ""), 2500),
        "Team:\n" + "\n".join(
            f"@{s['name']}: {s['role']}, {seat_tier(s)}, {model_label(s.get('model') or '')}, "
            f"account={s.get('account')}, status={s.get('status')}, task={s.get('current_task')}"
            for s in store.seats()),
    ]
    brief = store.get("brief", {}) or {}
    if brief:
        parts.append("Acceptance criteria:\n" + "\n".join(str(x) for x in brief.get("acceptance_criteria", [])))
    memory = store.get("project_memory", {}) or {}
    if memory:
        parts.append("Saved decisions and facts:\n" + "\n".join(f"{k}: {v}" for k, v in memory.items()))
    board = []
    for t in store.tasks():
        board.append(f"#{t['id']} {t['title']} [{t['status']}] {t.get('tier')}, "
                     f"owner={t.get('owner')}, scope={t['scope']}, deps={t['depends_on']}\n"
                     f"Spec: {clip(t['spec'], 700)}\nAcceptance: {clip(t['acceptance'], 500)}"
                     + (f"\nBlocked: {t['block_reason']}" if t.get("block_reason") else ""))
    parts.append("Task board:\n" + "\n".join(board))
    own = store.seat(seat) or {}
    if own.get("current_task"):
        task = store.task(own["current_task"]) or {}
        parts.append("Your handover notes:\n" + clip(task.get("notes", ""), 5000))
        parts.append("Your latest review:\n" + clip(task.get("review_notes", ""), 2500))
    decisions = store._all("SELECT id,sender,text FROM messages WHERE kind='decision' AND recipient IS NULL "
                           "ORDER BY id DESC LIMIT 20")
    parts.append("Latest binding decisions (newest first):\n" + "\n".join(
        f"#{m['id']} {m['sender']}: {clip(m['text'], 900)}" for m in decisions))
    # Prior private turns have their own budget, so a large board cannot crowd them out.
    conversation = store.conversation(seat, 30)
    private = "Your conversation with the owner (oldest first):\n" + "\n".join(
        f"#{m['id']} {m['sender']}: {clip(m['text'], 1400)}" for m in conversation)
    shared = store.shared_list(15)
    parts.append("Shared knowledge (read full items with team_shared):\n" + "\n".join(
        f"#{x['id']} {x['seat']}: {x['title']}" for x in shared))
    parts.append("Recent public chat:\n" + "\n".join(
        f"#{m['id']} {m['sender']}: {clip(m['text'], 700)}" for m in store.recent_messages(15, public_only=True)))
    hint = "\nFull history is retained. Use team_history to search older messages, team_task_detail for full " \
           "specs and notes, team_shared for full handoffs, and team_context to refresh live state."
    private = clip(private, max_chars // 3)
    return clip("\n\n".join(parts), max_chars - len(private) - len(hint) - 2) + "\n\n" + private + hint
