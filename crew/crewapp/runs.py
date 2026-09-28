"""Team projects started from the app: each runs as its own background process
(so the app can close and reopen without disturbing it) and is observed through
its SQLite store."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from crewlib import config as cfgmod, tiers
from crewlib.cli import claim_run_dir
from crewlib.store import Store
from crewlib.util import clip, crew_home, db_damaged, now
from crewlib.web import PHASES, friendly_activity, state as run_state

CREW_ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ("refine", "plan", "build", "deliver")
DAMAGED = ("This project's record is damaged (the file team.db, in the project's folder under Crew's runs), so "
           "Crew cannot show its progress or carry it on. The record is kept as it is, and the work the team did "
           "is untouched in the project's folder. To carry on, start a new project and ask the team to continue "
           "from that folder.")


class RecordDamaged(ValueError):
    """A project's record cannot be read (the app answers with DAMAGED, in plain words)."""
PREVIEW_CANDIDATES = ("index.html", "dist/index.html", "build/index.html", "public/index.html", "site/index.html",
                      "docs/index.html", "web/index.html")


def runs_dir() -> Path:
    path = crew_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_dir(run_id: str) -> Path | None:
    # A project's id is about 60 letters; a longer one is not a project (and too long a name for the computer).
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", run_id):
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
        with open(run_dir / "app-run.log", "ab") as log:  # the project keeps its own handle; the app lets go of it
            proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "crewlib", *args], cwd=str(CREW_ROOT),
                                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        self.procs[run_id] = proc

    def start(self, request: str, repo: str | None = None, mode: str | None = None, hours=None,
              head_to_head: str | None = None, accounts: list[str] | None = None) -> str:
        if not isinstance(request, str):
            raise ValueError("Tell the team what you want first.")
        request = request.strip()
        if len(request) < 3:
            raise ValueError("Tell the team what you want first.")
        if repo is not None and not isinstance(repo, str):
            raise ValueError("The project folder must be a folder's path.")
        names = [a for a in (accounts if isinstance(accounts, list) else []) if isinstance(a, str)
                 and re.fullmatch(r"[\w.@+-]+", a)]
        if names:  # a choice the team cannot use (a subscription since renamed or removed) is said now, not lost
            from . import settings as settings_mod
            try:
                cfgmod.load(str(settings_mod.path()) if settings_mod.path().is_file() else None).restrict(names)
            except cfgmod.ConfigError as exc:
                raise ValueError(f"This project cannot use those subscriptions: {exc}.") from None
        run_id, run_dir = claim_run_dir(runs_dir(), request)
        # The request travels in a file, not on the command line: words that start with "-" would be read as
        # options, and Windows limits a command line to about 32,000 characters.
        request_file = run_dir / "request.txt"
        request_file.write_text(request, encoding="utf-8")
        args = ["start", "--request-file", str(request_file), "--headless", "--run-id", run_id]
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
        if head_to_head in ("off", "some", "all"):
            args += ["--head-to-head", head_to_head]
        if names:
            args += ["--accounts", ",".join(names)]
        self._spawn(run_id, args)
        (runs_dir() / "LATEST").write_text(run_id, encoding="utf-8")
        return run_id

    def resume(self, run_id: str) -> bool:
        """Continue a stopped project. False when it is still running: starting it again would put two
        orchestrators on one project."""
        if _run_dir(run_id) is None or self.store(run_id) is None:
            raise ValueError("That project could not be found.")
        if self.alive(run_id):
            return False
        self._spawn(run_id, ["resume", run_id, "--headless"])
        return True

    @staticmethod
    def exists(run_id: str) -> bool:
        """Is there a project (at least its folder) with this id?"""
        return _run_dir(run_id) is not None

    def start_problem(self, run_id: str) -> str:
        """Why a project that has no team yet will not get one: its program ended before the team began (git
        missing, a settings problem …), in the words its log ends with. '' while it may still be starting."""
        run_dir = _run_dir(run_id)
        if run_dir is None or (run_dir / "team.db").is_file():
            return ""
        proc = self.procs.get(run_id)
        try:
            ended = proc.poll() is not None if proc is not None else now() - run_dir.stat().st_mtime > 180
            log = (run_dir / "app-run.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        if not ended:
            return ""
        last = [line.strip() for line in log.splitlines() if line.strip()][-1:]
        return "The project could not start" + (f": {clip(last[0], 300)}" if last else ".") + \
            " Nothing was lost; you can start it again."

    def running(self, run_id: str) -> bool:
        """Started by this app, and its process is still alive."""
        proc = self.procs.get(run_id)
        return proc is not None and proc.poll() is None

    def alive(self, run_id: str, st: Store | None = None) -> bool:
        """Is the project's orchestrator running — started by this app, or by another Crew (before a restart or an
        update, or from a terminal)? Its heartbeat says so; a project from a Crew that wrote none is judged by its
        recent activity, as before."""
        if self.running(run_id):
            return True
        st = st if st is not None else self.store(run_id)
        if st is None:
            return False
        beat = st.orchestrator_alive()
        if beat is not None:
            return beat
        return st.get("phase", "refine") in ACTIVE and self._recent(st)

    # ---------------------------------------------------------------- state

    def store(self, run_id: str) -> Store | None:
        run_dir = _run_dir(run_id)
        if run_dir is None or not (run_dir / "team.db").is_file():
            return None
        with self._lock:
            if run_id not in self._stores:
                try:
                    self._stores[run_id] = Store(run_dir / "team.db")
                except sqlite3.DatabaseError as exc:
                    if db_damaged(exc):
                        raise RecordDamaged(DAMAGED) from None  # left as it is: it is the project's own record
                    raise
            return self._stores[run_id]

    def stop(self, run_id: str) -> bool:
        st = self.store(run_id)
        if st is None:
            return False
        st.set("stop_requested", now())
        return True

    def say(self, run_id: str, text: str, to: str | None = None) -> bool:
        """A message from the owner: to the whole team, or directly to one agent (or "ceo"); only that agent sees
        a direct message, and its answer comes back to the owner."""
        st = self.store(run_id)
        if st is None or not isinstance(text, str) or not text.strip():
            return False
        if to is not None and not isinstance(to, str):
            raise ValueError("Choose who reads your message from the list.")
        to = (to or "").strip().lower()
        if to and to != "ceo" and st.seat(to) is None:
            raise ValueError(f"There is no agent called {to} in this project.")
        st.owner_message(text.strip()[:4000], to or None)  # through the prompt writer when the project uses it
        return True

    def interrupt(self, run_id: str, seat: str) -> bool:
        """The owner's "Ask now": the agent stops its current step and answers."""
        st = self.store(run_id)
        if st is None or not isinstance(seat, str) or st.seat(seat) is None:
            return False
        st.set(f"interrupt:{seat}", now())
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
        try:
            st = self.store(run_id)
        except RecordDamaged as exc:
            return {"id": run_id, "starting": True, "messages": [], "problem": str(exc)}
        if st is None:
            return None
        data = run_state(st, runs_dir() / run_id, after)
        phase = st.get("phase", "refine")
        running = self.alive(run_id, st)
        tasks = st.tasks()
        data.update(id=run_id, running=running, raw_phase=phase, mode=st.get("mode") or "", preview=self.preview(run_id),
                    folder=str(self.project_dir(run_id) or ""), started=st.get("started_at"),
                    request=st.get("goal", ""), timer=float(st.get("max_hours") or 0),
                    agents=agents_view(st), estimate=estimate(st, tasks, running), shares=tiers.shares(st),
                    contests=[{"task": e["task_id"], "winner": tiers.model_label(e["data"].get("winner") or ""),
                               "loser": tiers.model_label(e["data"].get("loser") or ""),
                               "both_passed": bool(e["data"].get("winner_passed") and e["data"].get("loser_passed")),
                               "reason": e["data"].get("reason") or ""} for e in st.events("contest", limit=50)],
                    head_to_head=st.get("head_to_head") or "", accounts_chosen=st.get("accounts_chosen") or [])
        by_id = {t["id"]: t for t in tasks}
        seats = {s["name"]: s for s in st.seats()}
        for t in data["tasks"]:
            full = by_id.get(t["id"]) or {}
            builder = seats.get(full.get("owner") or "") or {}
            t.update(effort=full.get("effort") or "", size=full.get("size") or "", kind=full.get("kind") or "",
                     tokens=full.get("tokens") or 0, raw=full.get("status") or "", tier=full.get("tier") or "",
                     twin=full.get("twin"),
                     model=tiers.model_label(builder.get("model") or "") if builder else "")
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
            except RecordDamaged:  # still listed: its page says what happened
                try:
                    title = (run_dir / "request.txt").read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    title = ""
                out.append({"id": run_dir.name, "title": clip(" ".join(title.split()) or run_dir.name, 80),
                            "phase": "Record damaged", "raw_phase": "damaged", "running": False, "done": False,
                            "progress": [0, 0], "started": run_dir.stat().st_mtime, "mode": "", "preview": {}})
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
            "running": self.alive(run_dir.name, st),
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
        if what is None and n.startswith("ceo-question"):
            what = "your question"
        return "CEO" + (f" · {what}" if what else "")
    if n.startswith("reviewer"):
        return "Reviewer" + (f" · task #{task}" if task else "")
    if n.startswith("judge"):
        return "Head-to-head judge" + (f" · task #{task}" if task else "")
    if n == "refiner":
        return "Brief writer"
    if n.startswith("writer"):
        return "Prompt writer"
    return (name or "").replace("-", " ").strip().capitalize()
ROLES = {"lead": "Lead", "member": "Builder", "reviewer": "Reviewer", "ceo": "CEO", "refiner": "Brief writer",
         "writer": "Prompt writer"}
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
                    "tier": tiers.seat_tier(s["vendor"] or "claude"),
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
                                   "tier": "ceo" if d.get("role") == "ceo" else tiers.seat_tier(d.get("vendor") or "claude"),
                                   "product": PRODUCTS.get(d.get("vendor"), d.get("vendor") or ""),
                                   "model": d.get("model") or "", "account": d.get("account") or "",
                                   "effort": d.get("effort") or "auto", "status": "working", "task": ev["task_id"],
                                   "doing": {"ceo": "thinking it through", "refiner": "writing the brief",
                                             "writer": "writing up your message"}.get(d.get("role"),
                                                                                      "checking the work"),
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

