"""The orchestrator: deterministic coordination around non-deterministic agents.

It owns the process (phases, assignment, reviews, merges, timers, failover,
usage) so the agents can spend their intelligence on the work itself.
Everything it knows lives in the run's SQLite store, so a crashed run can be
resumed.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, lessons, prompts, scheduler
from .agents import (ClaudeSeat, ClaudeSetup, CodexSeat, CodexSetup, Event, RunResult, copy_claude_session,
                     run_once_claude, run_once_codex)
from .config import Account, Config, SeatSpec
from .store import Store, StoreError
from .tools import _fmt_msg, _mentions
from .util import Redactor, atomic_write, clip, crew_home, hhmm, human_duration, load_env_file, now

TICK = 1.0


@dataclass
class SeatRT:
    spec: SeatSpec
    account: Account
    worktree: Path
    runner: ClaudeSeat | CodexSeat | None = None
    busy: bool = False
    turn_started: float = 0.0
    last_event: float = 0.0
    last_label: str = ""
    pending: list[str] = field(default_factory=list)
    errors_in_row: int = 0
    restarts: int = 0
    nudges: int = 0
    last_nudge: float = 0.0
    idle_nudges: int = 0
    down: bool = False
    benched: bool = False  # not used in this run (solo mode)
    stopping: bool = False
    restart_times: list[float] = field(default_factory=list)
    cooldown_until: float = 0.0
    want_effort: str = ""  # the effort the CEO set for the seat's current task

    @property
    def name(self) -> str:
        return self.spec.name


class Orchestrator:
    def __init__(self, cfg: Config, run_dir: Path, repo: Path, request: str, run_id: str, resume: bool = False):
        self.cfg, self.run_dir, self.repo, self.request, self.run_id = cfg, run_dir, repo, request, run_id
        self.resume = resume
        self.store = Store(run_dir / "team.db")
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.seats: dict[str, SeatRT] = {}
        self.jobs: dict[str, threading.Thread] = {}
        self.job_results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.prefix = f"crew/{run_id}"
        self.integration = f"{self.prefix}/main"
        self.main_wt = run_dir / "worktrees" / "_main"
        self.started = now()
        self.last_progress = now()
        self.stall_count = 0
        self.task_snapshot: dict[int, str] = {}
        self.chat_seen = 0
        self.grace_until: dict[int, float] = {}
        self.processed_escalations: set[int] = set()
        self.plan_reviewed = False
        self.plan_revisions = 0
        self.final_rounds = 0
        self.merging = False
        self.all_parked_notice = 0.0
        self.last_activity_write: dict[str, float] = {}
        secrets = {**load_env_file(crew_home() / "secrets.env")}
        self.secrets = secrets
        self.redact = Redactor(secrets)
        self.log_file = run_dir / "orchestrator.log"

    @property
    def lead_name(self) -> str:
        return self.store.get("lead") or self.cfg.lead.name

    # ================================================================ logging

    def log(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {self.redact(text)}\n"
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def say(self, text: str, urgent: bool = False, task_id: int | None = None) -> None:
        """Orchestrator note in the group chat (visible to everyone)."""
        self.store.post("crew", "system", self.redact(text), task_id=task_id, urgent=urgent)

    def phase(self) -> str:
        return self.store.get("phase", "refine")

    def set_phase(self, value: str) -> None:
        self.store.set("phase", value)
        self.log(f"phase -> {value}")

    # ================================================================== setup

    def prepare(self) -> None:
        (self.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        user_skills = crew_home() / "skills" / ".claude-plugin" / "plugin.json"
        if not user_skills.is_file():  # skills that agents create for future teams
            user_skills.parent.mkdir(parents=True, exist_ok=True)
            (crew_home() / "skills" / "skills").mkdir(exist_ok=True)
            user_skills.write_text('{"name": "crew-team-skills", "version": "1.0.0", "description": '
                                   '"Skills created by past Crew teams.", "author": {"name": "Crew teams"}}\n',
                                   encoding="utf-8")
        st = self.store
        if not self.resume:
            base_branch = gitops.current_branch(self.repo)
            base_commit = gitops.head(self.repo)
            st.set("goal", self.request)
            st.set("repo", str(self.repo))
            st.set("base_branch", base_branch)
            st.set("base_commit", base_commit)
            st.set("integration", self.integration)
            st.set("started_at", self.started)
            st.set("project_name", self.repo.name)
            st.set("settings", {"chat_budget": self.cfg.team.chat_budget, "max_escalations": 4})
            gitops.add_worktree(self.repo, self.main_wt, self.integration, base_commit)
        else:
            self.started = st.get("started_at", now())
            self.integration = st.get("integration", self.integration)
            self.prefix = self.integration.rsplit("/", 1)[0]
            gitops.add_worktree(self.repo, self.main_wt, self.integration, st.get("base_commit"))
        for acc in self.cfg.accounts:
            st.upsert_account(acc.name, vendor=acc.vendor, profile=str(acc.profile_dir() or "default"))
        for spec in self.cfg.seats:
            wt = self.run_dir / "worktrees" / spec.name
            if not wt.exists():
                gitops.git(self.repo, "worktree", "add", "-f", "--detach", str(wt), self.integration)
            model = self.cfg.models.work if spec.vendor == "claude" else (self.cfg.models.codex or "codex-default")
            prev = st.seat(spec.name) or {}
            if self.resume and prev.get("role"):
                spec.role = prev["role"]  # leadership may have changed hands before the stop
            account = self.cfg.account(prev.get("account") or spec.account)
            st.upsert_seat(spec.name, vendor=spec.vendor, role=spec.role, account=account.name, model=model,
                           worktree=str(wt), status="starting")
            self.seats[spec.name] = SeatRT(spec=spec, account=account, worktree=wt)
        self.chat_seen = st.last_message_id() if self.resume else 0
        for t in st.tasks():
            self.task_snapshot[t["id"]] = t["status"]

    # ============================================================ agent setup

    def _claude_setup(self, effort: str | None = None, model: str | None = None) -> ClaudeSetup:
        return ClaudeSetup(model=model or self.cfg.models.work, effort=effort or self.cfg.models.effort_work,
                           work_model=self.cfg.models.work, permission_mode=self.cfg.team.permission_mode,
                           run_dir=self.run_dir, extra_env=self._secret_env())

    def task_effort(self, task: dict | None) -> str:
        """How hard the builder of a task should think: the owner's fixed choice, else the CEO's call per task
        (made at plan review), else a sensible default from the task's size and kind."""
        fixed = self.cfg.models.effort_work
        if fixed and fixed != "auto":
            return fixed
        if task and task.get("effort"):
            return task["effort"]
        if not task:
            return "auto"
        if task.get("kind") in ("research", "verify", "docs"):
            return "medium"
        return {"S": "medium", "M": "high", "L": "xhigh"}.get(task.get("size") or "M", "high")

    def _codex_setup(self, effort: str | None = None) -> CodexSetup:
        return CodexSetup(model=self.cfg.models.codex, effort=effort or self.cfg.models.effort_work, run_dir=self.run_dir,
                          extra_env=self._secret_env(),
                          bypass_sandbox=self.cfg.team.permission_mode == "bypassPermissions")

    def _secret_env(self) -> dict[str, str]:
        env = {k: v for k, v in self.secrets.items() if k != "ANTHROPIC_API_KEY"}
        if "ANTHROPIC_API_KEY" in self.secrets:  # would switch a subscription seat to API billing
            env["CREW_SECRET_ANTHROPIC_API_KEY"] = self.secrets["ANTHROPIC_API_KEY"]
        return env

    def _system_prompt(self, rt: SeatRT) -> str:
        seats = self.store.seats()
        if rt.spec.role == "lead" and self.store.get("mode") == "solo":
            return prompts.solo_system(rt.name, seats)
        if rt.spec.role == "lead":
            return prompts.lead_system(rt.name, seats)
        return prompts.member_system(rt.name, seats, self.lead_name)

    def start_seat(self, rt: SeatRT, first: str | None, resume_session: str | None = None,
                   effort: str | None = None) -> None:
        system = self._system_prompt(rt)
        index = list(self.seats).index(rt.name)
        port_env = {"CREW_PORT_BASE": str(4100 + 100 * index)}  # separate server ports per agent
        effort = effort or rt.want_effort or None
        if rt.spec.vendor == "claude":
            setup = self._claude_setup(effort=effort)
            setup.extra_env = {**setup.extra_env, **port_env}
            rt.runner = ClaudeSeat(rt.name, rt.spec.role, rt.account, rt.worktree, setup, system,
                                   self.events, self.redact)
        else:
            setup = self._codex_setup(effort=effort)
            setup.extra_env = {**setup.extra_env, **port_env}
            rt.runner = CodexSeat(rt.name, rt.spec.role, rt.account, rt.worktree, setup, system,
                                  self.events, self.redact)
        rt.busy = bool(first)
        rt.turn_started = rt.last_event = now()
        rt.runner.start(first_message=first, resume=resume_session)
        self.store.update_seat(rt.name, status="busy" if first else "idle", account=rt.account.name,
                               effort=setup.effort)
        self.log(f"started {rt.name} on {rt.account.name}" + (f" (resume {resume_session})" if resume_session else ""))

    # =================================================================== run

    def run(self) -> str:
        try:
            self.prepare()
            if not self.resume:
                self.refine()
                self.kickoff()
            else:
                self.store.set("stop_requested", None)
                self.set_phase(self.store.get("phase_before_stop") or ("build" if self.store.tasks() else "plan"))
                self.started = now() - 60  # the time limit counts from the resume
                self.resume_seats()
            self.loop()
        except KeyboardInterrupt:
            self.say("Stopped by the owner.")
            self.set_phase("stopped")
        except Exception as exc:  # the report must still be written
            self.log("FATAL " + traceback.format_exc())
            self.say(f"The orchestrator hit an internal error and stopped: {exc}")
            self.set_phase("failed")
        finally:
            self.shutdown()
        return self.phase()

    # ---------------------------------------------------------------- refine

    def repo_overview(self) -> str:
        files = gitops.git(self.repo, "ls-files", check=False).stdout.splitlines()
        head = "\n".join(files[:150]) + (f"\n… and {len(files) - 150} more files" if len(files) > 150 else "")
        readme = next((self.repo / n for n in ("README.md", "README", "readme.md") if (self.repo / n).is_file()), None)
        intro = clip(readme.read_text(encoding="utf-8", errors="replace"), 2000) if readme else "(no README)"
        return f"Files ({len(files)}):\n{head or '(empty repository)'}\n\nREADME:\n{intro}"

    def refine(self) -> None:
        self.set_phase("refine")
        self.say("Received the owner's request. Refining it into a precise brief…")
        lead = self.seats[self.lead_name]
        res = run_once_claude(
            prompts.refiner_prompt(self.request, self.repo_overview()), seat="refiner", role="member",
            account=lead.account, workdir=self.main_wt,
            setup=self._claude_setup(effort=self.cfg.models.effort_light), redact=self.redact,
            json_schema=prompts.REFINER_SCHEMA, read_only=True, timeout=600, with_team_tools=False)
        self._account_usage(lead.account.name, res)
        brief = res.structured if isinstance(res.structured, dict) and res.structured.get("goal") else None
        if brief is None:
            self.log(f"refiner failed ({clip(res.text, 300)}); using the request as the brief")
            brief = {"title": clip(self.request.strip().splitlines()[0] if self.request.strip() else "Project", 80),
                     "goal": self.request, "deliverables": [], "acceptance_criteria": [],
                     "constraints": [], "assumptions": ["The request was used as written."]}
        self.store.set("brief", brief)
        self.store.set("project_name", brief.get("title") or self.repo.name)
        self.say("Brief:\n" + prompts.brief_text(brief))

    def brief_text(self) -> str:
        return prompts.brief_text(self.store.get("brief", {}) or {"goal": self.request})

    # --------------------------------------------------------------- kickoff

    def decide_mode(self) -> str:
        """Solo for small or hard-to-split jobs (fastest, one writer); the full team only when parallel work pays."""
        mode = self.cfg.team.mode
        if mode == "auto":
            brief = self.store.get("brief", {}) or {}
            parts = int(brief.get("independent_parts") or 0)
            if len(self.seats) < 2 or brief.get("size") == "small" or (parts and parts <= 2):
                mode = "solo"
            else:
                mode = "team"
        self.store.set("mode", mode)
        return mode

    def kickoff(self) -> None:
        if self.decide_mode() == "solo":
            self.kickoff_solo()
            return
        brief = self.store.get("brief", {}) or {}
        if self.cfg.team.mode == "auto":
            self.say(f"This job has about {brief.get('independent_parts', 'several')} parts that can be built at the "
                     "same time, so the whole team works on it.")
        self.set_phase("plan")
        self.store.set("plan_msg_id", self.store.last_message_id())
        brief = self.brief_text()
        for rt in self.seats.values():
            if rt.spec.role == "lead":
                self.start_seat(rt, prompts.kickoff_lead(brief, self.request))
            else:
                self.start_seat(rt, prompts.kickoff_member(brief, self.lead_name))
        self.say(f"Team started: {', '.join(self.seats)}. {self.lead_name} is planning.")

    def kickoff_solo(self) -> None:
        """One builder, everyone else checks: single-agent speed with independent review kept."""
        lead = self.seats[self.lead_name]
        for rt in self.seats.values():
            if rt is not lead:
                rt.benched = True
                self.store.update_seat(rt.name, status="standby", note="checks and backs up this run")
        brief = self.store.get("brief", {}) or {}
        criteria = "\n".join(f"- {c}" for c in brief.get("acceptance_criteria") or []) or "Meets the brief."
        task_id = self.store.create_task(
            title=brief.get("title") or "The project", spec=self.brief_text(), acceptance=criteria,
            scope=["**"], depends_on=[], size="L", kind="build", suggested_owner=lead.name, created_by="crew")
        self.store.update_task(task_id, effort=self.solo_effort())
        self.set_phase("build")
        self.last_progress = now()
        self.say(f"This job is small enough that one builder is fastest, so {lead.name} builds it and the others "
                 "check it: a fresh reviewer, then the CEO model.")
        task = self.store.task(task_id)
        if self.give_task(lead, task, "normal"):
            first = "\n\n".join([prompts.kickoff_solo(self.brief_text(), self.request), *lead.pending])
            lead.pending.clear()
            self.start_seat(lead, first)

    def resume_seats(self) -> None:
        solo = self.store.get("mode") == "solo"
        for rt in self.seats.values():
            if solo and rt.name != self.lead_name:
                rt.benched = True
                continue
            row = self.store.seat(rt.name) or {}
            task = self.store.task(row["current_task"]) if row.get("current_task") else None
            msg = "The run was resumed after an interruption. "
            if task and task["status"] in ("in_progress", "changes"):
                msg += f"Continue task #{task['id']} ({task['title']}); read your notes and the chat first."
            else:
                msg += "Read the team chat and wait for your next assignment."
            self.start_seat(rt, msg, resume_session=row.get("session_id"))
        self.say("Run resumed.")

    # ================================================================== loop

    def loop(self) -> None:
        hours = self.store.get("max_hours")
        hours = float(self.cfg.team.max_hours if hours is None else hours)
        max_seconds = hours * 3600  # 0: no time limit
        while self.phase() not in ("done", "stopped", "failed"):
            try:
                ev = self.events.get(timeout=TICK)
                self.handle_event(ev)
                while True:
                    self.handle_event(self.events.get_nowait())
            except queue.Empty:
                pass
            self.drain_jobs()
            if self.store.get("stop_requested"):
                self.say("Stop requested by the owner. Saving everyone's work.")
                self.stop_run()
                break
            if max_seconds > 0 and now() - self.started > max_seconds:
                self.say(f"The timer you set ({hours:g} h) is up. Stopping and saving the work.")
                self.stop_run()
                break
            self.tick()

    def stop_run(self) -> None:
        self.store.set("phase_before_stop", self.phase())
        self.set_phase("stopped")

    def tick(self) -> None:
        modes = scheduler.refresh_modes(self.store)
        self.wake_waiting(modes)
        self.route_chat()
        self.track_progress()
        self.handle_parked(modes)
        phase = self.phase()
        if phase == "plan":
            self.plan_phase()
        elif phase == "build":
            self.build_phase(modes)
        elif phase == "deliver":
            self.deliver_phase()
        self.watchdog()
        self.process_escalations()
        self.deliver_pending()

    # ================================================================ events

    def handle_event(self, ev: Event) -> None:
        rt = self.seats.get(ev.seat)
        if rt is None or rt.runner is None or ev.data.get("gen") != getattr(rt.runner, "gen", None):
            return  # an event from a process we already replaced (restart, failover): ignore it
        rt.last_event = ev.ts
        if ev.kind == "init":
            if ev.data.get("session_id"):
                self.store.update_seat(rt.name, session_id=ev.data["session_id"])
            mcp = ev.data.get("mcp") or {}
            if mcp and mcp.get("crew_team") not in (None, "connected"):
                self.log(f"{rt.name}: team tools status {mcp.get('crew_team')}")
                self.say(f"{rt.name} could not connect to the team tools ({mcp.get('crew_team')}); restarting it.")
                self.restart_seat(rt, "team tools unavailable")
        elif ev.kind == "activity":
            rt.last_label = ev.data.get("label", rt.last_label)
            if now() - self.last_activity_write.get(rt.name, 0) > 2:
                self.last_activity_write[rt.name] = now()
                self.store.update_seat(rt.name, note=clip(rt.last_label, 120), last_event_at=ev.ts)
        elif ev.kind == "helper":
            if ev.data.get("state") != "step":  # starts and ends are kept for the app's agents panel
                row = self.store.seat(rt.name) or {}
                self.store.event("helper", seat=rt.name, task_id=row.get("current_task"), id=ev.data.get("id"),
                                 state=ev.data.get("state"), what=ev.data.get("what", ""), type=ev.data.get("type", ""))
        elif ev.kind == "repeat":
            self.say(f"@{rt.name} you have run the same step several times ({clip(ev.data.get('label', ''), 80)}). "
                     "Step back: check your assumptions, try a different approach, or block the task with the reason.",
                     urgent=True)
        elif ev.kind == "rate":
            info = ev.data.get("info") or {}
            fields = scheduler.apply_rate(self.store, rt.account.name, info)
            self.store.event("rate", seat=rt.name, account=rt.account.name, **{k: v for k, v in fields.items()})
        elif ev.kind == "result":
            self.on_result(rt, ev.data)
        elif ev.kind == "exit":
            self.on_exit(rt, ev.data)

    def on_result(self, rt: SeatRT, data: dict) -> None:
        rt.busy = False
        tokens, cost = int(data.get("tokens") or 0), float(data.get("cost") or 0)
        self.store.add_seat_usage(rt.name, tokens=tokens, cost=cost, turns=1)
        self.store.add_account_usage(rt.account.name, tokens=tokens, cost=cost)
        row = self.store.seat(rt.name) or {}
        if row.get("current_task"):
            self.store.add_task_usage(row["current_task"], tokens=tokens, cost=cost)
        self.store.update_seat(rt.name, status="idle", note="")
        if data.get("auth_error"):
            rt.down = True
            self.store.update_seat(rt.name, status="down", note=f"{rt.account.name} is not signed in")
            self.store.event("auth_error", seat=rt.name, account=rt.account.name)
            self.release_task_of(rt, f"{rt.account.name} is not signed in")
            self.say(f"{rt.name} cannot sign in to {rt.account.name}. Run `crew setup` to sign in again; "
                     "the rest of the team carries on.", urgent=True)
            if rt.name == self.lead_name:
                self.promote_lead(rt, "not signed in")
            return
        if data.get("limit_hit"):
            acc = self.store.account(rt.account.name) or {}
            until = acc.get("parked_until") or int(now() + 3600)
            self.store.upsert_account(rt.account.name, status="rejected", parked_until=max(until, int(now() + 60)))
            self.store.event("limit_hit", seat=rt.name, account=rt.account.name)
            self.say(f"{rt.account.name} reached its usage limit (resets {hhmm(until)}). Moving {rt.name} to another account.")
            self.failover(rt, reason="usage limit")
            return
        if data.get("is_error"):
            rt.errors_in_row += 1
            self.log(f"{rt.name} turn error ({rt.errors_in_row}): {clip(data.get('text', ''), 300)}")
            if rt.errors_in_row >= 3:
                self.restart_seat(rt, "repeated errors")
            else:
                rt.pending.append("Your last turn ended with an error. Check what happened and continue your work.")
            return
        rt.errors_in_row = 0
        self.after_turn(rt)

    def after_turn(self, rt: SeatRT) -> None:
        """A seat finished a turn cleanly: nudge if it left its task hanging."""
        row = self.store.seat(rt.name) or {}
        task = self.store.task(row["current_task"]) if row.get("current_task") else None
        if task and task["owner"] == rt.name and task["status"] == "in_progress" and self.phase() == "build":
            if rt.idle_nudges < 3:  # the nudge is delivered together with any unread chat
                rt.idle_nudges += 1
                rt.pending.append(
                    f"Task #{task['id']} is still in progress. Continue until it is done and verified, then submit "
                    "(team_task_submit). If you are blocked, use team_task_block; if you cannot do it, "
                    "team_task_release. Do not wait for others.")
            elif rt.idle_nudges >= 3:
                self.store.append_note(task["id"], "crew", f"{rt.name} stopped without finishing; task reassigned.")
                self.store.update_task(task["id"], status="todo", owner=None, suggested_owner=None)
                self.store.update_seat(rt.name, current_task=None)
                rt.idle_nudges = 0
                self.say(f"Task #{task['id']} returned to the board ({rt.name} kept stopping without finishing).")
        else:
            rt.idle_nudges = 0

    def on_exit(self, rt: SeatRT, data: dict) -> None:
        rt.busy = False
        if rt.stopping or self.phase() in ("done", "stopped", "failed"):
            return
        self.log(f"{rt.name} exited code={data.get('code')} {clip(data.get('stderr', ''), 400)}")
        if rt.spec.vendor == "codex" and data.get("code") == 127:
            rt.down = True
            self.store.update_seat(rt.name, status="down", note="Codex is not installed")
            self.release_task_of(rt, "Codex is not installed on this machine")
            return
        self.restart_seat(rt, f"process exited ({data.get('code')})")

    # =============================================================== restarts

    def restart_seat(self, rt: SeatRT, reason: str) -> None:
        rt.restarts += 1
        rt.restart_times = [t for t in rt.restart_times if now() - t < 900] + [now()]
        self.store.update_seat(rt.name, restarts=rt.restarts)
        self.store.event("restart", seat=rt.name, reason=reason)
        if rt.runner:
            rt.stopping = True
            rt.runner.stop()
            rt.stopping = False
        if len(rt.restart_times) > 4:  # more than four restarts in 15 minutes
            self.release_task_of(rt, f"{rt.name} is out: {reason}")
            if rt.name == self.lead_name and self.promote_lead(rt, reason):
                rt.down = True
                self.store.update_seat(rt.name, status="down")
                return
            if rt.name == self.lead_name:  # nobody can replace the lead: cool down and try again
                rt.runner = None
                rt.restart_times.clear()
                rt.cooldown_until = now() + 300
                self.store.update_seat(rt.name, status="waiting", note="cooling down after repeated failures")
                self.say(f"{rt.name} (lead) keeps failing ({reason}); retrying in 5 minutes.")
                return
            rt.down = True
            self.store.update_seat(rt.name, status="down")
            self.say(f"{rt.name} failed repeatedly ({reason}) and is out for this run; its work goes to the others.")
            return
        session = (self.store.seat(rt.name) or {}).get("session_id")
        row = self.store.seat(rt.name) or {}
        task = self.store.task(row["current_task"]) if row.get("current_task") else None
        msg = f"You were restarted ({reason}). Read the chat, then "
        msg += (f"continue task #{task['id']} from your notes." if task and task["status"] == "in_progress"
                else "wait for your next assignment.")
        self.log(f"restarting {rt.name}: {reason}")
        rt.errors_in_row = 0
        self.start_seat(rt, msg, resume_session=session)

    def promote_lead(self, old: SeatRT, reason: str) -> bool:
        """The lead cannot continue: hand leadership to the healthiest Claude seat, with a fresh briefing."""
        accounts = {a["name"]: a for a in self.store.accounts()}
        candidates = [r for r in self.seats.values() if r is not old and not r.down and r.spec.vendor == "claude"]
        if not candidates:
            return False
        new = min(candidates, key=lambda r: scheduler._burn(accounts.get(r.account.name, {})))
        new.benched = False
        self.release_task_of(new, f"{new.name} became the lead")
        if new.runner:
            new.stopping = True
            new.runner.stop()
            new.stopping = False
        old.spec.role, new.spec.role = "member", "lead"
        self.store.set("lead", new.name)
        self.store.update_seat(old.name, role="member")
        self.store.update_seat(new.name, role="lead")
        recent = "\n".join(_fmt_msg(m, 300) for m in self.store.recent_messages(30))
        briefing = (f"You are now the LEAD: {old.name} became unavailable ({reason}). Take over calmly.\n\n"
                    f"{self.brief_text()}\n\nBoard:\n{self._board_text()}\n\nRecent chat:\n{recent}\n\n"
                    "Continue from here: keep the plan unless it is wrong, unblock the team, and finish the project.")
        self.start_seat(new, briefing)
        self.say(f"{old.name} is unavailable; {new.name} is now the lead.", urgent=True)
        self.store.event("lead_change", frm=old.name, to=new.name, reason=reason)
        return True

    def release_task_of(self, rt: SeatRT, reason: str) -> None:
        row = self.store.seat(rt.name) or {}
        if row.get("current_task"):
            tid = row["current_task"]
            task = self.store.task(tid)
            if task and task["status"] in ("in_progress", "changes", "blocked"):
                gitops.park_worktree(rt.worktree, rt.name)
                self.store.append_note(tid, "crew", f"Handed over: {reason}")
                self.store.update_task(tid, status="todo", owner=None, suggested_owner=None)
            self.store.update_seat(rt.name, current_task=None)

    # =============================================================== failover

    def handle_parked(self, modes: dict[str, str]) -> None:
        """Seats whose account is parked move to a same-vendor account with headroom, or wait."""
        usable = [a for a in self.store.accounts() if modes.get(a["name"]) != "parked"]
        if not usable and any(not rt.down for rt in self.seats.values()):
            earliest = min((a.get("parked_until") or 0) for a in self.store.accounts())
            if now() - self.all_parked_notice > 1800:
                self.all_parked_notice = now()
                self.say(f"Every subscription is at its limit. The team pauses and resumes at {hhmm(earliest)}. "
                         "Nothing is lost.")
            return
        work_waiting = bool(self.store.ready_tasks()) or bool(self.store.tasks(("changes",)))
        for rt in self.seats.values():
            if rt.down or rt.busy or rt.runner is None or modes.get(rt.account.name) != "parked":
                continue
            row = self.store.seat(rt.name) or {}
            if row.get("current_task") or rt.pending or work_waiting:
                self.failover(rt, reason="account at its limit")

    def failover(self, rt: SeatRT, reason: str) -> None:
        modes = {a["name"]: a.get("mode") for a in self.store.accounts()}
        same_vendor = [a for a in self.cfg.accounts_for(rt.spec.vendor)
                       if a.name != rt.account.name and modes.get(a.name) != "parked"]
        session = (self.store.seat(rt.name) or {}).get("session_id")
        if same_vendor:
            accs = {a["name"]: a for a in self.store.accounts()}
            target = min(same_vendor, key=lambda a: scheduler._burn(accs.get(a.name, {})))
            t0 = now()
            # Claude conversations move between accounts intact; Codex ones restart from the handover notes.
            copied = bool(rt.spec.vendor == "claude" and session and copy_claude_session(session, rt.account, target))
            old = rt.account
            if rt.runner:
                rt.stopping = True
                rt.runner.stop()
                rt.stopping = False
            rt.account = target
            self.store.update_seat(rt.name, account=target.name)
            row = self.store.seat(rt.name) or {}
            task = self.store.task(row["current_task"]) if row.get("current_task") else None
            doing = task and task["status"] == "in_progress"
            if copied:
                msg = (f"You were moved from {old.name} to {target.name} because of: {reason}. Your conversation is "
                       "intact. " + (f"Continue task #{task['id']}." if doing else "Carry on."))
            else:
                msg = (f"You were moved from {old.name} to {target.name} because of: {reason}. This is a fresh "
                       "conversation, so first read "
                       + (f"task #{task['id']} (team_task_detail), its handover notes, and the work already on your "
                          f"branch (git log / git diff {self.integration}...HEAD), then continue it."
                          if doing else "the team chat, then wait for your next assignment."))
            self.start_seat(rt, msg, resume_session=session if copied else None)
            self.store.event("failover", seat=rt.name, frm=old.name, to=target.name, seconds=now() - t0,
                             kept_context=copied)
            self.say(f"{rt.name} now runs on {target.name} (conversation kept: {'yes' if copied else 'no — continuing from its notes'}).")
            return
        # No same-vendor capacity: hand the task to a seat of the other vendor via its notes, branch and diff.
        row = self.store.seat(rt.name) or {}
        if row.get("current_task"):
            self.release_task_of(rt, f"{rt.account.name} is at its limit; continue from the notes and the branch")
            self.say(f"Task #{row['current_task']} goes back to the board so another seat continues it.")
        if rt.runner:
            rt.stopping = True
            rt.runner.stop()
            rt.stopping = False
        rt.runner = None
        self.store.update_seat(rt.name, status="waiting", note=f"waiting for {rt.account.name} to reset")

    def wake_waiting(self, modes: dict[str, str]) -> None:
        for rt in self.seats.values():
            if rt.runner is None and not rt.down and not rt.benched and modes.get(rt.account.name) != "parked" \
                    and rt.cooldown_until <= now() and self.phase() in ("plan", "build", "deliver"):
                session = (self.store.seat(rt.name) or {}).get("session_id")
                self.start_seat(rt, None, resume_session=session)
                self.say(f"{rt.account.name} has usage again; {rt.name} is back.")

    # ================================================================== chat

    def route_chat(self) -> None:
        new = self.store.messages_after(self.chat_seen, 500)
        if not new:
            return
        self.chat_seen = new[-1]["id"]
        with (self.run_dir / "chat.md").open("a", encoding="utf-8") as fh:
            for m in new:
                fh.write(f"**{hhmm(m['ts'])} {m['sender']}** [{m['kind']}] {self.redact(m['text'])}\n\n")
        for m in new:
            if m["kind"] == "decision" or m["sender"] == "you":
                self.last_progress = max(self.last_progress, m["ts"])

    def _wants_wake(self, rt: SeatRT, unread: list[dict]) -> bool:
        """Wake an idle agent only when a message needs it: tokens are spent on work, not on reading chatter."""
        is_lead = rt.name == self.lead_name
        for m in unread:
            if _mentions(m["text"], rt.name):
                return True
            if is_lead and (m["sender"] == "you" or m["kind"] in ("question", "blocker", "concern")):
                return True
        return False

    def deliver_pending(self) -> None:
        """Send idle seats their instructions plus the chat they missed (only when there is a reason)."""
        for rt in self.seats.values():
            if rt.down or rt.busy or rt.runner is None or not rt.runner.alive():
                continue
            if isinstance(rt.runner, CodexSeat) and rt.runner.busy:
                continue
            unread = self.store.unread(rt.name)
            if not rt.pending and not (unread and self._wants_wake(rt, unread)):
                continue
            parts = list(rt.pending)
            rt.pending.clear()
            if unread:
                parts.append(prompts.chat_digest(unread))
                self.store.mark_read(rt.name, unread[-1]["id"])
            running = getattr(getattr(rt.runner, "setup", None), "effort", None)
            if rt.want_effort and running and running != rt.want_effort:
                if isinstance(rt.runner, CodexSeat):
                    rt.runner.setup.effort = rt.want_effort  # Codex takes the effort per turn
                    self.store.update_seat(rt.name, effort=rt.want_effort)
                else:  # Claude Code sets effort per process: resume the same conversation at the new effort
                    session = (self.store.seat(rt.name) or {}).get("session_id")
                    rt.stopping = True
                    rt.runner.stop()
                    rt.stopping = False
                    self.log(f"{rt.name}: effort {running} → {rt.want_effort}")
                    self.start_seat(rt, "\n\n".join(parts), resume_session=session, effort=rt.want_effort)
                    continue
            rt.busy = True
            rt.turn_started = rt.last_event = now()
            self.store.update_seat(rt.name, status="busy")
            try:
                rt.runner.send("\n\n".join(parts))
            except (RuntimeError, OSError, ValueError) as exc:
                rt.busy = False
                rt.pending[:0] = parts[:-1] if unread else parts
                self.restart_seat(rt, f"could not deliver a message: {exc}")

    # ================================================================ progress

    def track_progress(self) -> None:
        for t in self.store.tasks():
            old = self.task_snapshot.get(t["id"])
            if old != t["status"]:
                self.task_snapshot[t["id"]] = t["status"]
                if old is not None:
                    self.store.event("task_status", task_id=t["id"], frm=old, to=t["status"])
                self.last_progress = now()
                self.stall_count = 0

    def watchdog(self) -> None:
        stall = self.cfg.team.stall_minutes * 60
        for rt in self.seats.values():
            # An idle agent must never sit on an unfinished task (whatever path led there).
            if (not rt.down and not rt.busy and rt.runner is not None and not rt.pending
                    and now() - rt.last_event > max(60.0, min(stall, 180.0)) and self.phase() == "build"):
                row = self.store.seat(rt.name) or {}
                task = self.store.task(row["current_task"]) if row.get("current_task") else None
                if task and task["owner"] == rt.name and task["status"] == "in_progress":
                    rt.last_event = now()
                    self.after_turn(rt)
        for rt in self.seats.values():
            if rt.down or not rt.busy or rt.runner is None:
                continue
            quiet = now() - rt.last_event
            limit = stall
            if rt.last_label.startswith("Bash"):
                limit = max(stall, self.cfg.team.checks_timeout_minutes * 60)
            if quiet < limit:
                continue
            if rt.nudges == 0 or now() - rt.last_nudge > limit:
                rt.nudges += 1
                rt.last_nudge = now()
                if rt.nudges <= 1:
                    self.log(f"watchdog: {rt.name} silent {human_duration(quiet)}; interrupting")
                    rt.runner.interrupt()
                    rt.pending.append("You were interrupted after a long silence. Record a one-line status with "
                                      "team_task_note, then continue. If you are stuck, block the task.")
                else:
                    rt.nudges = 0
                    self.restart_seat(rt, f"no activity for {human_duration(quiet)}")
        # Board-level stall (Magentic-One style progress ledger)
        if self.phase() != "build":
            return
        open_tasks = self.store.tasks(("todo", "in_progress", "review", "approved", "changes", "blocked"))
        if not open_tasks or now() - self.last_progress < self.cfg.team.ledger_minutes * 60:
            return
        self.stall_count += 1
        self.last_progress = now()
        ledger = "\n".join(f"#{t['id']} {t['status']} {t['title']} [{t['owner'] or '-'}]"
                           + (f" blocked: {t['block_reason']}" if t['block_reason'] else "") for t in open_tasks)
        self.store.event("stall", count=self.stall_count)
        if self.stall_count <= 2:
            self.seats[self.lead_name].pending.append(
                f"PROGRESS STALLED: no task has moved for {self.cfg.team.ledger_minutes:.0f} minutes.\n{ledger}\n"
                "Replan now: split or simplify stuck tasks, cancel what is not needed, unblock with a decision, "
                "or reassign. Then keep the team moving.")
            self.say("No progress for a while; the lead is replanning.")
        elif self.stall_count == 3:
            self.store.event("escalation", seat="crew", question="The team has stalled three times. Ledger:\n"
                             + ledger + "\nDecide how to proceed so the project finishes with full quality.")
        else:
            self.say("The team could not make progress after replanning and a ruling. Stopping with an honest report.")
            self.set_phase("stopped")

    # ================================================================== plan

    def plan_phase(self) -> None:
        ready_at = self.store.get("plan_ready_at")
        if not ready_at or "ceo_plan" in self.jobs:
            if not ready_at and now() - self.started > 40 * 60:
                lead = self.seats[self.lead_name]
                if not lead.pending and not lead.busy and lead.nudges < 3:
                    lead.nudges += 1
                    lead.pending.append("The team is waiting for your plan. Create the tasks and call team_plan_ready.")
            return
        if self.plan_reviewed or not self.cfg.team.ceo_reviews or self.plan_revisions >= 2:
            self.start_build()
            return
        if self.store.get("verdict:plan"):
            verdict = self.store.get("verdict:plan")
            self.store.set("verdict:plan", None)
            self.plan_reviewed = verdict["verdict"] == "approve"
            if self.plan_reviewed:
                if verdict.get("notes"):
                    self.seats[self.lead_name].pending.append(
                        "The CEO approved the plan with these adjustments (apply them with team_task_edit/create if "
                        "they improve the plan, then carry on):\n" + verdict["notes"])
                self.start_build()
            else:
                self.plan_revisions += 1
                self.store.set("plan_ready_at", None)
                self.seats[self.lead_name].pending.append(
                    "The CEO requires changes to the plan before the build starts:\n" + verdict["notes"]
                    + "\nRevise the tasks (team_task_edit / team_task_create / team_task_cancel), then call "
                    "team_plan_ready again.")
            return
        self.start_job("ceo_plan", self._ceo_plan_job)

    def _board_text(self) -> str:
        return "\n".join(
            f"#{t['id']} [{t['status']}] {t['title']} size {t['size']} kind {t['kind']} effort {t.get('effort') or '-'}"
            f" owner→{t['suggested_owner'] or '-'}"
            f" deps {t['depends_on']} scope {t['scope']}\n    spec: {clip(t['spec'], 400)}\n    accept: {clip(t['acceptance'], 300)}"
            for t in self.store.tasks())

    def _ceo_plan_job(self) -> None:
        prompt = prompts.ceo_plan_prompt(self.brief_text(), self.store.get("plan_summary", ""), self._board_text(),
                                         lessons.render_for_ceo())
        res = self.run_ceo("ceo-plan", prompt)
        if not self.store.get("verdict:plan"):
            self.log(f"CEO plan review gave no verdict ({clip(res.text, 200)}); proceeding")
            self.plan_reviewed = True

    def start_build(self) -> None:
        if self.phase() != "plan":
            return
        self.set_phase("build")
        self.last_progress = now()
        self.say("Plan approved. Build started.", urgent=True)

    # ================================================================= build

    def build_phase(self, modes: dict[str, str]) -> None:
        self.dispatch_reviews()
        self.dispatch_merges()
        self.dispatch_returns()
        self.assign_work(modes)
        self.check_completion()

    def assign_work(self, modes: dict[str, str]) -> None:
        ready = self.store.ready_tasks()
        if not ready:
            return
        idle = [rt for rt in self.seats.values()
                if not rt.down and not rt.busy and rt.runner is not None and not rt.pending
                and not (self.store.seat(rt.name) or {}).get("current_task")]
        if not idle:
            return
        accounts = {a["name"]: a for a in self.store.accounts()}
        idle_rows = [self.store.seat(rt.name) for rt in idle]
        idle_names = {rt.name for rt in idle}
        lead_rt = self.seats.get(self.lead_name)
        lead_alive = lead_rt is not None and not lead_rt.down
        for task in ready:
            owner = task.get("suggested_owner")
            if owner and owner == self.lead_name:
                # The lead's own tasks (usually the foundation) are never taken over while the lead is alive.
                self.grace_until[task["id"]] = float("inf") if lead_alive else 0.0
            elif owner and task["id"] not in self.grace_until:
                self.grace_until[task["id"]] = now() + 180
        cost_model = lessons_cost_model()
        for row in scheduler.order_idle_seats(idle_rows, accounts):
            rt = self.seats[row["name"]]
            acc = accounts.get(rt.account.name, {})
            task = scheduler.choose_task(row, ready, acc, modes.get(rt.account.name, "normal"), cost_model,
                                         row.get("model") or "", idle_names, self.grace_until)
            if task is None:
                continue
            if self.give_task(rt, task, modes.get(rt.account.name, "normal")):
                ready = [t for t in ready if t["id"] != task["id"]]
                ready = [t for t in ready if not self.store.lease_conflicts(t)]
                idle_names.discard(rt.name)

    def _free_branch(self, branch: str, keep: SeatRT | None = None) -> None:
        for other in self.seats.values():
            if other is keep:
                continue
            probe = gitops.git(other.worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
            if probe == branch:
                gitops.park_worktree(other.worktree, other.name)

    def give_task(self, rt: SeatRT, task: dict, mode: str) -> bool:
        branch = task["branch"] or f"{self.prefix}/task-{task['id']}"
        resumed = bool(task["branch"])
        try:
            self._free_branch(branch, keep=rt)
            gitops.checkout_task(rt.worktree, branch, self.integration)
            task = self.store.start_task(task["id"], rt.name, branch)
        except (StoreError, gitops.GitError) as exc:
            self.log(f"could not give #{task['id']} to {rt.name}: {exc}")
            return False
        self.store.update_seat(rt.name, current_task=task["id"], chat_used=0)
        rt.idle_nudges = 0
        rt.want_effort = self.task_effort(task)
        rt.pending.append(prompts.assignment(task, branch, mode, resumed=resumed))
        self.say(f"Task #{task['id']} → {rt.name}: {task['title']}", task_id=task["id"])
        return True

    def dispatch_returns(self) -> None:
        """Tasks sent back by review (or conflicts) return to their owner, or to a free seat after a grace period."""
        for task in self.store.tasks(("changes",)):
            owner = self.seats.get(task["owner"] or "")
            row = self.store.seat(owner.name) if owner else None
            owner_free = owner and not owner.down and not owner.busy and owner.runner is not None and not owner.pending \
                and (not row.get("current_task") or row.get("current_task") == task["id"])
            if owner_free:
                try:
                    self._free_branch(task["branch"], keep=owner)
                    gitops.checkout_task(owner.worktree, task["branch"], self.integration)
                except gitops.GitError as exc:
                    self.log(f"return #{task['id']}: {exc}")
                    continue
                self.store.update_task(task["id"], status="in_progress")
                self.store.update_seat(owner.name, current_task=task["id"], chat_used=0)
                owner.want_effort = self.task_effort(task)
                owner.pending.append(prompts.assignment(self.store.task(task["id"]), task["branch"],
                                                        (self.store.account(owner.account.name) or {}).get("mode", "normal"),
                                                        resumed=True))
            elif self.grace_until.setdefault(-task["id"], now() + 300) < now():
                self.store.append_note(task["id"], "crew", f"Reassigned: {task['owner']} was busy elsewhere.")
                self.store.update_task(task["id"], status="todo", owner=None, suggested_owner=None)
                self.grace_until.pop(-task["id"], None)

    # ---------------------------------------------------------------- review

    def dispatch_reviews(self) -> None:
        for task in self.store.tasks(("review",)):
            key = f"review-{task['id']}"
            if key in self.jobs:
                continue
            if not self.store.get(f"review_sha:{task['id']}"):  # snapshot once; a resumed run reuses it
                owner = self.seats.get(task["owner"] or "")
                if owner is not None and owner.busy and now() - (task["submitted_at"] or now()) < 180:
                    continue  # let the author finish its turn before the work is snapshotted
                if owner is not None:
                    gitops.commit_all(owner.worktree, f"task #{task['id']}: {clip(task['summary'] or task['title'], 70)}")
                    self.store.update_seat(owner.name, current_task=None)
                    sha = gitops.head(owner.worktree)
                else:
                    sha = gitops.out(self.repo, "rev-parse", task["branch"])
                self.store.set(f"review_sha:{task['id']}", sha)
            self.store.set(f"review:{task['id']}", None)
            if self.cfg.team.review == "off":
                self.job_results.put((key, ("approve", "Review is switched off in settings.", None)))
                self.jobs[key] = threading.current_thread()
                continue
            self.start_job(key, self._review_job, task["id"])

    def _review_job(self, task_id: int) -> tuple[str, str, str | None]:
        task = self.store.task(task_id)
        wt = self.run_dir / "worktrees" / f"_review-{task_id}"
        sha = self.store.get(f"review_sha:{task_id}") or task["branch"]
        gitops.remove_worktree(self.repo, wt)
        gitops.git(self.repo, "worktree", "add", "-f", "--detach", str(wt), sha)
        try:
            checks = self.store.get("checks", []) or []
            result = gitops.run_checks(wt, checks, self.run_dir / "logs" / f"checks-task-{task_id}.log",
                                       self.cfg.team.checks_timeout_minutes * 60, env=None)
            if result.ran and not result.ok:
                return ("changes", "The checks fail on your branch:\n" + result.summary, "checks")
            author_vendor = self.seats[task["owner"]].spec.vendor if task["owner"] in self.seats else "claude"
            prefer = ({"claude": "codex", "codex": "claude"}[author_vendor] if self.cfg.team.review == "cross"
                      else author_vendor)
            for attempt in range(2):
                self.store.set(f"review:{task_id}", None)
                res = self.run_reviewer(task, prefer if attempt == 0 else None, result.summary if result.ran else "")
                verdict = self.store.get(f"review:{task_id}")
                if verdict:
                    return (verdict["verdict"], verdict["notes"], verdict["by"])
                self.log(f"reviewer for #{task_id} gave no verdict: {clip(res.text, 300)}")
            if result.ran and result.ok:
                return ("approve", "Reviewer unavailable twice; approved on passing checks.", None)
            return ("changes", "No reviewer could review this task and no checks are set. The lead should set checks.", None)
        finally:
            gitops.remove_worktree(self.repo, wt)

    def run_reviewer(self, task: dict, prefer_vendor: str | None, check_log: str) -> RunResult:
        modes = {a["name"]: a.get("mode") for a in self.store.accounts()}
        owner_acc = self.seats[task["owner"]].account.name if task["owner"] in self.seats else None
        acc_row = scheduler.pick_account(self.store.accounts(), modes, prefer_vendor, avoid=owner_acc)
        if acc_row is None:
            return RunResult(is_error=True, text="no account available")
        account = self.cfg.account(acc_row["name"])
        prompt = prompts.reviewer_prompt(task, self.integration, self.store.get("checks", []) or [], check_log)
        wt = self.run_dir / "worktrees" / f"_review-{task['id']}"
        name = f"reviewer-{task['id']}"
        self.say(f"Reviewing task #{task['id']} with fresh eyes ({account.vendor}, {account.name}).", task_id=task["id"])
        self.store.event("oneoff", seat=name, task_id=task["id"], state="start", role="reviewer", vendor=account.vendor,
                         account=account.name, effort=self.task_effort(task),
                         model=self.cfg.models.work if account.vendor == "claude" else (self.cfg.models.codex or ""))
        if account.vendor == "codex":
            res = run_once_codex(prompt, seat=name, role="reviewer", account=account, workdir=wt,
                                 setup=self._codex_setup(effort=self.task_effort(task)), redact=self.redact,
                                 task_id=task["id"], read_only=True)
        else:
            res = run_once_claude(prompt, seat=name, role="reviewer", account=account, workdir=wt,
                                  setup=self._claude_setup(effort=self.task_effort(task)), redact=self.redact,
                                  task_id=task["id"], read_only=True)
        self._account_usage(account.name, res)
        self.store.event("oneoff", seat=name, task_id=task["id"], state="error" if res.is_error else "done",
                         tokens=res.tokens, seconds=round(res.duration_s or 0))
        return res

    def on_review_done(self, task_id: int, outcome: tuple[str, str, str | None]) -> None:
        verdict, notes, by = outcome
        task = self.store.task(task_id)
        if task is None or task["status"] != "review":
            return
        rounds = task["review_rounds"] + 1
        self.store.event("review", task_id=task_id, verdict=verdict, by=by, rounds=rounds)
        if verdict != "approve":
            self.store.set(f"review_sha:{task_id}", None)
        if verdict == "approve":
            self.store.update_task(task_id, status="approved", review_notes=notes, review_rounds=rounds)
            self.say(f"Task #{task_id} approved{f' by {by}' if by else ''}.", task_id=task_id)
            return
        self.store.update_task(task_id, status="changes", review_notes=notes, review_rounds=rounds)
        self.say(f"Task #{task_id} needs changes (round {rounds}): {clip(notes, 400)}", task_id=task_id)
        if rounds > self.cfg.team.max_review_rounds:
            self.seats[self.lead_name].pending.append(
                f"Task #{task_id} has failed review {rounds} times. Decide: clarify the spec (team_task_edit), split it, "
                f"or record a binding decision on the disputed points. Latest review:\n{clip(notes, 2000)}")

    # ----------------------------------------------------------------- merge

    def dispatch_merges(self) -> None:
        if self.merging:
            return
        approved = self.store.tasks(("approved",))
        if approved:
            self.merging = True
            self.start_job(f"merge-{approved[0]['id']}", self._merge_job, approved[0]["id"])

    def _merge_job(self, task_id: int) -> tuple[str, str]:
        task = self.store.task(task_id)
        sha = self.store.get(f"review_sha:{task_id}") or task["branch"]
        res = gitops.merge_into(self.main_wt, sha, f"crew: task #{task_id} {task['title']}")
        if not res.ok:
            return ("conflict", ", ".join(res.conflicts)) if res.is_conflict else ("error", res.message)
        checks = self.store.get("checks", []) or []
        result = gitops.run_checks(self.main_wt, checks, self.run_dir / "logs" / f"checks-merge-{task_id}.log",
                                   self.cfg.team.checks_timeout_minutes * 60)
        if result.ran and not result.ok:
            gitops.clean_worktree(self.main_wt)
            gitops.revert_last_merge(self.main_wt, f"task #{task_id} broke the checks")
            return ("red", result.summary)
        gitops.clean_worktree(self.main_wt)
        return ("merged", gitops.head(self.main_wt))

    def on_merge_done(self, task_id: int, outcome: tuple[str, str]) -> None:
        self.merging = False
        status, detail = outcome
        task = self.store.task(task_id)
        self.store.set(f"review_sha:{task_id}", None)
        if status == "merged":
            self.store.update_task(task_id, status="merged", finished_at=now())
            self.store.event("merged", task_id=task_id, tokens=task["tokens"], size=task["size"],
                             seconds=now() - (task["started_at"] or now()), rounds=task["review_rounds"])
            effort = self.task_effort(task)
            if effort != "auto":
                lessons.record_effort_outcome(task["kind"], task["size"], effort, task["review_rounds"],
                                              (now() - (task["started_at"] or now())) / 60, task["tokens"],
                                              project=self.store.get("project_name", "") or "")
            self.say(f"Task #{task_id} merged into the team's result. ✔", task_id=task_id)
        elif status == "error":
            fails = int(self.store.get(f"merge_errors:{task_id}", 0) or 0) + 1
            self.store.set(f"merge_errors:{task_id}", fails)
            self.log(f"merge of #{task_id} failed (not a conflict): {detail}")
            if fails >= 3:
                self.store.update_task(task_id, status="blocked",
                                       block_reason=f"The orchestrator could not merge it: {clip(detail, 500)}")
                self.say(f"Task #{task_id} could not be merged three times for a technical reason; the lead decides.",
                         urgent=True, task_id=task_id)
            else:
                self.store.update_task(task_id, status="approved")  # retry on the next tick
            return
        elif status == "conflict":
            self._bounce(task_id)
            self.store.update_task(task_id, status="changes",
                                   review_notes=f"Merge conflict with the team's latest work in: {detail}. "
                                                f"Merge `{self.integration}` into your branch, resolve, re-verify, resubmit.")
            self.say(f"Task #{task_id} conflicts with newer work ({clip(detail, 200)}); back to its owner.", task_id=task_id)
        else:
            self._bounce(task_id)
            self.store.update_task(task_id, status="changes",
                                   review_notes="After merging, the checks failed (the merge was undone):\n" + detail)
            self.say(f"Task #{task_id} broke the checks once combined with the others; undone and returned.",
                     task_id=task_id)

    def _bounce(self, task_id: int) -> None:
        """Count merge-time returns; a task that keeps bouncing goes to the lead instead of looping forever."""
        n = int(self.store.get(f"bounces:{task_id}", 0) or 0) + 1
        self.store.set(f"bounces:{task_id}", n)
        if n >= 3:
            self.seats[self.lead_name].pending.append(
                f"Task #{task_id} has come back from merging {n} times (conflicts or failing checks). Find the root "
                "cause — overlapping scopes, a shared file that needs one owner, or a broken check — and fix the plan "
                "(team_task_edit, team_decide, or take the task yourself).")

    # ------------------------------------------------------------ completion

    def check_completion(self) -> None:
        tasks = self.store.tasks()
        if not tasks or any(t["status"] not in ("merged", "cancelled") for t in tasks):
            return
        if self.store.get("done_requested_at"):
            self.set_phase("deliver")
            return
        lead = self.seats[self.lead_name]
        asked = self.store.get("completion_asked")
        if asked and self.store.get("completion_task_count") != len(tasks):
            asked = None
        if not lead.busy and not lead.pending and not asked:
            self.store.set("completion_asked", now())
            self.store.set("completion_task_count", len(tasks))
            self._sync_lead_to_integration(lead)
            if self.store.get("mode") == "solo":
                lead.pending.append(
                    "Every task is merged: your work passed its review. Your worktree shows the merged result. If you "
                    "know of any remaining gap, fix it here and commit; otherwise call team_project_done now with the "
                    "plain-language report for the owner (what was built, how to use it, what was verified, limits).")
                return
            lead.pending.append(
                "Every task is merged. Your worktree now shows the team's combined result. Verify the whole project "
                "against the brief yourself (run it, test it, look at it). Fix small gaps directly on this branch "
                "(commit them), or create tasks for bigger ones. When it is right, call team_project_done with the "
                "plain-language report for the owner.")

    def _sync_lead_to_integration(self, lead: SeatRT) -> None:
        branch = f"{self.prefix}/final"
        self._free_branch(branch, keep=lead)
        gitops.checkout_task(lead.worktree, branch, self.integration)
        gitops.git(lead.worktree, "merge", "--no-edit", self.integration, check=False)

    # ================================================================ deliver

    def deliver_phase(self) -> None:
        if "final" in self.jobs:
            return
        if self.store.get("delivered"):
            self.set_phase("done")
            return
        self.start_job("final", self._final_job)

    def _final_job(self) -> str:
        lead = self.seats[self.lead_name]
        final_branch = f"{self.prefix}/final"
        if gitops.git(self.repo, "rev-parse", "--verify", final_branch, check=False).returncode == 0:
            gitops.commit_all(lead.worktree, "crew: lead's final touches")
            res = gitops.merge_into(self.main_wt, final_branch, "crew: final touches")
            if not res.ok:
                self.log(f"final touches conflict: {res.conflicts}")
        checks = self.store.get("checks", []) or []
        gitops.clean_worktree(self.main_wt)
        result = gitops.run_checks(self.main_wt, checks, self.run_dir / "logs" / "checks-final.log",
                                   self.cfg.team.checks_timeout_minutes * 60)
        gitops.clean_worktree(self.main_wt)
        if result.ran and not result.ok:
            return "red:" + result.summary
        if self.cfg.team.ceo_reviews and self.final_rounds < 1:
            self.final_rounds += 1
            self.store.set("verdict:final", None)
            prompt = prompts.ceo_final_prompt(self.brief_text(), self.store.get("done_report", ""), checks,
                                              self.store.get("base_commit"))
            self.run_ceo("ceo-final", prompt, workdir=self.main_wt)
            verdict = self.store.get("verdict:final") or {}
            if verdict.get("verdict") == "changes":
                return "changes:" + verdict.get("notes", "")
        return "ok"

    def on_final_done(self, outcome: str) -> None:
        if outcome.startswith("red:") or outcome.startswith("changes:"):
            kind, _, detail = outcome.partition(":")
            what = "The final checks fail" if kind == "red" else "The CEO's final review requires changes"
            self.store.set("done_requested_at", None)
            self.store.set("completion_asked", None)
            self.set_phase("build")
            lead = self.seats[self.lead_name]
            lead.pending.append(f"{what}:\n{clip(detail, 3000)}\nFix these (create tasks or fix directly on your "
                                "final branch), verify, then call team_project_done again.")
            self.say(f"{what}; the team is fixing it before delivery.", urgent=True)
            return
        self.deliver()

    def deliver(self) -> None:
        mode = self.cfg.team.deliver
        base = self.store.get("base_branch")
        where = f"the branch `{self.integration}`"
        if mode in ("merge", "push") and base and not gitops.is_dirty(self.repo) \
                and gitops.current_branch(self.repo) == base:
            res = gitops.git(self.repo, "merge", "--no-edit", self.integration, check=False)
            if res.returncode == 0:
                where = f"your project folder ({self.repo})"
            else:
                gitops.git(self.repo, "merge", "--abort", check=False)
                self.log("deliver merge failed: " + clip(res.stderr, 400))
        if mode == "push":
            push = gitops.git(self.repo, "push", "origin", "HEAD", check=False, timeout=600)
            if push.returncode != 0:
                self.log("push failed: " + clip(push.stderr, 400))
        self.store.set("delivered", {"at": now(), "where": where})
        self.write_report(where)
        self.say(f"Delivered. The result is in {where}. The full report is ready.", urgent=True)
        self.retrospective()
        self.set_phase("done")

    # ================================================================== CEO

    def solo_effort(self) -> str:
        """In solo mode there is no plan review, so the CEO decides the builder's effort up front."""
        if self.cfg.models.effort_work != "auto":
            return self.cfg.models.effort_work
        res = self.run_ceo("ceo-effort", prompts.ceo_effort_prompt(self.brief_text(), lessons.render_for_ceo()),
                           json_schema=prompts.CEO_EFFORT_SCHEMA, timeout=600)
        choice = res.structured if isinstance(res.structured, dict) else {}
        effort = choice.get("effort") if choice.get("effort") in lessons.EFFORT_ORDER else "high"
        self.say(f"The CEO set the builder's effort to {effort}" + (f": {choice['reason']}" if choice.get("reason")
                                                                     else " (default)."))
        return effort

    def run_ceo(self, name: str, prompt: str, workdir: Path | None = None, json_schema: dict | None = None,
                timeout: float = 2400) -> RunResult:
        modes = {a["name"]: a.get("mode") for a in self.store.accounts()}
        claude_accounts = [a for a in self.store.accounts() if a["vendor"] == "claude"]
        acc_row = scheduler.pick_account(claude_accounts, modes)
        if acc_row is None:
            return RunResult(is_error=True, text="no Claude account available")
        account = self.cfg.account(acc_row["name"])
        model = self.cfg.models.ceo or self.cfg.models.work
        effort = self.cfg.models.effort_ceo
        self.store.event("oneoff", seat=name, state="start", role="ceo", vendor="claude", account=account.name,
                         model=model, effort=effort if effort != "auto" else "max")
        res = run_once_claude(prompt, seat=name, role="ceo", account=account, workdir=workdir or self.main_wt,
                              setup=self._claude_setup(effort=effort, model=model), redact=self.redact,
                              read_only=True, timeout=timeout, json_schema=json_schema)
        self._account_usage(account.name, res)
        if res.is_error and model != self.cfg.models.work:
            # The CEO model has its own, tighter limit: fall back to the workhorse at maximum effort.
            self.log(f"CEO model unavailable ({clip(res.text, 200)}); using {self.cfg.models.work} at max effort")
            res = run_once_claude(prompt, seat=name, role="ceo", account=account, workdir=workdir or self.main_wt,
                                  setup=self._claude_setup(effort="max"), redact=self.redact,
                                  read_only=True, timeout=timeout, json_schema=json_schema)
            self._account_usage(account.name, res)
        self.store.event("oneoff", seat=name, state="error" if res.is_error else "done", tokens=res.tokens,
                         seconds=round(res.duration_s or 0))
        return res

    def process_escalations(self) -> None:
        for ev in self.store.events("escalation"):
            if ev["id"] in self.processed_escalations or f"ruling-{ev['id']}" in self.jobs:
                continue
            self.processed_escalations.add(ev["id"])
            context = self._board_text() + "\n\nRecent chat:\n" + "\n".join(
                _fmt_msg(m, 300) for m in self.store.recent_messages(25))
            prompt = prompts.ceo_ruling_prompt(ev["data"].get("question", ""), context)
            self.start_job(f"ruling-{ev['id']}", self.run_ceo, f"ceo-ruling-{ev['id']}", prompt)

    # ================================================================== jobs

    def start_job(self, key: str, fn, *args) -> None:
        def runner():
            try:
                result = fn(*args)
            except Exception as exc:  # a failed job must never kill the loop
                self.log(f"job {key} failed: {traceback.format_exc()}")
                result = exc
            self.job_results.put((key, result))

        thread = threading.Thread(target=runner, daemon=True, name=f"job-{key}")
        self.jobs[key] = thread
        thread.start()

    def drain_jobs(self) -> None:
        while True:
            try:
                key, result = self.job_results.get_nowait()
            except queue.Empty:
                return
            self.jobs.pop(key, None)
            if isinstance(result, Exception):
                if key.startswith("merge-"):
                    self.merging = False
                    tid = int(key.split("-")[1])
                    gitops.git(self.main_wt, "merge", "--abort", check=False)
                    self.store.update_task(tid, status="changes", review_notes=f"Merge failed: {result}")
                elif key.startswith("review-"):
                    tid = int(key.split("-")[1])
                    self.store.update_task(tid, status="changes", review_notes=f"Review failed to run: {result}")
                elif key == "final":
                    self.on_final_done(f"red:{result}")
                continue
            if key.startswith("review-"):
                self.on_review_done(int(key.split("-")[1]), result)
            elif key.startswith("merge-"):
                self.on_merge_done(int(key.split("-")[1]), result)
            elif key == "final":
                self.on_final_done(result)

    # ================================================================ usage

    def _account_usage(self, account: str, res: RunResult) -> None:
        self.store.add_account_usage(account, tokens=res.tokens, cost=res.cost_usd)
        if res.rate:
            scheduler.apply_rate(self.store, account, res.rate)

    # =============================================================== report

    def write_report(self, where: str) -> Path:
        brief = self.store.get("brief", {}) or {}
        tasks = self.store.tasks()
        merged = [t for t in tasks if t["status"] == "merged"]
        elapsed = human_duration(now() - self.started)
        reviews = self.store.events("review")
        first_pass = sum(1 for e in reviews if e["data"].get("verdict") == "approve" and e["data"].get("rounds") == 1)
        failovers = self.store.events("failover")
        lines = [
            f"# {brief.get('title') or 'Your project'}",
            "",
            self.redact(self.store.get("done_report", "") or "The team finished the work."),
            "",
            "## Where it is",
            f"The finished work is in {where}.",
            "",
            "## How it went",
        ]
        if self.store.get("mode") == "solo":
            lines.append(f"- Time: {elapsed}. One builder did the work; a reviewer with fresh eyes and the CEO model "
                         "checked it.")
        else:
            active = sum(1 for rt in self.seats.values() if not rt.benched)
            lines.append(f"- Time: {elapsed}, with {active} agents working in parallel." if active > 1 else
                         f"- Time: {elapsed}, with one agent building and others checking.")
        pieces = len(merged)
        lines.append(f"- {pieces} {'piece' if pieces == 1 else 'pieces'} of work built, each checked by a reviewer "
                     f"who had not written it ({first_pass} approved at the first review).")
        if failovers:
            lines.append(f"- Usage limits were handled {len(failovers)} time(s) by moving work to another subscription.")
        lines += ["", "## Subscriptions used"]
        for acc in self.store.accounts():
            util = "" if acc["util_5h"] is None else f", {acc['util_5h'] * 100:.0f}% of the 5-hour allowance"
            lines.append(f"- {acc['name']}: {acc['tokens']:,} tokens{util}")
        path = self.run_dir / "REPORT.md"
        atomic_write(path, "\n".join(lines) + "\n")
        return path

    def retrospective(self) -> None:
        """Save what this run taught us: cost model, failovers, stalls, the CEO's effort lessons, and the lead's own."""
        update_cost_model(self.store, self.cfg)
        try:
            lessons.derive_ceo_lessons()
        except Exception as exc:  # lessons must never break delivery
            self.log(f"CEO lessons: {exc}")
        for ev in self.store.events("failover"):
            d = ev["data"]
            lessons.add("usage", f"Failover {d.get('frm')} → {d.get('to')} kept the conversation "
                        f"({'yes' if d.get('kept_context') else 'no'}) and took {d.get('seconds', 0):.0f}s; the task "
                        "continued without restarting.", source="crew", project=self.store.get("project_name", ""))
        stalls = self.store.events("stall")
        if stalls:
            lessons.add("process", f"A project stalled {len(stalls)} time(s); stalls usually mean tasks were too "
                        "large or blocked on an unmade decision — split tasks and decide early.", source="crew")
        lead = self.seats.get(self.lead_name)
        if lead and lead.runner and lead.runner.alive() and not lead.busy:
            lead.busy = True
            lead.turn_started = now()
            lead.runner.send("The project is delivered. Save 2–4 lessons for future teams with team_lesson_add: what "
                             "made this team fast, what slowed it down, what the reviews caught. Be specific. Then end "
                             "your turn.")
            deadline = now() + 240
            while lead.busy and now() < deadline:
                try:
                    ev = self.events.get(timeout=1)
                except queue.Empty:
                    continue
                if ev.seat == lead.name and ev.kind in ("result", "exit"):
                    lead.busy = False
        lessons.write_playbook()

    # =============================================================== shutdown

    def shutdown(self) -> None:
        for rt in self.seats.values():
            if rt.runner is not None:
                rt.stopping = True
                try:
                    rt.runner.stop()
                except Exception:
                    pass
            try:
                if gitops.is_dirty(rt.worktree):
                    gitops.commit_all(rt.worktree, "crew: work saved at shutdown")
            except Exception:
                pass
            self.store.update_seat(rt.name, status="stopped")
        if self.phase() in ("stopped", "failed") and not (self.run_dir / "REPORT.md").exists():
            self.store.set("done_report", "The run stopped before the work was finished. Everything done so far is "
                           "saved; `crew resume` continues from here.")
            self.write_report(f"the branch `{self.integration}`")
        self.log("shutdown complete")


# ===================================================================== costs


def lessons_cost_model() -> dict:
    from .lessons import _db

    db = _db()
    try:
        row = db.execute("SELECT value FROM memo WHERE key='cost_model'").fetchone()
    finally:
        db.close()
    import json

    return json.loads(row["value"]) if row else {}


def update_cost_model(store: Store, cfg: Config) -> None:
    """Learn tokens per task-size unit (per model) and utilization per token (per account)."""
    import json

    from .lessons import _db

    model = cfg.models.work
    merged = [e for e in store.events("merged") if e["data"].get("tokens")]
    per_unit = [e["data"]["tokens"] / scheduler.SIZE_UNITS.get(e["data"].get("size", "M"), 3) for e in merged]
    util_per_token: dict[str, float] = {}
    for acc in store.accounts():
        rates = [e for e in store.events("rate") if e["data"].get("account") == acc["name"]
                 and e["data"].get("util_5h") is not None]
        if len(rates) >= 2 and acc["tokens"]:
            delta = rates[-1]["data"]["util_5h"] - rates[0]["data"]["util_5h"]
            if delta > 0:
                util_per_token[acc["name"]] = delta / acc["tokens"]
    old = lessons_cost_model()
    tpu = dict(old.get("tokens_per_unit") or {})
    if per_unit:
        new = sum(per_unit) / len(per_unit)
        tpu[model] = new if model not in tpu else 0.6 * tpu[model] + 0.4 * new
    upt = dict(old.get("util_per_token") or {})
    for name, value in util_per_token.items():
        upt[name] = value if name not in upt else 0.6 * upt[name] + 0.4 * value
    db = _db()
    try:
        db.execute("INSERT INTO memo(key,value) VALUES('cost_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (json.dumps({"tokens_per_unit": tpu, "util_per_token": upt}),))
    finally:
        db.close()
