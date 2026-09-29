# Crew — architecture

Crew runs several AI coding agents (Claude Code and Codex, each on its own
subscription) as one team on one git repository. The team talks in a single
group chat that humans can read, splits the work, reviews each other, and
delivers one result. The goal: the quality of one top model working alone,
delivered 3–4× faster, with the usage spread across every subscription.

This document is the design. `README.md` is the user guide.

---

## 1. What the research says (and what we do about it)

| Finding | Source | Design response |
|---|---|---|
| 41–87% of multi-agent runs fail in production, mostly from **coordination** defects, not model weakness | Coordination-as-a-layer (arXiv 2605.03310); MAST (arXiv 2503.13657): 41.8% spec/design, 36.9% inter-agent misalignment, 21.3% verification | Coordination is **code, not conversation**. A deterministic orchestrator owns the task board, leases, merges, reviews, timers and failover. Agents decide *content*; the orchestrator decides *process*. |
| Parallel writers make conflicting implicit decisions; systems that work keep **writes single-threaded** per scope and add agents as reviewers/advisers; "map-reduce-and-manage" | Cognition, *Don't Build Multi-Agents* (2025) and *Multi-Agents: What's Actually Working* (2026) | Every task declares a **file scope**; the store refuses overlapping leases. The lead first lands a **foundation task** (skeleton + interface contracts) so parallel work starts from shared decisions. |
| A **clean-context reviewer** catches ~2 bugs per PR, 58% severe | Cognition (Devin Review) | Every task is reviewed by a **fresh session** that never saw the author's conversation — preferably another vendor/model. Nobody merges their own work. |
| Debate ≈ majority voting; debate alone does not improve expected correctness; agents **conform** more than they correct | *Debate or Vote* (NeurIPS 2025); controlled MAD studies | No open-ended debate. One bounded round of concerns on the plan, then a binding **DECISION** by the lead. Technical disputes are settled by **evidence** (a test, a benchmark) or one ruling from the "CEO" model — never by more chat. Chat budgets are enforced. |
| Agents stall, loop, can't tell time, and stop before the job is done | MAST FM-1.3 step repetition, FM-1.5 unaware of termination, FM-3.1 premature termination; Anthropic C-compiler ("time blindness") | Heartbeat **watchdog** per seat, **progress ledger** with a stall counter that forces a replan (Magentic-One), task time budgets, repetition detection, and an explicit termination rule. No agent ever waits on another agent. |
| High-quality tests are the real manager; logs must not pollute context | Anthropic, *Building a C compiler with parallel Claudes* (2026) | Checks run by the orchestrator, output stored in files, agents see short summaries. Tasks carry acceptance criteria and must submit evidence. |
| Long-running agents need a "shift handover": progress notes, one feature at a time, clean commits | Anthropic, *Effective harnesses for long-running agents* (2025) | Task notes are the handover. Any seat (or another vendor) can resume a task from its notes, branch and diff. |
| Agents overwrite each other without leases; humans need to see the traffic | MCP Agent Mail (advisory file leases, human-visible archive) | Leases are enforced, not advisory. The group chat is the human-visible record. No private messages exist. |

---

## 2. Moving parts

```
                 ┌───────────── You (browser / terminal) ─────────────┐
                 │  group chat · task board · usage · final report    │
                 └───────────────────────┬────────────────────────────┘
                                         │
      ┌──────────────────────── Orchestrator (Python) ──────────────────────────┐
      │ phases · scheduler · watchdog · progress ledger · reviews · merges ·     │
      │ failover · lessons · secrets redaction                                    │
      └──────┬──────────────┬──────────────┬──────────────┬──────────────────────┘
             │              │              │              │
       seat: lead      seat: member   seat: member   seat: member      one-shot runs:
       Claude Code     Claude Code    Claude Code    Codex             refiner · reviewer ·
       (account A)     (account B)    (account C)    (ChatGPT)         CEO (Fable 5.1)
             │              │              │              │
             └──── team tools (MCP) ── one SQLite store ──┘
                                         │
                     one git repo: integration branch + a worktree per seat
```

**Seat vs account.** A *seat* is a worker (identity, worktree, conversation).
An *account* is fuel (a Claude or ChatGPT subscription login). Seats normally
run one-to-one on accounts, but a seat can move to another account of the same
vendor mid-task, taking its full conversation with it. That is how the project
keeps going when one subscription runs dry.

