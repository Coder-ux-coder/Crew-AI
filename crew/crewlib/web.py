"""A small live view of a run: the group chat, who is doing what, usage, and the
final report. Plain language only — no code, no git. (The full app with voice,
side-panel browser and settings is the next phase; this is its data source.)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import Store
from .util import now

FRIENDLY = {
    "todo": "waiting", "in_progress": "being built", "review": "being checked", "approved": "approved",
    "changes": "being improved", "merged": "done", "blocked": "needs a decision", "cancelled": "dropped",
}
MODES = {"spend": "plenty left", "normal": "on track", "conserve": "saving", "parked": "resting", "unknown": ""}


def friendly_activity(label: str, status: str) -> str:
    """Say what an agent is doing without showing commands or code."""
    l = (label or "").strip()
    if status == "standby":
        return "standing by to check the work"
    if status in ("idle", "stopped", "down", "waiting", "starting") and not l:
        return {"idle": "ready", "stopped": "stopped", "down": "unavailable", "waiting": "waiting for its subscription",
                "starting": "starting"}.get(status, status)
    low = l.lower()
    if low.startswith("bash"):
        if any(w in low for w in ("test", "pytest", "unittest", "jest", "vitest")):
            return "running the tests"
        if any(w in low for w in ("git ", "ls ", "cat ", "find ", "tree")):
            return "looking around the project"
        if any(w in low for w in ("pip ", "npm ", "install")):
            return "installing tools"
        return "running a step"
    for key, text in (("read", "reading the project"), ("grep", "searching the project"), ("glob", "searching the project"),
                      ("edit", "writing code"), ("write", "writing code"), ("notebook", "writing code"),
                      ("websearch", "researching online"), ("webfetch", "researching online"),
                      ("task", "working with a helper"), ("agent", "working with a helper"),
                      ("team_chat", "talking to the team"), ("team_task_submit", "handing in work"),
                      ("team_task", "updating the plan"), ("team_reply_owner", "answering you"),
                      ("team_", "coordinating"), ("toolsearch", "getting ready"),
                      ("writing", "thinking"), ("tool result", "working")):
        if low.startswith(key):
            return text
    return "working"


PHASES = {"refine": "Understanding your request", "plan": "Planning", "build": "Building",
          "deliver": "Final checks", "done": "Finished", "stopped": "Stopped", "failed": "Stopped (error)"}


def state(store: Store, run_dir: Path, after: int) -> dict:
    brief = store.get("brief", {}) or {}
    tasks = store.tasks()
    report = run_dir / "REPORT.md"
    return {
        "title": brief.get("title") or store.get("project_name") or "Your project",
        "phase": PHASES.get(store.get("phase", "refine"), store.get("phase", "")),
        "done": store.get("phase") in ("done", "stopped", "failed"),
        "progress": [sum(1 for t in tasks if t["status"] == "merged"),
                     sum(1 for t in tasks if t["status"] != "cancelled")],
        "messages": [{"id": m["id"], "t": m["ts"], "who": m["sender"], "kind": m["kind"], "text": m["text"],
                      "to": m.get("recipient"), "ref": m.get("ref"), "original": m.get("original")}
                     for m in store.messages_after(after, 300)],
        "seats": [{"name": s["name"], "role": s["role"], "status": s["status"],
                   "doing": friendly_activity(s["note"] or "", s["status"]),
                   "task": s["current_task"], "account": s["account"]} for s in store.seats()],
        "accounts": [{"name": a["name"], "mode": MODES.get(a["mode"] or "", a["mode"] or ""), "raw": a["mode"],
                      "util": a["util_5h"], "reset": a["reset_5h"]} for a in store.accounts()],
        "tasks": [{"id": t["id"], "title": t["title"], "status": FRIENDLY.get(t["status"], t["status"]),
                   "who": t["owner"] or ""} for t in tasks],
        "report": report.read_text(encoding="utf-8") if report.is_file() else "",
        "now": now(),
    }


def serve(run_dir: Path, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    store = Store(run_dir / "team.db")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            return

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif url.path == "/api/state":
                after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                self._json(state(store, run_dir, after))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if self.path == "/api/say":
                text = (data.get("text") or "").strip()
                if text:
                    store.post("you", "human", text[:4000], urgent=True)
                self._json({"ok": bool(text)})
            elif self.path == "/api/stop":
                store.set("stop_requested", now())
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="crew-web").start()
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crew</title>
<style>
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e6e4df;--accent:#3b5bdb;--ok:#2b8a3e;--warn:#e67700;--bad:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1c1c1f;--ink:#ececf0;--mute:#9a9aa3;--line:#2c2c31;--accent:#7c9cff;--ok:#51cf66;--warn:#ffa94d;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 20px 10px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline}
h1{font-size:20px;margin:0}.phase{color:var(--mute)}.bar{flex-basis:100%;height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .6s}
main{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;padding:0 20px 20px}
@media (max-width:820px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
h2{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
#chat{height:62vh;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.m{padding:8px 10px;border-radius:10px;background:var(--bg);word-wrap:break-word;white-space:pre-wrap}
.m b{font-weight:600}.m .k{color:var(--mute);font-size:12px;margin-left:6px}.m.you{background:color-mix(in srgb,var(--accent) 14%,var(--panel))}
.m.decision{border-left:3px solid var(--accent)}.m.blocker{border-left:3px solid var(--bad)}.m.system{color:var(--mute)}
form{display:flex;gap:8px;margin-top:10px}input{flex:1;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
button{padding:10px 14px;border-radius:10px;border:0;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}
.mute{color:var(--mute);font-size:13px}.pill{font-size:12px;padding:1px 8px;border-radius:99px;background:var(--bg);white-space:nowrap}
.meter{height:5px;background:var(--line);border-radius:3px;margin-top:4px}.meter i{display:block;height:100%;border-radius:3px}
#report{white-space:pre-wrap}
</style></head><body>
<header><h1 id="title">Your project</h1><span class="phase" id="phase"></span><div class="bar"><i id="prog"></i></div></header>
<main>
<section><h2>Team chat</h2><div id="chat"></div>
<form id="f"><input id="say" placeholder="Message the team…" autocomplete="off"><button>Send</button></form></section>
<div style="display:grid;gap:16px;align-content:start">
<section id="rep" hidden><h2>Result</h2><div id="report"></div></section>
<section><h2>Team</h2><div id="seats"></div></section>
<section><h2>Work</h2><div id="tasks"></div></section>
<section><h2>Subscriptions</h2><div id="accs"></div></section>
</div></main>
<script>
let after=0;const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const hm=t=>new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
async function tick(){
  try{const r=await fetch('/api/state?after='+after);const s=await r.json();
  $('title').textContent=s.title;$('phase').textContent=s.phase;document.title=s.title+' · Crew';
  const [d,t]=s.progress;$('prog').style.width=(t?Math.round(100*d/t):0)+'%';
  const c=$('chat');const stick=c.scrollTop+c.clientHeight>=c.scrollHeight-40;
  for(const m of s.messages){after=m.id;const div=document.createElement('div');
    div.className='m '+(m.who==='you'?'you ':'')+m.kind;
    div.innerHTML='<b>'+esc(m.who==='crew'?'Crew':m.who)+'</b><span class="k">'+hm(m.t)+(m.kind!=='update'&&m.kind!=='human'&&m.kind!=='system'?' · '+esc(m.kind):'')+'</span>\n'+esc(m.text);
    c.appendChild(div);}
  if(stick)c.scrollTop=c.scrollHeight;
  $('seats').innerHTML=s.seats.map(x=>'<div class="row"><div><b>'+esc(x.name)+'</b> <span class="mute">'+esc(x.role)+'</span><div class="mute">'+esc(x.doing||x.status)+'</div></div><span class="pill">'+(x.task?'on a task':(x.status==='busy'?'busy':'free'))+'</span></div>').join('');
  $('tasks').innerHTML=s.tasks.length?s.tasks.map(x=>'<div class="row"><span>'+esc(x.title)+'</span><span class="pill">'+esc(x.status)+'</span></div>').join(''):'<span class="mute">The plan is being made…</span>';
  $('accs').innerHTML=s.accounts.map(a=>{const u=a.util==null?null:Math.round(a.util*100);const col=a.raw==='parked'?'var(--bad)':a.raw==='conserve'?'var(--warn)':'var(--ok)';
    return '<div class="row"><div style="flex:1"><b>'+esc(a.name)+'</b> <span class="mute">'+esc(a.mode)+(a.reset&&u!=null?' · resets '+hm(a.reset):'')+'</span><div class="meter"><i style="width:'+(u??0)+'%;background:'+col+'"></i></div></div><span class="pill">'+(u==null?'—':u+'%')+'</span></div>'}).join('');
  if(s.report){$('rep').hidden=false;$('report').textContent=s.report;}
  }catch(e){}
  setTimeout(tick,1500);}
$('f').onsubmit=async e=>{e.preventDefault();const v=$('say').value.trim();if(!v)return;$('say').value='';
  await fetch('/api/say',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:v})});};
tick();
</script></body></html>
"""
