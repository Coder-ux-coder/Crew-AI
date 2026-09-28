"""The prompt writer for chats and new projects: the owner's words (often dictated) rewritten as a clear, precise
prompt. It works in the message box: the owner sees the result before sending, or — with "Improve my messages
automatically" on — the message is sent as written up. Nothing is changed behind the owner's back.
(Messages to a team that is working go through the project's own prompt writer; see the orchestrator.)"""

from __future__ import annotations

from crewlib import config as cfgmod, prompts
from crewlib.agents import ClaudeSetup, run_once_claude
from crewlib.util import Redactor, clip, crew_home

from . import settings as settings_mod

READERS = {
    "claude": "Claude, an AI assistant, in a chat with the owner",
    "codex": "ChatGPT, an AI assistant, in a chat with the owner",
    "team": "a team of AI engineers who will plan and build it (the owner's request for a new project)",
}


def improve(chats, text: str, reader: str = "claude", recent: str = "") -> dict:
    """{'text': the written-up message, 'changed': bool}. Raises ValueError with a plain reason."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write or say your message first.")
    if len(text) > 12000:
        raise ValueError("That message is too long for the prompt writer (12,000 characters at most).")
    cfg = cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None)
    account = chats.pick_account(cfg, "claude")
    if account is None:
        raise ValueError("The prompt writer needs a Claude subscription (Settings → Subscriptions).")
    work = crew_home() / "writer"
    work.mkdir(parents=True, exist_ok=True)
    setup = ClaudeSetup(model=cfg.models.work, effort="low", work_model=cfg.models.work, permission_mode="default",
                        run_dir=work, extra_env={})
    context = f"Reader: {READERS.get(reader, READERS['claude'])}."
    if recent:
        context += f"\n\nThe conversation so far (latest last):\n{clip(recent, 3000)}"
    res = run_once_claude(prompts.writer_prompt(text, context), seat="writer", role="member", account=account,
                          workdir=work, setup=setup, redact=Redactor(), json_schema=prompts.WRITER_SCHEMA,
                          read_only=True, timeout=180, with_team_tools=False)
    out = res.structured if isinstance(res.structured, dict) else {}
    better = str(out.get("message") or "").strip()
    if res.is_error or not better:
        raise ValueError("The prompt writer could not help just now" + (f" ({clip(res.text, 160)})." if res.text else "."))
    return {"text": better, "changed": better != text}