**Team tools (MCP).** Every agent gets the same small tool set, served by
`crewlib/mcp_server.py` over stdio and backed by one SQLite file (WAL mode):

| Tool | Who | Purpose |
|---|---|---|
| `team_chat_post` / `team_chat_read` | all | the one group chat (no private messages exist) |
| `team_tasks`, `team_status` | all | the board, seats, usage modes |
| `team_task_create`, `team_set_checks`, `team_plan_ready`, `team_decide`, `team_project_done` | lead | plan, test commands, binding decisions, completion |
| `team_task_note`, `team_task_submit`, `team_task_block`, `team_task_release` | task owner | progress/handover notes, hand-in with evidence |
| `team_review_submit` | reviewer run | approve / request changes |
| `team_escalate` | all | ask the CEO model for one binding ruling |
| `team_lesson_add`, `team_lessons` | all | the shared experience memory |

Every tool answer carries a one-line digest of unread chat, and a Claude Code
`PostToolUse` hook injects **urgent** messages (from you or the lead) straight
into a working agent's context, so nobody has to poll.

---

## 3. The run, step by step

**Solo or team, decided per job.** The refiner also estimates the job's size and
how many parts could be built at once. Small or hard-to-split jobs run in **solo
mode**: one top agent writes everything and the others only check it (a
clean-context reviewer, then the CEO model). That keeps writes single-threaded,
which is what works best today, and gives single-agent speed with independent
review. Only jobs with several independent parts run as a **full team**, because
coordination has fixed costs (planning, reviews, integration) that only parallel
work pays back. In our measurements on a small job, one agent alone took 9
minutes where the full team took 34. `team.mode` can force either.

1. **Refine.** A one-shot refiner turns your request (often voice-typed and
   rambling) into a brief: goal, deliverables, acceptance criteria,
   constraints, assumptions. Posted to the chat.
2. **Plan.** The lead reads the repo and brief, lands a foundation task
   (skeleton, interfaces, test scaffold), and creates the task graph: each task
   with spec, acceptance criteria, file scope, size, dependencies, and a
   suggested owner. Members orient themselves (read-only) and may post **one**
   concern each. The lead decides. Optional: the CEO model reviews the plan once.
3. **Build.** The orchestrator hands ready tasks to idle seats (lead's
   suggestion first, then the usage-aware scheduler). Each seat works on its own
   task branch in its own worktree, notes progress, and submits with evidence.
4. **Check → review → merge.** On submit the orchestrator commits, runs the
   checks, and launches a clean-context reviewer. Approved work is merged into
   the integration branch and the checks run again; red merges are reverted
   automatically and the task reopens with the log. Conflicts go back to the
   owner. After two rejected rounds the lead decides.
5. **Deliver.** When the board is empty the lead verifies the brief's
   acceptance criteria, the CEO model does a final review, and the lead writes a
   plain-language report. The integration branch is merged into your branch
   (or left as a branch / pushed, per settings). Lessons are saved.

---

## 4. Guarantees against the four classic failures

**Freezing.** Headless sessions with pre-approved tools (no permission
prompts); no agent waits on another; watchdog (no activity for
`stall_minutes` → nudge → restart from the saved session → reassign);
progress ledger (no board movement for `ledger_minutes` → lead must replan);
repetition detector (same command failing in a loop → nudge); task time
budgets; hard caps on wall clock and spend; explicit termination.

**Slop.** Top models only; acceptance criteria per task; evidence required
at submit; orchestrator-run checks; clean-context cross-review; the lead's
final verification; the CEO's final review. No self-merges.

**Arguing.** Chat budgets per seat (blockers always allowed); one round of
concerns; binding decisions; disputes go to tests or a single CEO ruling;
agents are told that agreement is not evidence.

**Corruption.** Worktree isolation; only the orchestrator writes the
integration branch; checks after every merge with automatic revert; all state
in SQLite transactions so `crew resume` continues after a crash; secrets are
redacted from every transcript.

---

## 5. Usage and time management

Claude Code emits a `rate_limit_event` (utilization and reset time for the
5-hour and weekly windows) as it works; Codex records the same in its session
files and reports `usage_limit_reached` errors. From these the scheduler puts
each account in one of four modes:

