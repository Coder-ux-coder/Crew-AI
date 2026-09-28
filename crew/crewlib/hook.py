"""Claude Code hook: push urgent team messages into a working agent's context.

Registered for PostToolUse. After each tool call it checks the team store for
unread messages that are urgent (a decision, a blocker, the human) or that
mention this seat, and hands them to the model as additionalContext. Cheap
(one SQLite query) and silent when there is nothing to say.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    db, seat = os.environ.get("CREW_DB"), os.environ.get("CREW_SEAT")
    if not db or not seat:
        return
    from .store import Store
    from .tools import _fmt_msg, _mentions, owner_words

    store = Store(db)
    try:
        seen = int(store.get(f"hook_seen:{seat}", 0) or 0)
        cursor = int((store.seat(seat) or {}).get("chat_cursor") or 0)
        fresh = [m for m in store.messages_after(max(seen, cursor), 200)
                 if m["sender"] != seat and m.get("recipient") in (None, seat) and m["kind"] not in ("draft", "drafted")
                 and (m["urgent"] or m.get("recipient") == seat or _mentions(m["text"], seat))]
        if not fresh:
            return
        store.set(f"hook_seen:{seat}", fresh[-1]["id"])
        direct = store.owner_asks(seat, fresh)
        team = [m for m in fresh if not (m["sender"] == "you" and m.get("recipient") == seat)]
        if not direct and not team:
            return
        parts = []
        if direct:
            store.set(f"owner_q:{seat}", direct[-1]["id"])  # the orchestrator makes sure the owner gets an answer
            parts.append("THE OWNER MESSAGED YOU DIRECTLY (only you see this):\n"
                         + "\n".join(f"- {owner_words(m)}" for m in direct[-3:])
                         + "\nAnswer now with team_reply_owner, in plain words, then carry on with your work.")
        if team:
            parts.append("Urgent team chat messages arrived while you were working (read them now; decisions are "
                         "binding):\n" + "\n".join(_fmt_msg(m, 600) for m in team[-5:]))
        context = "\n\n".join(parts)
        event = payload.get("hook_event_name") or "PostToolUse"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))
    finally:
        store.close()


if __name__ == "__main__":
    main()
