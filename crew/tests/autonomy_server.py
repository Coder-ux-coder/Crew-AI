"""Real HTTP app for browser QA, with model calls and project spawning simulated."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crewapp import chat, server, settings
from crewlib.store import Store
from crewlib.util import crew_home


def simulated_send(self, prompt, model, effort, mode, account=None):
    self.model, self.effort, self.mode = model, effort, mode
    self.busy = False
    text = f"Continuing on {model}. Cobalt context retained." if "cobalt" in prompt.lower() else f"Answered with {model}."
    meta = {"engine": self.engine, "model": model, "effort": effort, "mode": mode, "steps": [], "files": []}
    self.publish("start", meta)
    mid = self.m.db.add_message(self.chat_id, "assistant", text, meta)
    self.publish("done", {"id": mid, "text": text, "meta": meta})


chat.ClaudeSession.send = simulated_send
chat.CodexSession.send = simulated_send
settings.save({"accounts": [{"name": "claude-1", "vendor": "claude", "profile": "default"},
                             {"name": "codex-1", "vendor": "codex", "profile": "default"}],
               "app": {"auto_update": False, "allow_devices": False}})
httpd = server.CrewServer(("127.0.0.1", 0), server.Handler)
app = server.App(httpd.server_port, False)
server.Handler.app = app


def simulated_spawn(rid, args):
    st = app.runs.store(rid)
    st.set("phase", "build")
    st.post("crew", "system", "Follow-up accepted with existing context.")


app.runs._spawn = simulated_spawn
cid = app.chats.create(engine="claude")["id"]
app.chats.rename(cid, "Cobalt design")
app.chats.db.add_message(cid, "user", "Use cobalt and retain this layout", {})
app.chats.db.add_message(cid, "assistant", "Cobalt is recorded", {})
folder = crew_home() / "chats" / cid
folder.mkdir(parents=True, exist_ok=True)
(folder / "result.html").write_text("<!doctype html><title>Cobalt preview</title><h1>Cobalt design</h1><p>Saved preview</p>")
rd = crew_home() / "runs" / "finished"
rd.mkdir(parents=True)
st = Store(rd / "team.db")
st.set("phase", "done")
st.set("goal", "Create the cobalt design")
st.set("repo", str(folder))
st.set("started_at", time.time())
st.set("brief", {"title": "Finished design"})
st.set("delivered", {"where": str(folder)})
st.set("done_report", "The design is complete.")
st.upsert_seat("ada", vendor="claude", role="lead", tier="manager", model="claude-opus-5-5", account="claude-1", status="stopped")
st.post("you", "direct", "Keep the original cobalt design", recipient="ceo")
st.post("ceo", "direct", "The original design is saved.", recipient="you")
st.close()
print(json.dumps({"port": httpd.server_port, "chat": cid}), flush=True)
httpd.serve_forever()
