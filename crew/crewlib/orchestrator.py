"""The orchestrator: deterministic coordination around non-deterministic agents.

It owns the process (phases, assignment, reviews, merges, timers, failover,
usage) so the agents can spend their intelligence on the work itself.
Everything it knows lives in the run's SQLite store, so a crashed run can be
resumed.
"""

from __future__ import annotations

import os
import queue
import random
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, lessons, prompts, quality, scheduler, scorecard, tiers
from . import tools as team_tools
from .agents import (ClaudeSeat, ClaudeSetup, CodexSeat, CodexSetup, Event, RunResult, copy_claude_session,
                     run_once_claude, run_once_codex)
from .config import Account, Config, SeatSpec
from .store import Store, StoreError
from .tiers import TIERS, model_label, seat_tier, vendor_of
from .tools import _fmt_msg, _mentions, owner_words
from .usage import limit_resets_at
from .util import Redactor, atomic_write, clip, crew_home, hhmm, human_duration, load_env_file, now

TICK = 1.0
HEARTBEAT = 15.0          # seconds between "this project's orchestrator is alive" notes (read by the app)
ALIVE_SECONDS = 60.0      # a note older than this: the orchestrator is gone
CONTEST_KINDS = ("build", "fix", "test", "docs")  # work whose two versions can be compared side by side
CONTESTS_SOME = 2         # head-to-heads per project when the owner chose "some"
CONTEST_WAIT = 15 * 60    # how long a finished version waits for its rival before it is checked alone
# How far each task status is along the way to done. "changes" and "blocked" are steps back: a task sent back again
# and again (its checks can never pass) goes round in circles, and that is not progress.
STAGES = {"todo": 0, "in_progress": 1, "review": 2, "approved": 3, "merged": 4, "cancelled": 4}


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
    owner_interrupt: float = 0.0  # when the owner interrupted this seat's turn to ask it something

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
        self.task_stage: dict[int, int] = {}  # the furthest stage each task has reached (see STAGES)
        self.chat_seen = 0
        self.backlog_checked = False  # the owner's drafts and CEO questions from while the team was paused
        self.grace_until: dict[int, float] = {}
        self.ready_since: dict[int, float] = {}  # when each to-do task first became ready
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

    def accounts(self) -> list[dict]:
        """The project's subscriptions as recorded (limits, usage), without any the owner has removed since: a seat,
        reviewer or judge must never be given one."""
        names = {a.name for a in self.cfg.accounts}
        return [a for a in self.store.accounts() if a["name"] in names]

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
        st.set("prompt_writer", bool(self.cfg.team.prompt_writer))  # the current setting, on a resume too
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
            known = {a.name for a in self.cfg.accounts_for(spec.vendor)}
            last = prev.get("account")
            account = self.cfg.account(last if last in known else spec.account)
            st.upsert_seat(spec.name, vendor=spec.vendor, role=spec.role, account=account.name, model=model,
                           worktree=str(wt), status="starting")
            if last and last not in known:  # that subscription was removed since: its conversation went with it
                st.update_seat(spec.name, session_id=None)
                self.log(f"{spec.name}: {last} is no longer one of the subscriptions; continuing on {account.name}")
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
        (made at plan review), else a sensible default from the task's tier, size and kind."""
        fixed = self.cfg.models.effort_work
        if fixed and fixed != "auto":
            return fixed
        if task and task.get("effort"):
            return task["effort"]
        if not task:
            return "auto"
        if task.get("kind") in ("research", "verify", "docs") or task.get("tier") == "workhorse":
            return "medium"  # routine work (GPT-6 Sol's own default)
        return {"S": "medium", "M": "high", "L": "xhigh"}.get(task.get("size") or "M", "high")

    def review_effort(self, task: dict) -> str:
        """The manager checking a task thinks at least as hard as its builder, and never below medium."""
        fixed = self.cfg.models.effort_light
        if fixed and fixed != "auto":
            return fixed
        effort = self.task_effort(task)
        if effort in ("auto", "low"):
            return "medium"
        return effort

    def _codex_setup(self, effort: str | None = None, model: str | None = None) -> CodexSetup:
        return CodexSetup(model=model or self.cfg.models.codex, effort=effort or self.cfg.models.effort_work,
                          run_dir=self.run_dir, extra_env=self._secret_env(),
                          bypass_sandbox=self.cfg.team.permission_mode == "bypassPermissions")

    def _secret_env(self) -> dict[str, str]:
        env = {k: v for k, v in self.secrets.items() if k != "ANTHROPIC_API_KEY"}
        if "ANTHROPIC_API_KEY" in self.secrets:  # would switch a subscription seat to API billing
            env["CREW_SECRET_ANTHROPIC_API_KEY"] = self.secrets["ANTHROPIC_API_KEY"]
        return env

    def _system_prompt(self, rt: SeatRT) -> str:
        seats = self.store.seats()
        if self.store.get("mode") == "solo":
            builder = self.store.get("solo_builder") or self.lead_name
            if rt.name in (builder, self.store.get("solo_rival")):
                return prompts.solo_system(rt.name, seats)
            if rt.spec.role == "lead":
                return prompts.solo_manager_system(rt.name, seats, builder)
        if rt.spec.role == "lead":
            return prompts.lead_system(rt.name, seats, self.scorecard_text())
        return prompts.member_system(rt.name, seats, self.lead_name)

    def scorecard_text(self) -> str:
        try:
            return scorecard.render(self.cfg.models.codex, self.cfg.models.work)
        except Exception as exc:  # the scorecard informs; it must never stop a run
            self.log(f"scorecard: {exc}")
            return ""

    def head_to_head(self) -> str:
        return self.store.get("head_to_head") or self.cfg.team.head_to_head

    def head_to_head_style(self) -> str:
        """combine: the kept version takes in what the other did better; compete: the better one is kept as it is."""
        return self.store.get("head_to_head_style") or self.cfg.team.head_to_head_style
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

    def start_heartbeat(self) -> None:
        """Note every few seconds, from a thread of its own, that this orchestrator is alive: a slow step in the main
        loop must not make a live project look stopped. The app (and `crew resume`) read the note, so a project
        that is running is never started a second time — two orchestrators on one project would work against
        each other."""
        self._beating = threading.Event()
        self.store.set("alive", {"pid": os.getpid(), "at": now()})

        def beat() -> None:
            while not self._beating.wait(HEARTBEAT):
                try:
                    self.store.set("alive", {"pid": os.getpid(), "at": now()})
                except Exception:  # noqa: BLE001 — a missed note only makes the project look quieter
                    pass

        threading.Thread(target=beat, daemon=True, name="crew-heartbeat").start()

    def stop_heartbeat(self) -> None:
        beating = getattr(self, "_beating", None)
        if beating is not None:
            beating.set()
        try:
            self.store.set("alive", {"pid": os.getpid(), "at": now(), "stopped": True})
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> str:
        self.start_heartbeat()
        try:
            self.prepare()
            if not self.resume:
                self.refine()
                self.kickoff()
            else:
                self.store.set("stop_requested", None)
                self.store.set("final_red", 0)  # the owner may have fixed what failed: fresh attempts
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
            try:
                self.shutdown()
            finally:
                self.stop_heartbeat()
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
        self.store.event("oneoff", seat="refiner", state="start", role="refiner", vendor="claude",
                         account=lead.account.name, model=self.cfg.models.work, effort=self.cfg.models.effort_light)
        res = run_once_claude(
            prompts.refiner_prompt(self.request, self.repo_overview(), lessons.render_for_ceo()), seat="refiner",
            role="member", account=lead.account, workdir=self.main_wt,
            setup=self._claude_setup(effort=self.cfg.models.effort_light), redact=self.redact,
            json_schema=prompts.REFINER_SCHEMA, read_only=True, timeout=600, with_team_tools=False)
        self._account_usage(lead.account.name, res)
        self.store.event("oneoff", seat="refiner", state="error" if res.is_error else "done", tokens=res.tokens,
                         seconds=round(res.duration_s or 0))
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

    def workhorse_seats(self, modes: dict[str, str] | None = None) -> list[SeatRT]:
        """GPT-6 Sol seats that can take work now (not down, not benched, account not at its limit)."""
        modes = modes if modes is not None else {a["name"]: a.get("mode") for a in self.accounts()}
        return [rt for rt in self.seats.values() if rt.spec.vendor == "codex" and not rt.down and not rt.benched
                and modes.get(rt.account.name) != "parked"]

    def kickoff_solo(self) -> None:
        """One builder, others check: single-agent speed with independent review kept. Routine jobs are built by
        the workhorse (GPT-6 Sol) while the lead (Opus 5.5) stands by to decide and to verify; others by the lead."""
        lead = self.seats[self.lead_name]
        tier, effort = self.solo_plan()
        if tier == "workhorse":
            tier, moved = scorecard.route_tier("build", "L", tier, self.cfg.models.codex, self.cfg.models.work)
            if moved:
                self.say(f"The scorecard sends this job to the manager: {moved}.")
        builder = lead
        if tier == "workhorse":
            accounts = {a["name"]: a for a in self.accounts()}
            sol = self.workhorse_seats()
            if sol:
                builder = min(sol, key=lambda r: scheduler._burn(accounts.get(r.account.name, {})))
        self.store.set("solo_builder", builder.name)
        rival = self._solo_rival(builder) if self.head_to_head() in ("some", "all") else None
        if rival is not None:
            self.store.set("solo_rival", rival.name)
        for rt in self.seats.values():
            if rt not in (builder, lead, rival):
                rt.benched = True
                self.store.update_seat(rt.name, status="standby", note="backs up this run")
        brief = self.store.get("brief", {}) or {}
        criteria = "\n".join(f"- {c}" for c in brief.get("acceptance_criteria") or []) or "Meets the brief."
        task_id = self.store.create_task(
            title=brief.get("title") or "The project", spec=self.brief_text(), acceptance=criteria,
            scope=["**"], depends_on=[], size="L", kind="build", suggested_owner=builder.name, created_by="crew",
            tier=tier)
        self.store.update_task(task_id, effort=effort)
        self.set_phase("build")
        self.last_progress = now()
        who = model_label(self.cfg.models.codex if builder.spec.vendor == "codex" else self.cfg.models.work)
        if builder is lead:
            why = ""
            if tier == "workhorse":
                why = (f"It is routine work, but the workhorse ({model_label(self.cfg.models.codex)}) has no ChatGPT "
                       "subscription it can use right now. ")
            self.say(f"{why}This job is small enough that one builder is fastest, so {lead.name} ({who}) builds it at "
                     f"{effort} effort and others check it: a fresh reviewer, then the CEO model.")
        else:
            self.say(f"This job is routine, so the workhorse {builder.name} ({who}) builds it at {effort} effort. "
                     f"{lead.name} ({model_label(self.cfg.models.work)}) answers its questions and verifies the "
                     "result; a fresh reviewer checks it, then the CEO model.")
        if rival is not None:
            twin_id = self._make_twin(self.store.task(task_id))
            self.say(f"Head-to-head: {rival.name} ({self.model_of(rival)}) builds the same job independently. A "
                     "manager compares the two versions without knowing which is which and keeps the better one.")
        task = self.store.task(task_id)
        if self.give_task(builder, task, "normal"):
            first = "\n\n".join([prompts.kickoff_solo(self.brief_text(), self.request, contest=rival is not None),
                                  *builder.pending])
            builder.pending.clear()
            self.start_seat(builder, first)
        if rival is not None and self.give_task(rival, self.store.task(twin_id), "normal"):
            first = "\n\n".join([prompts.kickoff_solo(self.brief_text(), self.request, contest=True), *rival.pending])
            rival.pending.clear()
            self.start_seat(rival, first)
        if builder is not lead:
            self.start_seat(lead, None)  # idle until it is needed: no tokens are used while it waits

    def model_of(self, rt: SeatRT) -> str:
        return model_label(self.cfg.models.codex if rt.spec.vendor == "codex" else self.cfg.models.work)

    def _solo_rival(self, builder: SeatRT) -> SeatRT | None:
        """The other tier's builder for a one-builder head-to-head (never the lead, who verifies the result)."""
        accounts = {a["name"]: a for a in self.accounts()}
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        pool = [rt for rt in self.seats.values() if not rt.down and rt.name != self.lead_name
                and rt.spec.vendor != builder.spec.vendor and modes.get(rt.account.name) != "parked"]
        return min(pool, key=lambda r: scheduler._burn(accounts.get(r.account.name, {}))) if pool else None

    def _make_twin(self, task: dict) -> int:
        """A second, independent copy of a task for the other tier's model (a head-to-head)."""
        other = "manager" if task.get("tier") == "workhorse" else "workhorse"
        twin_id = self.store.create_task(
            title=task["title"], spec=task["spec"], acceptance=task["acceptance"], scope=task["scope"],
            depends_on=task["depends_on"], size=task["size"], kind=task["kind"], suggested_owner=None,
            created_by="crew", tier=other)
        self.store.update_task(twin_id, twin=task["id"], effort=task.get("effort"))
        self.store.update_task(task["id"], twin=twin_id)
        return twin_id

    def setup_contests(self) -> None:
        """When the owner asked for head-to-heads: pick the parts (all eligible ones, or the few where the scorecard
        knows least) and add a twin of each for the other tier's model."""
        if self.head_to_head() not in ("some", "all") or self.store.get("contests_set"):
            return
        self.store.set("contests_set", True)
        managers = [rt for rt in self.seats.values() if rt.spec.vendor == "claude" and not rt.down]
        if not self.workhorse_seats() or not managers:
            self.say("Head-to-head skipped: it needs both a workhorse (ChatGPT) and a manager (Claude) subscription.")
            return
        eligible = [t for t in self.store.tasks(("todo",)) if t["kind"] in CONTEST_KINDS and t["size"] in ("S", "M")
                    and not t.get("twin")]
        if self.head_to_head() == "some":
            known = scorecard.stats()["models"]

            def evidence(t: dict) -> tuple:
                rival = self.cfg.models.work if t.get("tier") == "workhorse" else self.cfg.models.codex
                cell = ((known.get(rival) or {}).get("by_kind") or {}).get(f"{t['kind']} {t['size']}") or {}
                return (cell.get("n", 0), scorecard.SIZE_ORDER.get(t["size"], 9), t["id"])

            eligible = sorted(eligible, key=evidence)[:CONTESTS_SOME]
        made = [t["id"] for t in eligible if self._make_twin(t)]
        if made:
            self.say(f"Head-to-head: {', '.join(f'#{i}' for i in made)} will each be built twice, by "
                     f"{model_label(self.cfg.models.codex)} and {model_label(self.cfg.models.work)}. A manager "
                     "compares the two versions without knowing which is which and keeps the better one.")

    def resume_seats(self) -> None:
        solo = self.store.get("mode") == "solo"
        builder = self.store.get("solo_builder") or self.lead_name
        for rt in self.seats.values():
            if solo and rt.name not in (builder, self.lead_name, self.store.get("solo_rival")):
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

    def interrupt_for_owner(self) -> None:
        for rt in self.seats.values():
            if not self.store.get(f"interrupt:{rt.name}"):
                continue
            asks = self.store.owner_asks(rt.name)
            if not asks and any(m["kind"] == "draft" and m.get("recipient") == rt.name
                                for m in self.store.owner_pending()):
                continue  # the prompt writer is still writing the question up: interrupt once it is ready
            self.store.set(f"interrupt:{rt.name}", None)
            if rt.busy and rt.runner is not None and asks:
                rt.owner_interrupt = now()
                rt.runner.interrupt()
                self.say(f"{rt.name} pauses its work to answer the owner.")

    def tick(self) -> None:
        modes = scheduler.refresh_modes(self.store)
        self.interrupt_for_owner()
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

    def answer_owner(self, rt: SeatRT, text: str, failed: bool = False) -> None:
        """If the owner messaged this seat and it has not answered with team_reply_owner, its words are the answer."""
        asked = int(self.store.get(f"owner_q:{rt.name}") or 0)
        if not asked:
            return
        answered = any(m["sender"] == rt.name and m.get("recipient") == "you"
                       for m in self.store.messages_after(asked, 200))
        self.store.set(f"owner_q:{rt.name}", None)
        if answered:
            return
        reply = clip((text or "").strip(), 3500) if not failed and (text or "").strip() else \
            "I could not answer just now (my last step ended with a problem). Please ask again in a moment."
        self.store.post(rt.name, "direct", self.redact(reply), recipient="you")

    def on_result(self, rt: SeatRT, data: dict) -> None:
        rt.busy = False
        interrupted, rt.owner_interrupt = now() - rt.owner_interrupt < 120, 0.0
        if interrupted:  # stopped at the owner's request: not a failure; the question comes next
            data = {**data, "is_error": False}
            rt.pending.append("Your last step was paused so that you could answer the owner. Once you have "
                              "answered, continue your work exactly where you left off.")
        else:
            self.answer_owner(rt, data.get("text", ""), failed=bool(data.get("is_error") or data.get("limit_hit")))
        tokens, cost = int(data.get("tokens") or 0), float(data.get("cost") or 0)
        self.store.add_seat_usage(rt.name, tokens=tokens, cost=cost, turns=1)
        self.store.add_account_usage(rt.account.name, tokens=tokens, cost=cost)
        row = self.store.seat(rt.name) or {}
        if row.get("current_task"):
            self.store.add_task_usage(row["current_task"], tokens=tokens, cost=cost)
        self.store.update_seat(rt.name, status="idle", note="")
        if interrupted:  # not a turn the seat chose to end: no nudges, and it does not count against the seat
            return
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
            until = self.park(rt.account.name, data.get("text") or "")
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
        accounts = {a["name"]: a for a in self.accounts()}
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
        recent = "\n".join(_fmt_msg(m, 300) for m in self.store.recent_messages(30, public_only=True))
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

    def park(self, account: str, text: str, reported: int | None = None) -> int:
        """A subscription reached its usage limit: park it until the limit really lifts. That is the time reported
        with it (Claude Code's rate report), or, when none is still ahead (ChatGPT reports its limit only in
        words), the time in its message ("try again in 2 hours 5 minutes"), else an hour. Returns that time."""
        until = int(reported or 0) or int((self.store.account(account) or {}).get("parked_until") or 0)
        if until <= now():
            until = int(limit_resets_at(text or "") or now() + 3600)
        until = max(until, int(now() + 60))
        self.store.upsert_account(account, status="rejected", parked_until=until)
        return until

    def handle_parked(self, modes: dict[str, str]) -> None:
        """Seats whose account is parked move to a same-vendor account with headroom, or wait."""
        usable = [a for a in self.accounts() if modes.get(a["name"]) != "parked"]
        if not usable and any(not rt.down for rt in self.seats.values()):
            earliest = min((a.get("parked_until") or 0) for a in self.accounts())
            if now() - self.all_parked_notice > 1800:
                self.all_parked_notice = now()
                self.say(f"Every subscription is at its limit. The team pauses and resumes at {hhmm(earliest)}. "
                         "Nothing is lost.")
            return
        managers = [a for a in self.accounts() if a["vendor"] == "claude"]
        if managers and all(modes.get(a["name"]) == "parked" for a in managers) and \
                now() - self.all_parked_notice > 1800 and self.phase() in ("plan", "build"):
            self.all_parked_notice = now()
            earliest = min((a.get("parked_until") or 0) for a in managers)
            self.say(f"The manager's subscriptions ({model_label(self.cfg.models.work)}) are at their limit until "
                     f"{hhmm(earliest)}. The workhorse carries on with routine work; checks and harder work resume "
                     "then. Nothing is lost.")
        work_waiting = bool(self.store.ready_tasks()) or bool(self.store.tasks(("changes",)))
        for rt in self.seats.values():
            if rt.down or rt.busy or rt.runner is None or modes.get(rt.account.name) != "parked":
                continue
            row = self.store.seat(rt.name) or {}
            if row.get("current_task") or rt.pending or work_waiting:
                self.failover(rt, reason="account at its limit")

    def failover(self, rt: SeatRT, reason: str) -> None:
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        same_vendor = [a for a in self.cfg.accounts_for(rt.spec.vendor)
                       if a.name != rt.account.name and modes.get(a.name) != "parked"]
        session = (self.store.seat(rt.name) or {}).get("session_id")
        if same_vendor:
            accs = {a["name"]: a for a in self.accounts()}
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
        if new:
            self.chat_seen = new[-1]["id"]
            with (self.run_dir / "chat.md").open("a", encoding="utf-8") as fh:
                for m in new:
                    to = f" → {m['recipient']}" if m.get("recipient") else ""
                    fh.write(f"**{hhmm(m['ts'])} {m['sender']}{to}** [{m['kind']}] {self.redact(m['text'])}\n\n")
            for m in new:
                if m["kind"] == "decision" or m["sender"] == "you":
                    self.last_progress = max(self.last_progress, m["ts"])
        if not self.backlog_checked or any(m["sender"] == "you" for m in new):
            self.backlog_checked = True
            self.owner_backlog()

    def owner_backlog(self) -> None:
        """The owner's messages that still need the software: drafts for the prompt writer, and questions for the
        CEO — including any sent while the team was paused."""
        for m in self.store.owner_pending():
            if m["kind"] == "draft":
                if f"draft-{m['id']}" not in self.jobs:
                    self.start_job(f"draft-{m['id']}", self._draft_job, m)
            elif not self.store.get(f"ceo_answered:{m['id']}") and f"owner-ceo-{m['id']}" not in self.jobs:
                self.start_job(f"owner-ceo-{m['id']}", self._owner_ceo_job, m["id"], owner_words(m))

    def _writer_context(self, to: str | None) -> str:
        brief = self.store.get("brief", {}) or {}
        parts = [f"Project: {brief.get('title') or ''} — {clip(brief.get('goal') or self.request, 700)}"]
        plan = self.store.get("plan_summary")
        if plan:
            parts.append("The plan: " + clip(plan, 900))
        if to == "ceo":
            parts.append("Reader: the CEO (reviews the plan and the finished work; does not build).")
        elif to:
            seat = self.store.seat(to) or {}
            task = self.store.task(seat["current_task"]) if seat.get("current_task") else None
            parts.append(f"Reader: {to} alone — a private message ({seat.get('role') or 'member'}, "
                         f"{model_label(seat.get('model') or '')}"
                         + (f", working on task #{task['id']} {task['title']}" if task else "") + ").")
        else:
            parts.append("Reader: the whole team (the lead and every agent).")
        parts.append("The team: " + ", ".join(f"{s['name']} ({s['role']})" for s in self.store.seats()))
        if to:
            talk = self.store.conversation(to, 6)
            if talk:
                parts.append("The owner's private conversation with this reader so far:\n"
                             + "\n".join(_fmt_msg(x, 400) for x in talk))
        recent = self.store.recent_messages(12, public_only=True)
        if recent:
            parts.append("Recent team chat:\n" + "\n".join(_fmt_msg(x, 300) for x in recent))
        return "\n\n".join(parts)

    def _draft_job(self, m: dict) -> None:
        """The prompt writer: the owner's (often dictated) words become a clear message for their reader, and the
        owner's own words travel with it. If the writer cannot run, the owner's words go through unchanged."""
        to, raw, text = m.get("recipient"), m["text"], m["text"]
        name = f"writer-{m['id']}"
        try:
            modes = {a["name"]: a.get("mode") for a in self.accounts()}
            row = scheduler.pick_account([a for a in self.accounts() if a["vendor"] == "claude"], modes)
            if row is not None:
                account = self.cfg.account(row["name"])
                self.store.event("oneoff", seat=name, state="start", role="writer", vendor="claude",
                                 account=account.name, model=self.cfg.models.work, effort="low")
                res = run_once_claude(prompts.writer_prompt(raw, self._writer_context(to)), seat=name, role="member",
                                      account=account, workdir=self.main_wt, setup=self._claude_setup(effort="low"),
                                      redact=self.redact, json_schema=prompts.WRITER_SCHEMA, read_only=True,
                                      timeout=240, with_team_tools=False)
                self._account_usage(account.name, res)
                self.store.event("oneoff", seat=name, state="error" if res.is_error else "done", tokens=res.tokens,
                                 seconds=round(res.duration_s or 0))
                out = res.structured if isinstance(res.structured, dict) else {}
                better = str(out.get("message") or "").strip()
                if not res.is_error and better and len(better) <= 4 * len(raw) + 400:
                    text = better
        finally:
            self.store.post("you", "direct" if to else "human", text, urgent=True, recipient=to, ref=m["id"],
                            original=raw)
            self.store.mark_drafted(m["id"])

    def _wants_wake(self, rt: SeatRT, unread: list[dict]) -> bool:
        """Wake an idle agent only when a message needs it: tokens are spent on work, not on reading chatter."""
        is_lead = rt.name == self.lead_name
        for m in unread:
            if _mentions(m["text"], rt.name):
                return True
            if is_lead and (m["sender"] == "you" or m["kind"] in ("question", "blocker", "concern")):
                return True
        return False

    def _owner_directs(self, unread: list[dict], seat: str) -> tuple[list[dict], list[dict]]:
        """Split what a seat has not read into the owner's unanswered direct messages to it and the team chat
        (direct messages it has already answered are neither)."""
        direct = self.store.owner_asks(seat, unread)
        return direct, [m for m in unread if not (m["sender"] == "you" and m.get("recipient") == seat)]

    def deliver_pending(self) -> None:
        """Send idle seats their instructions plus the chat they missed (only when there is a reason)."""
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        for rt in self.seats.values():
            if rt.runner is None and not rt.down and rt.cooldown_until <= now() and \
                    modes.get(rt.account.name) != "parked" and self.phase() not in ("done", "stopped", "failed"):
                direct, _ = self._owner_directs(self.store.unread(rt.name), rt.name)
                if direct:  # a standing-by agent is started just to answer the owner
                    session = (self.store.seat(rt.name) or {}).get("session_id")
                    self.start_seat(rt, None, resume_session=session)
            if rt.down or rt.busy or rt.runner is None or not rt.runner.alive():
                continue
            if isinstance(rt.runner, CodexSeat) and rt.runner.busy:
                continue
            unread = self.store.unread(rt.name)
            direct, unread_team = self._owner_directs(unread, rt.name)
            if not rt.pending and not direct and not (unread_team and self._wants_wake(rt, unread_team)):
                continue
            parts = []
            if direct:
                self.store.set(f"owner_q:{rt.name}", direct[-1]["id"])
                parts.append(prompts.owner_direct(direct))
            parts += list(rt.pending)
            rt.pending.clear()
            if unread_team:
                parts.append(prompts.chat_digest(unread_team))
            if unread:
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
        """Note every change of a task's status; only a step forward counts as progress (a new task, or a task reaching
        a stage it had not reached before). A task that goes round review → changes → review — say its checks can
        never pass — would otherwise look busy for ever: the stall ladder (the lead replans, the CEO rules, then an
        honest stop) would never start, and the team would spend the owner's usage on it without end."""
        for t in self.store.tasks():
            old = self.task_snapshot.get(t["id"])
            if old != t["status"]:
                self.task_snapshot[t["id"]] = t["status"]
                if old is not None:
                    self.store.event("task_status", task_id=t["id"], frm=old, to=t["status"])
                stage = STAGES.get(t["status"], -1)
                if stage > self.task_stage.get(t["id"], -1):
                    self.task_stage[t["id"]] = stage
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
        managers = [a for a in self.accounts() if a["vendor"] == "claude"]
        if managers and all(scheduler.mode_of(a) == "parked" for a in managers):
            self.last_progress = now()  # waiting for the managers' usage to come back is a pause, not a stall
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
            f"#{t['id']} [{t['status']}] {t['title']} size {t['size']} kind {t['kind']} tier {t.get('tier') or '-'}"
            f" effort {t.get('effort') or '-'}"
            f" owner→{t['suggested_owner'] or '-'}"
            f" deps {t['depends_on']} scope {t['scope']}\n    spec: {clip(t['spec'], 400)}\n    accept: {clip(t['acceptance'], 300)}"
            for t in self.store.tasks())

    def _ceo_plan_job(self) -> None:
        prompt = prompts.ceo_plan_prompt(self.brief_text(), self.store.get("plan_summary", ""), self._board_text(),
                                         lessons.render_for_ceo(), self.scorecard_text())
        self.store.set("verdict:plan", None)
        res = self.run_ceo("ceo-plan", prompt, json_schema=prompts.CEO_PLAN_SCHEMA,
                           accept=lambda r: bool(self.store.get("verdict:plan")) or self._valid_verdict(r))
        if not self.store.get("verdict:plan"):
            self.apply_ceo_verdict("plan", "ceo-plan", res)
        if not self.store.get("verdict:plan"):
            self.log(f"CEO plan review gave no verdict ({clip(res.text, 200)}); proceeding")
            self.plan_reviewed = True

    @staticmethod
    def _valid_verdict(res: RunResult) -> bool:
        data = res.structured if isinstance(res.structured, dict) else {}
        return not res.is_error and data.get("verdict") in ("approve", "changes")

    def apply_ceo_verdict(self, kind: str, seat: str, res: RunResult) -> bool:
        """Record the CEO's structured answer exactly as its team_verdict tool would (same checks, same chat post)."""
        if not self._valid_verdict(res):
            return False
        data = res.structured
        notes = str(data.get("notes") or "").strip()
        if data["verdict"] == "changes" and len(notes) < 20:
            notes = (notes + " (the CEO gave no further detail)").strip()
        args = {"kind": kind, "verdict": data["verdict"], "notes": notes}
        if kind == "plan":
            args["efforts"] = [t for t in data.get("tasks") or [] if isinstance(t, dict)]
        try:
            team_tools.verdict(team_tools.Ctx(store=self.store, seat=seat, role="ceo"), args)
        except team_tools.ToolError as exc:
            self.log(f"CEO verdict not recorded: {exc}")
            return False
        return True

    def start_build(self) -> None:
        if self.phase() != "plan":
            return
        self.set_phase("build")
        self.last_progress = now()
        self.say("Plan approved. Build started.", urgent=True)
        self.setup_contests()

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
        accounts = {a["name"]: a for a in self.accounts()}
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
        workhorse_usable = bool(self.workhorse_seats(modes))
        may_help = self.manager_may_help(ready) if workhorse_usable else set()
        for row in scheduler.order_idle_seats(idle_rows, accounts):
            rt = self.seats[row["name"]]
            acc = accounts.get(rt.account.name, {})
            task = scheduler.choose_task(row, ready, acc, modes.get(rt.account.name, "normal"), cost_model,
                                         row.get("model") or "", idle_names, self.grace_until,
                                         workhorse_usable=workhorse_usable, may_help=may_help)
            if task is None:
                continue
            if self.give_task(rt, task, modes.get(rt.account.name, "normal")):
                ready = [t for t in ready if t["id"] != task["id"]]
                ready = [t for t in ready if not self.store.lease_conflicts(t)]
                idle_names.discard(rt.name)

    def manager_may_help(self, ready: list[dict]) -> set[int]:
        """Workhorse tasks an idle manager seat may take: waiting 10+ minutes for a workhorse seat while the
        managers are below their share of the tokens (the owner's target is 60-70%)."""
        t = now()
        for task in ready:
            self.ready_since.setdefault(task["id"], t)
        waiting = {task["id"] for task in ready
                   if task.get("tier") == "workhorse" and t - self.ready_since[task["id"]] >= 600}
        if not waiting:
            return set()
        share = tiers.share_of(self.store, "manager")
        return waiting if share is not None and share < tiers.TARGETS["manager"][0] else set()

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
        rt.pending.append(prompts.assignment(task, branch, mode, resumed=resumed, contest=bool(task.get("twin"))))
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

    def _snapshot(self, task: dict) -> bool:
        """Commit the author's work and fix exactly what is reviewed (once; a resumed run reuses it).
        False while the author is still finishing its turn."""
        if self.store.get(f"review_sha:{task['id']}"):
            return True
        owner = self.seats.get(task["owner"] or "")
        if owner is not None and owner.busy and now() - (task["submitted_at"] or now()) < 180:
            return False
        if owner is not None:
            gitops.commit_all(owner.worktree, f"task #{task['id']}: {clip(task['summary'] or task['title'], 70)}")
            self.store.update_seat(owner.name, current_task=None)
            sha = gitops.head(owner.worktree)
        else:
            sha = gitops.out(self.repo, "rev-parse", task["branch"])
        self.store.set(f"review_sha:{task['id']}", sha)
        return True

    def dispatch_reviews(self) -> None:
        for task in self.store.tasks(("review",)):
            key = f"review-{task['id']}"
            if key in self.jobs or float(self.store.get(f"review_wait:{task['id']}") or 0) > now():
                continue
            twin = self.store.task(task["twin"]) if task.get("twin") else None
            if twin is not None and twin["status"] not in ("cancelled", "merged") and self._dispatch_contest(task, twin):
                continue
            if not self._snapshot(task):
                continue
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
            # Crew's own scan of what the change adds: a leaked secret goes straight back; risky lines go to the
            # reviewer, who judges each in context.
            scan = quality.scan(wt, self.integration, self.redact.values())
            if scan.findings:
                self.store.event("scan", task_id=task_id, secrets=len(scan.blocking), risky=len(scan.risky))
            if scan.blocking:
                return ("changes", quality.blocking_text(scan, self.integration), "scan")
            notes = quality.review_notes(scan)
            # The manager (Opus 5.5) checks every piece of work, on another account than its author's.
            failures, rounds = 0, 0
            while failures < 2 and rounds < 2 + len(self.cfg.accounts):
                rounds += 1
                self.store.set(f"review:{task_id}", None)
                res = self.run_reviewer(task, result.summary if result.ran else "", notes)
                verdict = self.store.get(f"review:{task_id}")
                if verdict:
                    return (verdict["verdict"], verdict["notes"], verdict["by"])
                self.log(f"reviewer for #{task_id} gave no verdict: {clip(res.text, 300)}")
                if res.limit_hit:  # a usage limit is not a verdict: try another manager subscription, or wait
                    if not self.manager_usable():
                        return ("wait", clip(res.text, 300), None)
                    continue
                failures += 1
            if result.ran and result.ok:
                return ("approve", "Reviewer unavailable twice; approved on passing checks.", None)
            return ("changes", "No reviewer could review this task and no checks are set. The lead should set checks.", None)
        finally:
            gitops.remove_worktree(self.repo, wt)

    def manager_usable(self) -> bool:
        return any(scheduler.mode_of(a) != "parked" for a in self.accounts() if a["vendor"] == "claude")

    def run_reviewer(self, task: dict, check_log: str, scan_notes: str = "") -> RunResult:
        """A fresh manager (Opus 5.5) checks the task, on another subscription than its author's when possible."""
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        owner_acc = self.seats[task["owner"]].account.name if task["owner"] in self.seats else None
        managers = [a for a in self.accounts() if a["vendor"] == "claude"]
        acc_row = scheduler.pick_account(managers, modes, avoid=owner_acc)
        if acc_row is None:
            return RunResult(is_error=True, limit_hit=True,
                             text="Every manager subscription is at its usage limit right now.")
        account = self.cfg.account(acc_row["name"])
        author = self.seats.get(task["owner"] or "")
        author_tier = seat_tier(author.spec.vendor) if author else (task.get("tier") or "manager")
        prompt = prompts.reviewer_prompt(task, self.integration, self.store.get("checks", []) or [], check_log,
                                         author_tier=author_tier, scan_notes=scan_notes)
        wt = self.run_dir / "worktrees" / f"_review-{task['id']}"
        name = f"reviewer-{task['id']}"
        model = self.cfg.models.work if account.vendor == "claude" else (self.cfg.models.codex or "")
        effort = self.review_effort(task)
        self.say(f"Reviewing task #{task['id']} with fresh eyes ({model_label(model)}, {account.name}).",
                 task_id=task["id"])
        self.store.event("oneoff", seat=name, task_id=task["id"], state="start", role="reviewer", vendor=account.vendor,
                         account=account.name, effort=effort, model=model)
        if account.vendor == "codex":
            res = run_once_codex(prompt, seat=name, role="reviewer", account=account, workdir=wt,
                                 setup=self._codex_setup(effort=effort), redact=self.redact,
                                 task_id=task["id"], read_only=True)
        else:
            res = run_once_claude(prompt, seat=name, role="reviewer", account=account, workdir=wt,
                                  setup=self._claude_setup(effort=effort), redact=self.redact,
                                  task_id=task["id"], read_only=True)
        self._account_usage(account.name, res)
        if res.limit_hit:
            self.park(account.name, res.text, res.resets_at)
        self.store.event("oneoff", seat=name, task_id=task["id"], state="error" if res.is_error else "done",
                         tokens=res.tokens, seconds=round(res.duration_s or 0))
        return res

    # ------------------------------------------------------------ head-to-head

    def _dispatch_contest(self, task: dict, twin: dict) -> bool:
        """A head-to-head task is in review. True when handled here (judging started, or waiting for the rival);
        False when the rival is too late and this version is checked on its own."""
        orig = task if task["id"] < twin["id"] else twin
        key = f"contest-{orig['id']}"
        if key in self.jobs or float(self.store.get(f"review_wait:{orig['id']}") or 0) > now():
            return True
        if twin["status"] == "review":
            if self._snapshot(task) and self._snapshot(twin):
                self.start_job(key, self._contest_job, orig["id"])
            return True
        if now() - (task["submitted_at"] or now()) < CONTEST_WAIT:
            return True  # the other version is still being built
        self._end_contest(survivor=task, dropped=twin, why="the other version was not ready in time.")
        return True  # the survivor is checked on its own at the next tick

    def _withdraw(self, task: dict, why: str) -> None:
        """Stop a seat that is still building a version that has been withdrawn."""
        rt = self.seats.get(task["owner"] or "")
        row = self.store.seat(rt.name) if rt else None
        if rt is None or not row or row.get("current_task") != task["id"]:
            return
        if rt.busy and rt.runner is not None:
            rt.runner.interrupt()
        gitops.park_worktree(rt.worktree, rt.name)
        self.store.update_seat(rt.name, current_task=None)
        rt.pending.append(f"Stop working on task #{task['id']}: {why} Your work so far is kept on its branch. "
                          "Wait for your next assignment.")

    def _keep_version(self, winner: dict, loser: dict) -> dict:
        """Keep one version under the original task's number (everything that depends on it keeps working) and
        withdraw the other. Returns the surviving task."""
        orig, twin = (winner, loser) if winner["id"] < loser["id"] else (loser, winner)
        if winner["id"] != orig["id"]:
            self.store.update_task(orig["id"], branch=winner["branch"], owner=winner["owner"], tier=winner["tier"],
                                   status=winner["status"], summary=winner["summary"], evidence=winner["evidence"],
                                   started_at=winner["started_at"], submitted_at=winner["submitted_at"],
                                   tokens=winner["tokens"], effort=winner.get("effort") or orig.get("effort"),
                                   attempts=winner.get("attempts") or 1)
            self.store.set(f"review_sha:{orig['id']}", self.store.get(f"review_sha:{winner['id']}"))
        self.store.update_task(orig["id"], twin=None)
        self.store.update_task(twin["id"], twin=None, status="cancelled", finished_at=now())
        self.store.set(f"review_sha:{twin['id']}", None)
        return self.store.task(orig["id"])

    def _end_contest(self, survivor: dict, dropped: dict, why: str) -> None:
        """A head-to-head that cannot be judged: keep the finished version and withdraw the other."""
        self._withdraw(dropped, "the head-to-head ended without your version.")
        kept = self._keep_version(survivor, dropped)
        self.store.append_note(kept["id"], "crew", f"Head-to-head ended: {why}")
        self.say(f"Head-to-head on task #{kept['id']} ended: {why} The finished version is checked on its own.",
                 task_id=kept["id"])

    def _contest_job(self, orig_id: int) -> tuple[str, object]:
        orig = self.store.task(orig_id)
        twin = self.store.task(orig["twin"]) if orig and orig.get("twin") else None
        if not orig or not twin:
            return ("fallback", "the head-to-head was already over")
        versions = [orig, twin]
        random.shuffle(versions)  # the judge never learns which model built which version
        labels = {"a": versions[0]["id"], "b": versions[1]["id"]}
        root = self.run_dir / "worktrees" / f"_contest-{orig_id}"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        folders = {}
        try:
            for letter, t in zip("ab", versions):
                folders[letter] = root / letter
                gitops.git(self.repo, "worktree", "add", "-f", "--detach", str(folders[letter]),
                           self.store.get(f"review_sha:{t['id']}") or t["branch"])
            checks = self.store.get("checks", []) or []
            results = {letter: gitops.run_checks(folders[letter], checks,
                                                 self.run_dir / "logs" / f"checks-contest-{orig_id}-{letter}.log",
                                                 self.cfg.team.checks_timeout_minutes * 60) for letter in "ab"}
            prompt = prompts.contest_prompt(orig, self.integration, checks,
                                            {k: r.summary if r.ran else "" for k, r in results.items()})
            res = self.run_judge(orig, twin, prompt, root)
            data = res.structured if isinstance(res.structured, dict) else {}
            valid = data.get("winner") in ("a", "b") and all(
                isinstance(data.get(k), dict) and data[k].get("verdict") in ("approve", "changes") for k in "ab")
            if not valid:
                return ("wait", clip(res.text, 300)) if res.limit_hit else ("fallback", clip(res.text, 300))
            for letter, r in results.items():  # failing checks are a defect whatever the judge thought
                if r.ran and not r.ok:
                    data[letter] = {"verdict": "changes",
                                    "notes": "The checks fail on this version:\n" + r.summary + "\n\n" + data[letter]["notes"]}
                scan = quality.scan(folders[letter], self.integration, self.redact.values())
                if scan.blocking:  # so is a leaked secret
                    data[letter] = {"verdict": "changes", "notes": quality.blocking_text(scan, self.integration)
                                    + "\n\n" + data[letter]["notes"]}
            return ("judged", {"labels": labels, "a": data["a"], "b": data["b"], "winner": data["winner"],
                               "reason": str(data.get("reason") or "").strip(), "by": f"judge-{orig_id}",
                               "borrow": str(data.get("borrow") or "").strip()})
        finally:
            for folder in folders.values():
                gitops.remove_worktree(self.repo, folder)
            shutil.rmtree(root, ignore_errors=True)

    def run_judge(self, orig: dict, twin: dict, prompt: str, workdir: Path) -> RunResult:
        """A fresh manager (Opus 5.5) judges the two versions blind, on another subscription than their authors'."""
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        authors = {self.seats[t["owner"]].account.name for t in (orig, twin) if t.get("owner") in self.seats}
        managers = [a for a in self.accounts() if a["vendor"] == "claude"]
        avoid = next((a for a in authors if any(m["name"] == a for m in managers)), None)
        acc_row = scheduler.pick_account(managers, modes, avoid=avoid)
        if acc_row is None:
            return RunResult(is_error=True, limit_hit=True, text="Every manager subscription is at its usage limit.")
        account = self.cfg.account(acc_row["name"])
        name = f"judge-{orig['id']}"
        effort = self.review_effort(orig)
        self.say(f"Judging the head-to-head on task #{orig['id']}: {model_label(self.cfg.models.work)} compares both "
                 "versions without knowing which model built which.", task_id=orig["id"])
        self.store.event("oneoff", seat=name, task_id=orig["id"], state="start", role="reviewer", vendor="claude",
                         account=account.name, effort=effort, model=self.cfg.models.work)
        res = run_once_claude(prompt, seat=name, role="reviewer", account=account, workdir=workdir,
                              setup=self._claude_setup(effort=effort), redact=self.redact, task_id=orig["id"],
                              read_only=True, json_schema=prompts.CONTEST_SCHEMA, timeout=2400)
        self._account_usage(account.name, res)
        self.store.event("oneoff", seat=name, task_id=orig["id"], state="error" if res.is_error else "done",
                         tokens=res.tokens, seconds=round(res.duration_s or 0))
        return res

    def on_contest_done(self, orig_id: int, outcome: tuple[str, object]) -> None:
        kind, detail = outcome
        orig = self.store.task(orig_id)
        twin = self.store.task(orig["twin"]) if orig and orig.get("twin") else None
        if orig is None or twin is None:
            return
        if kind == "wait":
            resets = [int(a.get("parked_until") or 0) for a in self.accounts() if a["vendor"] == "claude"]
            self.store.set(f"review_wait:{orig_id}", min(resets) if resets and min(resets) > now() else int(now() + 600))
            return
        if kind != "judged":
            self._end_contest(survivor=orig, dropped=twin, why="the judge could not compare the two versions.")
            return
        by_id = {detail["labels"]["a"]: detail["a"], detail["labels"]["b"]: detail["b"]}
        passed = {tid: v["verdict"] == "approve" for tid, v in by_id.items()}
        winner_id = detail["labels"][detail["winner"]]
        if passed[orig["id"]] != passed[twin["id"]]:
            winner_id = orig["id"] if passed[orig["id"]] else twin["id"]  # a version that passed beats one that did not
        winner, loser = (orig, twin) if winner_id == orig["id"] else (twin, orig)
        wseat, lseat = self.store.seat(winner["owner"] or "") or {}, self.store.seat(loser["owner"] or "") or {}
        wmodel = wseat.get("model") or self.cfg.models.work
        lmodel = lseat.get("model") or self.cfg.models.work
        project = self.store.get("project_name", "") or ""
        for t, won in ((winner, True), (loser, False)):
            self._score(t, first_pass=passed[t["id"]], outcome="won" if won else "lost", rounds=1)
        lessons.record_contest(project, orig["title"], orig["kind"], orig["size"], wmodel, lmodel,
                               seat_tier(wseat.get("vendor") or "claude"), seat_tier(lseat.get("vendor") or "claude"),
                               passed[winner["id"]], passed[loser["id"]], detail.get("reason", ""))
        self.store.set(f"scored:{orig_id}", True)
        self.store.event("contest", task_id=orig_id, winner=wmodel, loser=lmodel, winner_passed=passed[winner["id"]],
                         loser_passed=passed[loser["id"]], reason=detail.get("reason", ""))
        verdict = by_id[winner["id"]]
        self.store.event("review", task_id=orig_id, verdict=verdict["verdict"], by=detail.get("by"), rounds=1)
        state = lambda ok: "passed" if ok else "did not pass"  # noqa: E731
        self.say(f"Head-to-head on task #{orig_id} ({orig['title']}): {model_label(wmodel)}'s version "
                 f"{state(passed[winner['id']])} and {model_label(lmodel)}'s {state(passed[loser['id']])}. Kept "
                 f"{model_label(wmodel)}'s" + (f": {clip(detail['reason'], 300)}" if detail.get("reason") else "."),
                 task_id=orig_id)
        kept = self._keep_version(winner, loser)
        borrow = str(detail.get("borrow") or "").strip()
        combine = bool(borrow) and self.head_to_head_style() == "combine"
        joint = (f"Combine the best of both versions: the other version (branch {loser['branch']}, by "
                 f"{model_label(lmodel)}) does these things better. Fold them into yours, keep everything else, run "
                 f"the checks and resubmit:\n{borrow}\n(See the other version with: git diff "
                 f"{self.integration}...{loser['branch']})") if combine else ""
        if combine:
            self.store.append_note(kept["id"], "crew", f"Joint submission: {model_label(wmodel)}'s version, with "
                                                       f"what {model_label(lmodel)}'s did better folded in.")
            self.store.set(f"joint:{kept['id']}", {"base": wmodel, "with": lmodel})
        if passed[winner["id"]] and not combine:
            self.store.update_task(kept["id"], status="approved", review_notes=verdict["notes"], review_rounds=1)
        elif passed[winner["id"]]:  # collaborative: one more short round, then the usual review
            self.store.set(f"review_sha:{kept['id']}", None)
            self.store.update_task(kept["id"], status="changes", review_notes=joint, review_rounds=0)
            self.say(f"Task #{kept['id']}: combining the best of both — {model_label(wmodel)}'s version takes in what "
                     f"{model_label(lmodel)}'s did better: {clip(borrow, 300)}", task_id=kept["id"])
        else:
            self.store.set(f"review_sha:{kept['id']}", None)
            notes = verdict["notes"] + (f"\n\n{joint}" if joint else "")
            self.store.update_task(kept["id"], status="changes", review_notes=notes, review_rounds=1)
            self.say(f"Task #{kept['id']} needs changes before it is merged: {clip(verdict['notes'], 400)}",
                     task_id=kept["id"])

    # ------------------------------------------------------------ scorecard

    def _score(self, task: dict, first_pass: bool | None = None, outcome: str = "merged",
               rounds: int | None = None) -> None:
        """One scorecard record for the model that built this task."""
        builder = self.store.seat(task["owner"] or "") or {}
        start = task.get("started_at") or now()
        end = task.get("submitted_at") or now() if outcome != "merged" else now()
        try:
            lessons.record_effort_outcome(
                task["kind"], task["size"], self.task_effort(task),
                task["review_rounds"] if rounds is None else rounds, max(0.0, (end - start) / 60), task["tokens"],
                project=self.store.get("project_name", "") or "", tier=seat_tier(builder.get("vendor") or "claude"),
                model=builder.get("model") or self.cfg.models.work, outcome=outcome, first_pass=first_pass,
                handovers=max(0, int(task.get("attempts") or 1) - 1))
        except Exception as exc:  # the record must never break the run
            self.log(f"scorecard record: {exc}")

    def promote_task(self, task: dict, rounds: int, notes: str) -> None:
        """The workhorse's work failed its check twice: the task moves up to the manager, who continues from that
        work and the reviewer's notes. The workhorse's attempt is scored as not passing."""
        builder = self.store.seat(task["owner"] or "") or {}
        self._score(task, first_pass=False, outcome="moved-up", rounds=rounds)
        effort = self.task_effort(task)
        self.store.set(f"review_sha:{task['id']}", None)
        self.task_stage.pop(task["id"], None)  # a new attempt by the manager: its steps forward count again
        self.store.update_task(task["id"], status="todo", owner=None, suggested_owner=None, tier="manager",
                               review_rounds=0, tokens=0, started_at=None, review_notes=notes,
                               effort="high" if effort in ("low", "medium", "auto") else effort)
        self.store.append_note(task["id"], "crew", f"Moved up to the manager after {rounds} checks the workhorse's "
                               "work did not pass. Continue from the work on this branch and the latest review.")
        self.store.event("moved_up", task_id=task["id"], frm=builder.get("model") or "", to=self.cfg.models.work,
                         rounds=rounds)
        self.say(f"Task #{task['id']} moves up to {model_label(self.cfg.models.work)}: "
                 f"{model_label(builder.get('model') or '')}'s work did not pass the check {rounds} times. It "
                 "continues from that work and the reviewer's notes.", task_id=task["id"])

    def on_review_done(self, task_id: int, outcome: tuple[str, str, str | None]) -> None:
        verdict, notes, by = outcome
        task = self.store.task(task_id)
        if task is None or task["status"] != "review":
            return
        if verdict == "wait":
            resets = [int(a.get("parked_until") or 0) for a in self.accounts() if a["vendor"] == "claude"]
            until = min(resets) if resets and min(resets) > now() else int(now() + 600)
            self.store.set(f"review_wait:{task_id}", until)
            if not self.store.get(f"review_wait_said:{task_id}"):
                self.store.set(f"review_wait_said:{task_id}", True)
                self.say(f"Task #{task_id} is finished and waits for its check: the manager's subscriptions are at "
                         f"their usage limit until about {hhmm(until)}. Nothing is lost.", task_id=task_id)
            return
        self.store.set(f"review_wait:{task_id}", None)
        rounds = task["review_rounds"] + 1
        self.store.event("review", task_id=task_id, verdict=verdict, by=by, rounds=rounds)
        if verdict != "approve":
            self.store.set(f"review_sha:{task_id}", None)
        if verdict == "approve":
            self.store.update_task(task_id, status="approved", review_notes=notes, review_rounds=rounds)
            self.say(f"Task #{task_id} approved{f' by {by}' if by else ''}.", task_id=task_id)
            return
        builder = self.store.seat(task["owner"] or "") or {}
        if rounds >= 2 and task.get("tier") == "workhorse" and builder.get("vendor") == "codex":
            self.promote_task(task, rounds, notes)
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
            builder = self.store.seat(task["owner"] or "") or {}
            model = builder.get("model") or self.cfg.models.work
            self.store.event("merged", task_id=task_id, tokens=task["tokens"], size=task["size"],
                             seconds=now() - (task["started_at"] or now()), rounds=task["review_rounds"],
                             model=model, tier=seat_tier(builder.get("vendor") or "claude"))
            if not self.store.get(f"scored:{task_id}"):  # head-to-head tasks were scored when judged
                self._score(task)
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
                builder = self.store.get("solo_builder") or lead.name
                if builder != lead.name:
                    lead.benched = False
                    lead.pending.append(
                        f"Every task is merged: {builder}'s work passed its review. Your worktree now shows the merged "
                        "result. As the manager, verify the whole result against the brief yourself (run it, test it, "
                        "look at it). Fix any small gap directly here and commit; then call team_project_done with the "
                        "plain-language report for the owner (what was built, how to use it, what was verified, "
                        "limits).")
                    return
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
        scan = quality.scan(self.main_wt, self.store.get("base_commit"), self.redact.values())
        self.store.set("final_scan", {"secrets": len(scan.blocking), "risky": len(scan.risky), "lines": scan.lines})
        if scan.blocking and self.final_rounds < 2:
            self.final_rounds += 1
            return "changes:" + quality.final_blocking_text(scan)
        if self.cfg.team.ceo_reviews and self.final_rounds < 1:
            self.final_rounds += 1
            self.store.set("verdict:final", None)
            prompt = prompts.ceo_final_prompt(self.brief_text(), self.store.get("done_report", ""), checks,
                                              self.store.get("base_commit"), scan=quality.summary(scan))
            res = self.run_ceo("ceo-final", prompt, workdir=self.main_wt, json_schema=prompts.CEO_FINAL_SCHEMA,
                               accept=lambda r: bool(self.store.get("verdict:final")) or self._valid_verdict(r))
            if not self.store.get("verdict:final"):
                self.apply_ceo_verdict("final", "ceo-final", res)
            verdict = self.store.get("verdict:final") or {}
            if verdict.get("verdict") == "changes":
                return "changes:" + verdict.get("notes", "")
        return "ok"

    def on_final_done(self, outcome: str) -> None:
        if outcome.startswith("red:") or outcome.startswith("changes:"):
            kind, _, detail = outcome.partition(":")
            what = "The final checks fail" if kind == "red" else "The CEO's final review requires changes"
            if kind == "red":  # checks that can never pass would send the lead back for ever
                rounds = int(self.store.get("final_red", 0) or 0) + 1
                self.store.set("final_red", rounds)
                if rounds >= 3:
                    self.say(f"The final checks still fail after {rounds - 1} rounds of fixes:\n{clip(detail, 1500)}\n"
                             "Stopping with an honest report. Everything done so far is saved; `crew resume` "
                             "continues from here.", urgent=True)
                    self.set_phase("stopped")
                    return
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
        try:
            self.retrospective()
        except Exception:  # what the run taught is saved when it can be; it never turns a delivery into a failure
            self.log("retrospective: " + traceback.format_exc())
        self.set_phase("done")

    # ================================================================== CEO

    def solo_plan(self) -> tuple[str, str]:
        """Who builds a one-builder job and how hard it thinks. The manager (Opus 5.5) decided both while writing
        the brief, so the CEO is kept for checking; the owner's fixed effort, if set, wins."""
        brief = self.store.get("brief", {}) or {}
        tier = brief.get("builder_tier") if brief.get("builder_tier") in TIERS else "manager"
        effort = brief.get("builder_effort")
        if effort not in lessons.EFFORT_ORDER:
            effort = "medium" if tier == "workhorse" else "high"
        if self.cfg.models.effort_work != "auto":
            effort = self.cfg.models.effort_work
        return tier, effort

    def ceo_chain(self) -> list[str]:
        """The CEO model, then its backup, then the manager model: the first that can run does the job."""
        chain: list[str] = []
        for model in (self.cfg.models.ceo, self.cfg.models.ceo_backup, self.cfg.models.work):
            if model and model not in chain:
                chain.append(model)
        return chain

    def run_ceo(self, name: str, prompt: str, workdir: Path | None = None, json_schema: dict | None = None,
                timeout: float = 2400, accept=None, effort: str | None = None) -> RunResult:
        """A CEO call (GPT-6 Astra by default) at the CEO's effort. If that model cannot run — no ChatGPT
        subscription, a usage limit, an error, or no usable answer — the backup (Fable 5.1) and then the
        manager (Opus 5.5) take over at the same effort."""
        modes = {a["name"]: a.get("mode") for a in self.accounts()}
        effort = effort or (self.cfg.models.effort_ceo if self.cfg.models.effort_ceo != "auto" else "max")
        res = RunResult(is_error=True, text="no subscription available for the CEO")
        for model in self.ceo_chain():
            vendor = vendor_of(model)
            acc_row = scheduler.pick_account([a for a in self.accounts() if a["vendor"] == vendor], modes)
            if acc_row is None:
                self.log(f"CEO: no usable {vendor} subscription for {model}; trying the next model")
                continue
            account = self.cfg.account(acc_row["name"])
            run_effort = effort if vendor == "codex" else ("max" if effort == "ultra" else effort)
            self.store.event("oneoff", seat=name, state="start", role="ceo", vendor=vendor, account=account.name,
                             model=model, effort=run_effort)
            if vendor == "codex":
                res = run_once_codex(prompt, seat=name, role="ceo", account=account, workdir=workdir or self.main_wt,
                                     setup=self._codex_setup(effort=run_effort, model=model), redact=self.redact,
                                     read_only=True, timeout=timeout, json_schema=json_schema)
            else:
                res = run_once_claude(prompt, seat=name, role="ceo", account=account, workdir=workdir or self.main_wt,
                                      setup=self._claude_setup(effort=run_effort, model=model), redact=self.redact,
                                      read_only=True, timeout=timeout, json_schema=json_schema)
            self._account_usage(account.name, res)
            usable = not res.is_error and (accept is None or accept(res))
            self.store.event("oneoff", seat=name, state="done" if usable else "error", tokens=res.tokens,
                             seconds=round(res.duration_s or 0))
            if usable:
                return res
            self.log(f"CEO on {model} gave no usable answer ({clip(res.text, 200)}); trying the next model")
        return res

    def process_escalations(self) -> None:
        for ev in self.store.events("escalation"):
            if ev["id"] in self.processed_escalations or f"ruling-{ev['id']}" in self.jobs:
                continue
            self.processed_escalations.add(ev["id"])
            context = self._board_text() + "\n\nRecent chat:\n" + "\n".join(
                _fmt_msg(m, 300) for m in self.store.recent_messages(25, public_only=True))
            prompt = prompts.ceo_ruling_prompt(ev["data"].get("question", ""), context)
            self.start_job(f"ruling-{ev['id']}", self._ruling_job, ev["id"], prompt)

    def _owner_ceo_job(self, msg_id: int, question: str) -> None:
        self.store.set(f"ceo_answered:{msg_id}", True)
        context = (self.brief_text() + "\n\nBoard:\n" + self._board_text() + "\n\nRecent team chat:\n" + "\n".join(
            _fmt_msg(m, 300) for m in self.store.recent_messages(25, public_only=True)))
        # A question is not a review: a lighter effort answers it well and keeps the CEO near its token share.
        res = self.run_ceo(f"ceo-question-{msg_id}", prompts.ceo_owner_prompt(question, context), timeout=1800,
                           effort="high")
        text = (res.text or "").strip()
        if res.is_error or not text:
            text = "I could not answer just now (no CEO model was available). Please ask again later."
        self.store.post("ceo", "direct", self.redact(clip(text, 4000)), recipient="you")

    def _ruling_job(self, event_id: int, prompt: str) -> RunResult:
        name = f"ceo-ruling-{event_id}"
        before = self.store.last_message_id()

        def ruled(res: RunResult) -> bool:
            data = res.structured if isinstance(res.structured, dict) else {}
            decided = any(m["kind"] == "decision" and m["sender"] == name for m in self.store.messages_after(before))
            return decided or bool(str(data.get("decision") or "").strip())

        res = self.run_ceo(name, prompt, json_schema=prompts.CEO_RULING_SCHEMA, accept=ruled)
        decided = any(m["kind"] == "decision" and m["sender"] == name for m in self.store.messages_after(before))
        data = res.structured if isinstance(res.structured, dict) else {}
        if not decided and str(data.get("decision") or "").strip():
            text = str(data["decision"]).strip()
            if str(data.get("reason") or "").strip():
                text += "\nReason: " + str(data["reason"]).strip()
            team_tools.decide(team_tools.Ctx(store=self.store, seat=name, role="ceo"), {"text": "CEO ruling: " + text})
        return res

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
                elif key.startswith("contest-"):
                    self.on_contest_done(int(key.split("-")[1]), ("fallback", str(result)))
                elif key == "final":
                    self.on_final_done(f"red:{result}")
                continue
            if key.startswith("review-"):
                self.on_review_done(int(key.split("-")[1]), result)
            elif key.startswith("contest-"):
                self.on_contest_done(int(key.split("-")[1]), result)
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
            builder = self.store.seat(self.store.get("solo_builder") or self.lead_name) or {}
            lines.append(f"- Time: {elapsed}. One builder ({model_label(builder.get('model') or '')}) did the work; a "
                         "reviewer with fresh eyes and the CEO model checked it.")
        else:
            active = sum(1 for rt in self.seats.values() if not rt.benched)
            lines.append(f"- Time: {elapsed}, with {active} agents working in parallel." if active > 1 else
                         f"- Time: {elapsed}, with one agent building and others checking.")
        pieces = len(merged)
        lines.append(f"- {pieces} {'piece' if pieces == 1 else 'pieces'} of work built, each checked by a reviewer "
                     f"who had not written it ({first_pass} approved at the first review).")
        if failovers:
            lines.append(f"- Usage limits were handled {len(failovers)} time(s) by moving work to another subscription.")
        contests = self.store.events("contest")
        if contests:
            wins: dict[str, int] = {}
            for e in contests:
                wins[model_label(e["data"].get("winner") or "")] = wins.get(model_label(e["data"].get("winner") or ""), 0) + 1
            lines.append(f"- Head-to-head: {len(contests)} {'part was' if len(contests) == 1 else 'parts were'} built by "
                         "two models and judged blind; " + ", ".join(f"{m} won {n}" for m, n in wins.items()) + ".")
        moved = self.store.events("moved_up")
        if moved:
            lines.append(f"- {len(moved)} {'part' if len(moved) == 1 else 'parts'} moved up from the workhorse to the "
                         "manager after two unsuccessful checks.")
        share = tiers.shares(self.store)
        if share["total"]:
            lines += ["", "## How the work was shared (tokens)"]
            names = {"workhorse": model_label(self.cfg.models.codex), "manager": model_label(self.cfg.models.work),
                     "ceo": model_label(self.cfg.models.ceo)}
            for t in share["tiers"]:
                low, high = t["target"]
                target = f"about {high}%" if not low else f"{low}-{high}%"
                lines.append(f"- {t['name']} ({names[t['tier']]}): {t['pct']:g}% of {share['total']:,} tokens "
                             f"(target {target})")
        lines += ["", "## Subscriptions used"]
        for acc in self.store.accounts():
            util = "" if acc["util_5h"] is None else f", {acc['util_5h'] * 100:.0f}% of the 5-hour allowance"
            lines.append(f"- {acc['name']}: {acc['tokens']:,} tokens{util}")
        path = self.run_dir / "REPORT.md"
        atomic_write(path, "\n".join(lines) + "\n")
        return path

    def retrospective(self) -> None:
        """Save what this run taught us: cost model, failovers, stalls, the CEO's effort lessons, and the lead's own."""
        try:
            update_cost_model(self.store, self.cfg)
        except Exception as exc:  # the memory may be locked or damaged: the other lessons are still saved
            self.log(f"cost model: {exc}")
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
    """What past projects taught about cost: tokens per size unit (per model) and usage per token (per account).
    Empty when there is nothing yet, or when the memory cannot be read right now (it is locked, or damaged):
    the scheduler then plans without it rather than stopping the project."""
    import json
    import sqlite3

    from .lessons import _db

    try:
        db = _db()
        try:
            row = db.execute("SELECT value FROM memo WHERE key='cost_model'").fetchone()
        finally:
            db.close()
        value = json.loads(row["value"]) if row else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}

    def numbers(table) -> dict[str, float]:
        if not isinstance(table, dict):
            return {}
        return {str(k): float(v) for k, v in table.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}

    if not isinstance(value, dict):
        return {}
    return {"tokens_per_unit": numbers(value.get("tokens_per_unit")), "util_per_token": numbers(value.get("util_per_token"))}


def update_cost_model(store: Store, cfg: Config) -> None:
    """Learn tokens per task-size unit (per model) and utilization per token (per account)."""
    import json

    from .lessons import _db

    merged = [e for e in store.events("merged") if e["data"].get("tokens")]
    per_unit: dict[str, list[float]] = {}  # each model has its own pace (GPT-6 Sol and Opus 5.5 differ)
    for e in merged:
        per_unit.setdefault(e["data"].get("model") or cfg.models.work, []).append(
            e["data"]["tokens"] / scheduler.SIZE_UNITS.get(e["data"].get("size", "M"), 3))
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
    for model, values in per_unit.items():
        new = sum(values) / len(values)
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
