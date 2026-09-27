"""Team projects started from the app: each runs as its own background process
(so the app can close and reopen without disturbing it) and is observed through
its SQLite store."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from crewlib.cli import new_run_id
from crewlib.store import Store
from crewlib.util import crew_home, now
from crewlib.web import PHASES, friendly_activity, state as run_state

CREW_ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ("refine", "plan", "build", "deliver")
PREVIEW_CANDIDATES = ("index.html", "dist/index.html", "build/index.html", "public/index.html", "site/index.html",
                      "docs/index.html", "web/index.html")


def runs_dir() -> Path:
    path = crew_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_dir(run_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        return None
    path = runs_dir() / run_id
    return path if (path / "team.db").is_file() or path.is_dir() else None


class RunManager:
    def __init__(self, extra_env: dict[str, str] | None = None):
        self.extra_env = extra_env or {}
        self.procs: dict[str, subprocess.Popen] = {}
        self._stores: dict[str, Store] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ processes

    def _spawn(self, run_id: str, args: list[str]) -> None:
        run_dir = runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **self.extra_env, "PYTHONPATH": str(CREW_ROOT), "CREW_HOME": str(crew_home()),
               "NO_COLOR": "1"}
        env.pop("CLAUDECODE", None)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200 | 0x08000000  # new process group, no console window
        else:
            kwargs["start_new_session"] = True
        log = open(run_dir / "app-run.log", "ab")
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "crewlib", *args], cwd=str(CREW_ROOT), env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        self.procs[run_id] = proc

    def start(self, request: str, repo: str | None = None, mode: str | None = None, hours=None) -> str:
        request = (request or "").strip()
        if len(request) < 3:
            raise ValueError("Tell the team what you want first.")
        run_id = new_run_id(request)
        args = ["start", request, "--headless", "--run-id", run_id]
        if repo:
            args += ["--repo", repo]
        if mode in ("auto", "solo", "team"):
            args += ["--mode", mode]
        try:
            hours = float(hours or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if hours > 0:  # only when the owner set a timer; otherwise the team works until it is done
            args += ["--max-hours", f"{min(hours, 168):g}"]
        self._spawn(run_id, args)
        (runs_dir() / "LATEST").write_text(run_id)
        return run_id

    def resume(self, run_id: str) -> None:
        if self.running(run_id):
            return
        self._spawn(run_id, ["resume", run_id, "--headless"])

    def running(self, run_id: str) -> bool:
        proc = self.procs.get(run_id)
        return proc is not None and proc.poll() is None

    # ---------------------------------------------------------------- state

    def store(self, run_id: str) -> Store | None:
        run_dir = _run_dir(run_id)
        if run_dir is None or not (run_dir / "team.db").is_file():
            return None
        with self._lock:
            if run_id not in self._stores:
                self._stores[run_id] = Store(run_dir / "team.db")
            return self._stores[run_id]

    def stop(self, run_id: str) -> bool:
        st = self.store(run_id)
        if st is None:
            return False
        st.set("stop_requested", now())
        return True

    def say(self, run_id: str, text: str) -> bool:
        st = self.store(run_id)
        if st is None or not text.strip():
            return False
        st.post("you", "human", text.strip()[:4000], urgent=True)
        return True

    def project_dir(self, run_id: str) -> Path | None:
        st = self.store(run_id)
        repo = st.get("repo") if st else None
        return Path(repo) if repo else None

    def preview(self, run_id: str) -> dict:
        folder = self.project_dir(run_id)
        if folder is None or not folder.is_dir():
            return {}
        for rel in PREVIEW_CANDIDATES:
            if (folder / rel).is_file():
                return {"kind": "web", "url": f"/files/run/{run_id}/{rel}"}
        if (folder / "README.md").is_file():
            return {"kind": "doc", "url": f"/files/run/{run_id}/README.md"}
        return {}

    def state(self, run_id: str, after: int = 0) -> dict | None:
        st = self.store(run_id)
        if st is None:
            return None
        data = run_state(st, runs_dir() / run_id, after)
        phase = st.get("phase", "refine")
        running = self.running(run_id) or (phase in ACTIVE and self._recent(st))
        tasks = st.tasks()
        data.update(id=run_id, running=running, raw_phase=phase, mode=st.get("mode") or "", preview=self.preview(run_id),
                    folder=str(self.project_dir(run_id) or ""), started=st.get("started_at"),
                    request=st.get("goal", ""), timer=float(st.get("max_hours") or 0),
                    agents=agents_view(st), estimate=estimate(st, tasks, running))
        by_id = {t["id"]: t for t in tasks}
        for t in data["tasks"]:
            full = by_id.get(t["id"]) or {}
            t.update(effort=full.get("effort") or "", size=full.get("size") or "", kind=full.get("kind") or "",
                     tokens=full.get("tokens") or 0, raw=full.get("status") or "")
        return data

    @staticmethod
    def _recent(st: Store) -> bool:
        last = st.recent_messages(1)
        seats = st.seats()
        latest = max([m["ts"] for m in last] + [s.get("last_event_at") or 0 for s in seats] + [0])
        return now() - latest < 600

    def list(self) -> list[dict]:
        out = []
        for run_dir in runs_dir().iterdir():
            if not (run_dir / "team.db").is_file():
                continue
            try:
                out.append(self._summary(run_dir))
            except Exception as exc:  # a project that is still being created must not hide the others
                print(f"run {run_dir.name}: {exc}")
        return sorted(out, key=lambda r: r["started"] or 0, reverse=True)

    def _summary(self, run_dir: Path) -> dict:
        st = self.store(run_dir.name)
        brief = st.get("brief", {}) or {}
        tasks = st.tasks()
        phase = st.get("phase", "refine")
        return {
            "id": run_dir.name,
            "title": brief.get("title") or (st.get("goal", "") or run_dir.name)[:80],
            "phase": PHASES.get(phase, phase), "raw_phase": phase,
            "running": self.running(run_dir.name) or (phase in ACTIVE and self._recent(st)),
            "done": phase == "done",
            "progress": [sum(1 for t in tasks if t["status"] == "merged"),
                         sum(1 for t in tasks if t["status"] != "cancelled")],
            "started": st.get("started_at") or run_dir.stat().st_mtime,
            "mode": st.get("mode") or "",
            "preview": self.preview(run_dir.name),
        }


# ------------------------------------------------------------------ agents panel and estimates

PRODUCTS = {"claude": "Claude", "codex": "ChatGPT"}


def agent_title(name: str, role: str, task=None) -> str:
    """A readable name for one-off agents ('ceo-final' → 'CEO · final review')."""
    n = (name or "").lower()
    if n.startswith("ceo"):
        what = {"ceo-plan": "plan review", "ceo-final": "final review", "ceo-effort": "effort", "ceo-solo": "effort"}.get(n)
        if what is None and n.startswith("ceo-ruling"):
            what = "decision"
        return "CEO" + (f" · {what}" if what else "")
    if n.startswith("reviewer"):
        return "Reviewer" + (f" · task #{task}" if task else "")
    return (name or "").replace("-", " ").strip().capitalize()
ROLES = {"lead": "Lead", "member": "Builder", "reviewer": "Reviewer", "ceo": "CEO", "refiner": "Brief writer"}
SIZE_WEIGHT = {"S": 1, "M": 2, "L": 4}


def agents_view(st: Store) -> list[dict]:
    """Everyone who worked on the project: the standing team, the CEO and reviewers, and their helpers."""
    helpers: dict[str, dict] = {}
    for ev in st.events("helper", limit=600):
        d, hid = ev["data"], ev["data"].get("id")
        if d.get("state") == "start":
            helpers[hid] = {"seat": ev["seat"], "what": d.get("what") or "a helper", "type": d.get("type") or "helper",
                            "task": ev["task_id"], "started": ev["ts"], "status": "working"}
        elif hid in helpers:
            helpers[hid].update(status="done" if d.get("state") == "done" else "failed",
                                seconds=round(ev["ts"] - helpers[hid]["started"]))
    out = []
    for s in st.seats():
        mine = [h for h in helpers.values() if h["seat"] == s["name"]]
        out.append({"name": s["name"], "title": agent_title(s["name"], s["role"]), "role": ROLES.get(s["role"], s["role"] or "Builder"),
                    "product": PRODUCTS.get(s["vendor"], s["vendor"] or ""), "model": s["model"] or "",
                    "account": s["account"] or "", "effort": s.get("effort") or "auto", "status": s["status"],
                    "doing": friendly_activity(s["note"] or "", s["status"]), "task": s["current_task"],
                    "tokens": s["tokens"] or 0, "turns": s["turns"] or 0, "restarts": s["restarts"] or 0,
                    "last": s["last_event_at"], "helpers": mine[-8:],
                    "helpers_total": len(mine), "standing": True})
    oneoffs: dict[str, dict] = {}
    for ev in st.events("oneoff", limit=400):
        d = ev["data"]
        if d.get("state") == "start":
            oneoffs[ev["seat"]] = {"name": ev["seat"], "title": agent_title(ev["seat"], d.get("role"), ev["task_id"]),
                                   "role": ROLES.get(d.get("role"), d.get("role") or ""),
                                   "product": PRODUCTS.get(d.get("vendor"), d.get("vendor") or ""),
                                   "model": d.get("model") or "", "account": d.get("account") or "",
                                   "effort": d.get("effort") or "auto", "status": "working", "task": ev["task_id"],
                                   "doing": "thinking it through" if d.get("role") == "ceo" else "checking the work",
                                   "tokens": 0, "turns": 1, "started": ev["ts"], "helpers": [], "helpers_total": 0,
                                   "standing": False}
        elif ev["seat"] in oneoffs:
            oneoffs[ev["seat"]].update(status="done" if d.get("state") == "done" else "failed",
                                       doing="finished", tokens=d.get("tokens") or 0, seconds=d.get("seconds"))
    ordered = sorted(oneoffs.values(), key=lambda a: (a["status"] != "working", -(a.get("started") or 0)))
    return out + ordered[:30]


def estimate(st: Store, tasks: list[dict], running: bool) -> dict:
    """A rough forecast from how long finished tasks took (by size), shared across the builders."""
    done = [t for t in tasks if t["status"] == "merged" and t.get("started_at") and t.get("finished_at")]
    left = [t for t in tasks if t["status"] not in ("merged", "cancelled")]
    used = sum(int(s.get("tokens") or 0) for s in st.seats()) + sum(
        int(ev["data"].get("tokens") or 0) for ev in st.events("oneoff", limit=400) if ev["data"].get("state") != "start")
    out = {"tokens_used": used, "tasks_left": len(left), "tasks_done": len(done), "minutes_left": None,
           "tokens_left": None, "basis": "none", "elapsed": None}
    started = st.get("started_at")
    if started:
        end = now()
        if not running:  # a finished or paused project: up to its last message, not up to now
            last = st.recent_messages(1)
            end = last[0]["ts"] if last else end
        out["elapsed"] = max(0, round(end - float(started)))
    if not running or not tasks:
        return out
    weight_done = sum(SIZE_WEIGHT.get(t["size"], 2) for t in done)
    weight_left = sum(SIZE_WEIGHT.get(t["size"], 2) for t in left)
    if not weight_left:
        out["minutes_left"] = 5  # final checks
        return out
    builders = max(1, sum(1 for s in st.seats() if s["role"] in ("lead", "member") and s["status"] not in ("down",)))
    parallel = max(1, min(builders, len(left)))
    if weight_done:
        minutes_per = sum((t["finished_at"] - t["started_at"]) / 60 for t in done) / weight_done
        tokens_per = sum(int(t.get("tokens") or 0) for t in done) / weight_done
        out["basis"] = "finished tasks" if len(done) >= 3 else "first tasks"
    else:
        minutes_per, tokens_per = 12.0, 0  # before anything has finished: a typical pace
        out["basis"] = "typical pace"
    out["minutes_left"] = max(5, round(weight_left * minutes_per / parallel + 5))
    out["tokens_left"] = round(weight_left * tokens_per) if tokens_per else None
    return out