| Mode | When | What it gets |
|---|---|---|
| **spend** | plenty left and the window resets soon, or burning slower than the clock | the heaviest tasks — unused quota is lost at reset |
| **normal** | on pace | any task |
| **conserve** | little left, long until reset (or weekly nearly used) | small tasks and slow-but-cheap work: running suites, verification, docs |
| **parked** | limit reached | nothing; its seats move to another account until reset |

Task size (S/M/L) is priced from past runs (learned tokens per task, per
model). A large task is never started on an account unlikely to finish it.
New work goes to the account with the lowest burn relative to its clock, so
all subscriptions drain at a similar pace.

**Failover.** Claude → Claude: the session file is copied into the other
account's profile and resumed under the same id — same conversation, nothing
lost. Claude ↔ Codex: the task moves with its handover notes, branch and diff.
Either way the chat records it and the project does not stop.

---

## 6. Models (quality first)

Only the models on the allow-list run. Defaults: **Claude Opus 5.5** does the
work (lead, members, reviews, refiner, and every sub-agent, forced via
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE`); **Claude Fable 5.1** is the "CEO":
plan review, rulings, final review — high-leverage and rare, because it has
its own tighter limit. Since 2.3.1 the team's routine work goes to the
workhorse, **Claude Sonnet 5.5** (Claude seats with `tier = "workhorse"`; their
sub-agents run Sonnet too), and GPT-6 Astra is the CEO on ChatGPT with Fable 5.1
as its backup. Haiku and GPT-6 Sol are banned; Claude Code's background
"small fast model" is redirected to the seat's model (`ANTHROPIC_DEFAULT_HAIKU_MODEL`).
Cost is saved by effort level, caching and less chatter — never by a weaker
model.

---

## 7. Experience memory

`~/.crew/lessons.jsonl` holds lessons from every run: agents add them
(`team_lesson_add`), the orchestrator writes them from telemetry (task cost by
size and model, stalls, failovers, review findings), and the lead writes a
retrospective at the end. Duplicates reinforce instead of repeating. The
strongest lessons are injected into every agent's instructions at start, and
`~/.crew/PLAYBOOK.md` is the human-readable digest.

---

## 8. Files

```
crew/
  crew, crew.cmd              launchers
  crew.toml.example           settings: accounts, seats, models, limits
  secrets.env.example         your API keys (the real file is never committed)
  team_rules.md               shared instructions every agent follows (editable)
  crewlib/                    the engine (Python 3.11+, standard library only)
  tests/                      unit, fault-injection and end-to-end tests
```

---

## 9. How it is tested

**Unit tests** (`tests/test_units.py`): file-lease overlap rules, task dependencies
and atomic claiming, chat budgets and one-concern-per-plan, role authority (only the
lead plans, only the owner submits, only the assigned reviewer reviews), the MCP
protocol over real stdio, usage modes from real rate-limit events, settings and
the model ban list, lesson reinforcement, secret redaction, worktree merges,
conflict detection, automatic revert, and carrying early work onto a task branch.

**Fault-injection runs** (`tests/test_e2e.py`): the real orchestrator drives
protocol-faithful fake agents (`tests/fakes/`) that speak Claude Code's
stream-json and Codex's exec JSONL, launch the team tools like the real CLIs,
and keep resumable session files. Scenarios: a three-vendor team (Claude +
Codex) with cross-vendor reviews; rejected reviews, planning concerns and a
CEO-required plan revision; a usage-limit hit mid-task with failover to another
account that keeps the conversation; a frozen agent and a crashed agent; merge
conflicts; and stop-then-resume.

**Real runs** with Claude Code (Opus 5.5 workers, Fable 5.1 CEO) on a small
project confirmed the integration end to end and surfaced issues the fakes could
not: check commands written for bash, a lead starting work before its formal
assignment, and parallel agents sharing one system Python. Each became a fix and
a lesson.

Bugs these tests caught before release: build artifacts from check runs
blocking merges (an endless bounce loop); a stale "exited" event from a replaced
process cascading into restarts; a resumed run idling because the stopped phase
and in-flight reviews were not restored; transient Codex reconnect notices being
treated as failures; git refusing branch names nested under another branch.
