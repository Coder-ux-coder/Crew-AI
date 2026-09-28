# Crew — the full debugging campaign

My working brief for a complete, repeated bug hunt through every part of Crew. It is deliberately
exhaustive. It says what "working" means for every part of the product, how each part is likely to break,
which scenarios must be run, how every fix is proved, and how the work itself is protected from memory
loss over a long campaign. It is updated as the campaign runs; the campaign state and the bug ledger are
the source of truth for what has been done.

(This file lives at the root of the repository, outside the `crew` folder, so Crew's updater and installer
never copy it onto the owner's computer.)

---------------------------------------------------------------------------------------------------------------

## 0. Campaign state (updated after every step — read this first after any break or context summary)

- Status: RUNNING — third continuation (Claude, cloud container, 2026-09-28). All work and execution in
  the cloud (the owner's requirement), on branch `claude/exciting-heisenberg-6w6gge`.
- Round: 1, dynamic sweep. Baseline rerun here, before any change: full suite 157 tests OK, 0 skipped
  (Python 3.11, Playwright + Chromium present); ruff (CI config) 2 findings, both triaged noise; eslint
  0 problems on 17 files; `node --check` clean.
- Found first: the second continuation's cloud run (36431557271) failed. Its E2E team tests looped until
  their time limit. Cause: on Python 3.12 (the version the owner's installer sets up, and the cloud's)
  `unittest` exits 5 when it finds no tests, so the fake project's first task could never pass its
  check, and nothing ever stopped a task that keeps failing its checks (C14, C15; ledger). Also the
  cloud lint config missed the two triaged findings (fixed in the config).
- Done in this continuation: C14, C15 fixed with tests that fail on the old code; full suite under
  Python 3.12: 160 tests OK. API fuzzer written (`.github/campaign/fuzz_api.py`), not yet run.
- Next: run the fuzzer, fix what it finds (tests first); prove the P1 tests against pre-fix code
  (worktree of `ba081b8` ready in the scratchpad); browser sweep; C12; pipe ResourceWarnings; rounds 2–11.
- Release remains unapproved: do not modify `crew/VERSION.json`, merge into `Crew-AI`, or release.
  Version 2.3.1 and the installed-updater rehearsal require the owner's explicit approval.

Rules for keeping this state: after each fix, add a ledger line (section 12) and update "Done" and "Next".
After each round, write a one-paragraph round summary (section 11). Never rely on memory alone: if it is
not written here, it has not been done.

---------------------------------------------------------------------------------------------------------------

## 1. Mission and standard

The owner says Crew is "really buggy" and asks for everything to be checked — every single thing: models,
routing, updating, every screen, every feature, front end and back end, data, scenarios — and for bugs to
be hunted and fixed over and over again, with nothing lost in the long term.

The standard is not "the tests pass". The standard is:

- every screen opens, every control does what its label promises, and every failure is explained in plain
  words — nothing fails silently, nothing hangs, nothing shows a raw error or a traceback;
- every feature works with the owner's real setup: three Claude subscriptions whose names contain dots and
  e-mail-like text (`claude-1`, `ceo-pbit.gop.pk`, `zeeshandmg36-gmail.com`), one ChatGPT subscription
  (`mohidzeeshanrana-gmail.com`), Windows 10/11, a 1280×650 laptop view at 150% scaling, a Samsung phone on
  the same Wi-Fi, a slow or proxied office network, and voice dictation in English and Urdu;
- nothing is ever lost: chats, projects, private conversations, sign-ins, API keys, settings, workflows,
  lessons, the scorecard, captures — across updates, crashes, restarts, subscription switches and months
  of use; long chats and long projects keep their context;
- a complete round of checking finds nothing new that matters.

Codebase at the start: ~13,100 lines of Python (crewlib = the team engine; crewapp = the app) and ~6,200
lines of JavaScript/CSS/HTML (crewapp/static).

---------------------------------------------------------------------------------------------------------------

## 2. Ground rules (non-negotiable)

1. The owner's data survives. No fix deletes, resets or reinterprets existing data. Data-format changes
   are additive; old formats are still read. A damaged file is set aside with a dated name, never silently
   overwritten, and the owner is told what happened and what was kept.
2. Root cause first. Every fix addresses the cause, is as small as it can be, and does not change behaviour
   the owner relies on without a stated reason.
3. Proof. Every reproducible bug gets a regression test that fails before the fix and passes after it. The
   full suite stays green after every round; Python and JavaScript lint stay clean.
4. Secrets are never printed, logged, echoed back or committed. The redactor covers every place text
   leaves a process (chat logs, run logs, reports, errors, the team chat, notices).
5. Windows is the owner's platform. Every Windows-only path is read line by line and guarded so it cannot
   break other systems. Known traps: `os.kill(pid, 0)` terminates on Windows; an open file cannot be
   replaced or deleted; `open()` defaults to cp1252; `creationflags` exists only on Windows; PowerShell
   quoting (single quotes doubled); paths with spaces, apostrophes and Urdu; `pythonw.exe` has no console
   (stdout/stderr None); a port is held for minutes after a program closes; `SO_EXCLUSIVEADDRUSE`;
   line endings; `taskkill /T` kills children; long paths; antivirus locking files briefly.
6. Releases go to branch `Crew-AI` of Coder-ux-coder/crew-ai. Version bumped (2.3.x) with plain notes.
   Before a release is called done, the owner's installed updater is rehearsed against GitHub
   (download → install → restart → reconnect).
7. Commits carry the required trailers and no model identifiers elsewhere.
8. The report to the owner is honest: what was verified by running it, what could only be read (Windows),
   what is still open.

---------------------------------------------------------------------------------------------------------------

## 3. Method — every round has five steps

1. **Static sweep** — machine checks over all code (section 4); triage every finding (real bug, latent risk
   worth closing, or noise); fix real bugs and worthwhile risks.
2. **Dynamic sweep** — run the real app with the scripted fakes and drive it:
   - a browser harness: every screen at 1280×650 and 390×844, light and dark; every control clicked, every
     field filled, every menu opened; records console errors, uncaught exceptions, unhandled promise
     rejections, failed requests (4xx/5xx), horizontal overflow, off-screen elements, clipped/overlapping
     text; screenshots of each state for visual review;
   - an API fuzzer from the server's route table: every route with missing, empty, wrong-type, huge, Urdu,
     path-like and hostile values; flags any 500, traceback in the log, hang (> 30 s), or change to data
     that should not change;
   - the command line with normal and odd arguments;
   - the scenarios in section 8, each run to its end and checked against its expected outcome.
3. **Line-by-line review** of the round's files against section 7's checklists and section 6's bug classes.
4. **Fix → test → ledger.**
5. **Repeat the checks**; then the next round, until a round is clean.

| Round | Focus |
|---|---|
| 1 | Tooling; static sweep of everything; whole-app browser sweep at both sizes and themes |
| 2 | API fuzz; the app server route by route; settings and data files; security |
| 3 | Models and effort everywhere; routing (tiers, subscriptions, limits, failover) |
| 4 | Updating: Crew's updater, automatic updates, restart and reconnect, migrations, Claude Code updates |
| 5 | Chats (Claude and ChatGPT): sessions, streaming, attachments, plan mode, limits, failover, memory |
| 6 | Team engine: orchestrator state machine, scheduler, agents, tools, store, gitops, hook, prompts, quality |
| 7 | Workflows, skills, connections, library/captures, usage/scorecard, browser/phone/computer |
| 8 | Front end line by line: ui, app, chat, projects, settings, workflows, live, voice, the rest |
| 9 | Memory and long-term continuity (section 9); data robustness; scenarios end to end |
| 10 | Windows launch, install, update and restart paths by reading; installer scripts |
| 11+ | Repeat the full sweep until a round is clean |

---------------------------------------------------------------------------------------------------------------

## 4. Tooling

- **Python**: pyflakes; ruff (F, E9, B, PLE, PLW, RUF minus typography, S602/S604/S605/S307/S301/S506/S108);
  `python -W error -m compileall`; custom scans:
  - every text `open()`, `read_text()`, `write_text()` names an encoding;
  - every `creationflags=` is on a Windows-only path;
  - every `subprocess.run` has a timeout, or is long-lived by design and drained;
  - no `os.kill(pid, 0)` reachable on Windows;
  - every `json.loads` of a file or a request is guarded;
  - every `int()`/`float()` of external input is guarded;
  - every `[0]`, `max()`, `min()` on a list that can be empty is guarded;
  - every thread that runs a loop catches and logs exceptions;
  - every SQLite connection is used from one thread or protected by a lock.
- **JavaScript**: eslint's bug rules (no-undef, no-unused-vars, no-unreachable, no-dupe-keys,
  no-dupe-else-if, no-duplicate-case, no-fallthrough, no-self-assign, no-self-compare, no-cond-assign,
  no-constant-condition, getter-return, no-unsafe-finally, no-unsafe-optional-chaining, no-unsafe-negation,
  valid-typeof, use-isnan, no-async-promise-executor, no-loss-of-precision, no-sparse-arrays,
  no-import-assign, no-const-assign, no-func-assign, no-global-assign) with browser globals and ES
  modules; `node --check`; custom scans:
  - every `icon('name')` exists in index.html's sprite;
  - every `$('#id')` / `getElementById` exists;
  - every `api('/api/...')` path and method exists in the server's route table;
  - every `setInterval`/`setTimeout`/stream opened by a page is cleared when the page is left;
  - every `api()` call has a `catch` or is inside a `try`.
- **CSS**: every `var(--x)` is defined in light and dark.
- **Browser harness**: Playwright on /opt/pw-browsers/chromium.
- **API fuzzer**: generated from `ROUTES`.

---------------------------------------------------------------------------------------------------------------

## 5. The owner's setup — the reference configuration for every test

- Subscriptions: Claude `claude-1`, `ceo-pbit.gop.pk`, `zeeshandmg36-gmail.com`; ChatGPT
  `mohidzeeshanrana-gmail.com`. Names used as folder names, TOML values, command arguments, HTML text,
  JSON keys, log lines and regex inputs — all must work.
- Tiers: GPT-6 Sol workhorse, Opus 5.5 manager, GPT-6 Astra CEO, Fable 5.1 CEO backup. Banned: Haiku,
  Sonnet, Luna, Terra.
- Windows 10/11, Edge; laptop 1920×1080 at 150% (1280×650 CSS); Samsung phone ≈ 390×844; office proxy
  possible; dictation in English and Urdu; Crew started from the desktop icon and with Windows.

---------------------------------------------------------------------------------------------------------------

## 6. Bug classes to hunt while reading code

**Back end**
- Unhandled exceptions → 500s: missing keys, None, JSON with the wrong type (string for a number, list for
  an object), `int()` of text, empty sequences, division by zero.
- Threads: SQLite across threads; dicts/lists mutated while iterated; check-then-act races; the SSE hub;
  background threads that die silently; timers that never stop; locks held across slow calls.
- Processes: zombies and orphans; pipes never drained (deadlock when a buffer fills); handles left open;
  kill semantics; children surviving their parent on Windows; process groups.
- Files: encodings; CRLF; locked files; non-atomic writes (a crash leaves half a file); names with spaces,
  apostrophes, Urdu, very long names; path traversal in anything that serves or writes files.
- Subprocess safety: `shell=True`; commands built as strings from owner text; PowerShell and cmd quoting.
- Time: local vs UTC; DST; month ends; float vs int timestamps; clocks shown in the owner's time zone.
- Settings: TOML escaping (quotes, backslashes, newlines, unicode); migrations run twice; unknown keys; a
  hand-edited or corrupted file.
- State machines: orchestrator phases; task statuses and file leases; twins; review rounds; merges and
  conflicts; resume after stop or crash; failover; drafts and private messages; CEO jobs; stalls.
- Security: loopback vs network; pairing; CSRF (X-Crew header, Origin); /internal routes; file routes;
  uploads; redaction.

**Front end**
- Undefined names; null dereferences; promises without catch; `JSON.parse` of an HTML error page.
- Lifecycles: intervals, timeouts, streams and listeners not removed on leaving a page (leaks, duplicate
  handlers, requests for pages no longer shown); polling racing the owner's actions; double submission;
  stale state after navigating back; state lost on reload that should persist.
- Layout: overflow at both sizes; clipped or overlapping text; menus off-screen; dark-mode contrast;
  focus and keyboard (Enter, Shift+Enter, Escape, Tab); long names; Urdu (right-to-left) text.
- Feedback: every action shows success or a plain error; buttons disabled while working; nothing silent.

---------------------------------------------------------------------------------------------------------------

## 7. Feature-by-feature and file-by-file checklists

### 7.1 Models and effort (settings.py, config.py, tiers.py, agents.py, chat.py, prompts.py, UI pickers)
Must hold:
- The catalogue (KNOWN_MODELS) and labels: GPT-6 Sol, GPT-6 Astra, Opus 5.5, Fable 5.1, Opus 5. A ChatGPT
  model is never labelled Claude, never offered in a Claude picker, and vice versa; custom names display
  as themselves.
- Banned names refused everywhere: chat pickers, typed names, Settings, crew.toml edited by hand,
  workflows, tiers, CEO backup, Claude Code's sub-agents (work_model forcing), one-off runs (refiner,
  reviewer, judge, writer, CEO answers).
- Allowed list honoured; an empty or odd allowed list never locks the owner out.
- Effort names exactly the vendors': Claude auto/low/medium/high/xhigh/max; GPT-6
  auto/low/medium/high/xhigh/max/ultra (no minimal); older OpenAI models mapped to their nearest level;
  "ultra" never reaches Claude; "auto" means the vendor's default.
- Effort per chat, per project task (CEO), for small jobs (manager), the owner's fixed choice, the CEO's
  own, prompt writer (low), CEO answers (high), reviewers/judges, lessons; changes mid-run (Claude resumes
  the same conversation at the new effort; Codex on the next turn); the agents panel shows the effort in
  force.
- The model shown is the model that ran: chat header, recents, usage, scorecard, agents panel, task
  lines, report.
- A model needing a newer Claude Code: explained, Claude Code updated, retried.
Try: every model × every effort in a chat; banned names in crew.toml; `effort_ceo = "ultra"` with the
Claude backup; only-Claude and only-ChatGPT setups (the lead must be Claude — clear message).

### 7.2 Routing (scheduler.py, orchestrator.py, tiers.py, scorecard.py, tools.py, chat.py, workflows.py)
Tiers: workhorse takes routine specified tasks; manager takes judgement work and reviews every workhorse
task; CEO reviews the plan and result, answers the owner, rules on escalations. Check `tier_allows`,
`choose_task`, `manager_may_help`, twins, `route_tier`, move-up after two failed reviews, the lead's and
CEO's tier tools, the CEO verdict applying tiers and efforts, the CEO chain Astra → Fable → Opus with the
accept predicate, solo jobs (builder tier/effort, lead as manager, solo rival), team/solo in "auto", token
shares against targets.
Subscriptions: `pick_account` by headroom; conserve/parked; the reviewer on another subscription than the
author; the judge avoiding both authors; the chat's chosen subscription with fallback; a workflow's
subscription; a project restricted to ticked subscriptions (the lead still needs Claude); usage limits
(rate events, 5-hour and weekly windows, resets in local time, parked_until, waking seats); failover
(Claude session copied, Codex from notes, chat mid-conversation, a review at a limit waits, a usage pause
is not a stall).
Try: 1–3 Claude + 0/1 ChatGPT; limits on each role; head-to-head some/all, compete/combine; one-subscription
projects; the owner's exact names.

### 7.3 Updating (updater.py, server.py update routes, app.js, sw.js, offline.html, launcher.py, settings.py
migrations, prompts.team_rules, claude_cli.py)
- check(): forwarding from the old name/branch, cache-busting, unreachable GitHub or proxy, malformed JSON,
  lower/equal versions, `available` computed against the running version.
- install(): zip layout, manifest, removing only program files a newer version dropped, never touching
  ~/.crew, requirements changes (pip; continue if pip fails), a file that cannot be replaced (Windows) —
  stop cleanly, never half a program; disk full; a download cut in half (zip error handled).
- restart: helper waits for the old Crew; new Crew waits for its own port; window reconnects (same or other
  port); offline page reconnects; service worker refreshed; "Crew updated to X" shown once.
- automatic updates: only when nothing runs and the owner has been away 30 minutes; switch in Settings;
  never during a project; never twice at once.
- updating while a project runs: running projects keep the code they started with; lazy imports inside
  functions must not mix versions — make safe or warn.
- migrations: once only, exact old defaults only, never lose owner keys, survive hand edits; team rules
  refreshed only when unedited.
- Claude Code: newest copy found among several; daily update; update on demand; failure explained;
  versions shown.

### 7.4 Chats (chat.py, server chat routes, pages/chat.js, ui.js markdown, voice.js)
- New chat: product, model, effort, plan mode, subscription, timer/head-to-head (team), attachments,
  dictation, spoken conversation, ✦ prompt writer and its automatic mode, slash commands, skills.
- Streaming: text, thinking, tool steps, to-do list, helpers, files made, counters, context ring,
  /compact, /context, /usage.
- Stop, retry, edit and resend, approve/reject a plan.
- Limits and failover mid-answer; all subscriptions at their limit → plain message with reset time.
- Chats list: rename, delete (also while answering), pin, recents order, reopening old chats after an
  update.
- Markdown: tables, code, links (safe), images, long text, Urdu (RTL) — and no HTML injection.
- ChatGPT chats: model/effort passed as Codex expects; resume; errors explained.

### 7.5 Team projects (orchestrator.py, runs.py, server run routes, pages/projects.js, cli.py, web.py)
- Start from composer, CLI, workflow; brief; plan + CEO review; build; reviews; merges with checks;
  final checks and CEO final review; delivery (merge/branch/push); lessons; report.
- Project screen: phases, stats, estimates, shares, agents and helpers, plan, subscriptions, report,
  preview, open folder, stop/continue, timer, private conversations (To picker, CEO, helpers, Ask now,
  drafts, own words, unread answers).
- Resume after stop, app restart, computer restart; moved or deleted project folders; projects from older
  Crews; a damaged project never breaks the list.

### 7.6 Workflows (workflows.py, pages/workflows.js)
- Templates, create/edit/delete, run now, schedules, history, product/subscription, results and notices;
  due while closed (documented behaviour, never many runs); time zones; month ends; edits while running.

### 7.7 Library, skills, connections, usage, scorecard, panels
- Library: captures, drawing, asking Claude, files from chats, open/download/delete, big and oddly named
  files.
- Skills: Anthropic's installed per Claude subscription; built-in; Crew's and the owner's; enable/disable;
  pack rebuilt.
- Connections: keys add/replace/delete/masked/name rules/never logged; MCP servers (web or program), import
  from the Claude desktop app, handed to chats and teams.
- Usage: 5-hour and weekly use with resets, tokens per day, Claude Code version; scorecard tiles, table,
  rules, head-to-head, empty states.
- Browser (Playwright/Edge), phone (adb), computer (Windows screen): happy paths, missing add-ons,
  unplugged phone, refusals, errors explained.

### 7.8 Settings (settings.py, server settings routes, pages/settings.js)
- Every section and control saves, reloads, validates and explains errors; Advanced sections; instruction
  and team-rules editors; voice (Urdu); look; phone pairing; lessons; updates/check-up; a file the engine
  cannot load is never written.

### 7.9 App shell (app.js, ui.js, index.html, app.css, sw.js)
- Sidebar (collapse, recents, meters, badges), routing (every page, bad routes), toasts, confirm and dialog
  boxes, shortcuts, update banner/overlay, SSE reconnect, side panel resize, unpaired page, service worker,
  first run with nothing configured.

### 7.10 Launching, installing, Windows (launcher.py, __main__.py, server.main, install/*.ps1, crew.cmd)
- Icons, windowless start (log, message boxes), terminal start detached, running Crew found without the
  proxy, stuck Crew replaced, double click, start with Windows, installer/uninstaller, crew doctor.

### 7.11 Security (server.py, chat.py, runs.py, util.py)
- Loopback vs network, pairing tokens, X-Crew + Origin on writes, /internal, file routes (no traversal, no
  secrets), uploads (size, names), redaction on every outward text, no shell built from owner text.

### 7.12 File-by-file reading list (every line)
crewlib: orchestrator, scheduler, agents, tools, store, gitops, prompts, tiers, scorecard, lessons,
quality, config, cli, hook, mcp_server, web, usage, claude_cli, connections, util, __main__.
crewapp: server, chat, runs, settings, workflows, skills, captures, updater, launcher, writer, browser,
phone, computer, devices_mcp, sse.
static: ui.js, app.js, live.js, voice.js, devices.js, pages/*.js, index.html, app.css, sw.js,
offline.html, unpaired.html.
install: install-windows.ps1, uninstall-windows.ps1, the .cmd launchers, crew.cmd.

---------------------------------------------------------------------------------------------------------------

## 8. Scenarios to run end to end (each with its expected outcome)

1. First run: no crew.toml, no subscriptions; Claude Code present/absent; Codex absent → Crew opens, says
   exactly what to do, nothing crashes.
2. The owner's exact subscriptions: chats on each; a project on all four; a project restricted to one.
3. Usage limits hit by: a chat mid-answer; the lead; a builder; a reviewer; the judge; the CEO; the prompt
   writer; all at once → work moves or waits, the owner is told, nothing is lost or double-counted.
4. Updates from 2.2 and 2.3.0; GitHub unreachable; download cut; locked file; automatic update with and
   without activity → either updated and reconnected, or unchanged with a plain explanation.
5. Close the window and click the icon; double click; restart during a project; restart during an
   update; close the terminal → Crew always opens exactly once.
6. A very long chat; 20 attachments; an Urdu file name; a 300 MB file → works or is refused clearly.
7. Delete a chat while it answers; rename; pin; the same chat open on the phone → consistent everywhere.
8. Stop a project during planning, building, a merge, the final review; continue each → resumes exactly.
9. A workflow every minute for ten minutes; one due while Crew was closed; one edited while running; one
   on a subscription at its limit → runs the right number of times, results kept.
10. Damaged files: crew.toml, secrets.env, a chat file cut in half, an old team.db, memory.db locked,
    update.json empty, running.json with a stale process → Crew starts, keeps everything it can, explains.
11. Phone: pair, second phone, unpair, phone open during an update.
12. Private messages: each agent, a helper, the CEO, Ask now, a draft while paused, a finished project.

---------------------------------------------------------------------------------------------------------------

## 9. Memory and long-term continuity — nothing is lost, context carries over

### 9.1 In Crew
- **Chats**: every message saved as soon as it exists (atomic writes; a crash never leaves half a file);
  history reloads after restart and update; Claude session ids kept so conversations resume with their
  context; a chat that moves subscription keeps its context (Claude: session copied; ChatGPT: recap — check
  the recap covers enough and never duplicates); compaction at the context limit keeps the thread; the
  context ring is accurate; long chats load quickly; attachments stay reachable.
- **Projects**: every agent's session id stored and resumed; handover notes, brief, plan, decisions,
  reviews, private conversations and the team chat persisted; resume after stop or crash continues where it
  was; REPORT.md and chat.md kept; agents in long projects rely on notes when Claude Code compacts.
- **Team memory**: lessons in memory.db persist across projects, are de-duplicated and weighted, render the
  most useful first, do not grow without bound, survive corruption and migrations; the CEO's effort record
  and the scorecard accumulate correctly (180-day window counts recent work; older data is kept, not lost).
- **Settings, rules, instructions, keys, pairing**: never touched by updates; migrations additive.
- **Library and workflow history**: persisted and listed quickly as they grow.
- **Housekeeping**: logs that grow forever (app.log, run logs) rotated or capped; temporary folders
  cleaned; nothing important held only in memory.

### 9.2 In this campaign (my own continuity)
- This file is the single source of truth: section 0 (campaign state) is updated after every step, the
  ledger after every fix, and a round summary after every round.
- Before any long step, the next step is written down; after any break or summary, work resumes from
  section 0, never from recollection.
- Fixes are committed in small, described commits, so the repository history is also a record.

---------------------------------------------------------------------------------------------------------------

## 10. Done means

- No console errors and no failed requests on any screen or flow, at both sizes, in both themes.
- The API fuzzer finds no 500, no traceback and no hang.
- Lint clean (Python and JavaScript); the full suite green plus a regression test for every fix.
- Every ledger entry has a root cause, a fix and, where possible, a test.
- Memory and continuity checks in section 9.1 pass.
- A release on Crew-AI, rehearsed with the owner's installed updater.

---------------------------------------------------------------------------------------------------------------

## 11. Round summaries

(One paragraph per completed round: what was checked, what was found, what was fixed, what is left.)

---------------------------------------------------------------------------------------------------------------

## 12. Bug ledger

(id · area · symptom · root cause · fix · test)

### Third continuation (2026-09-28) — found and fixed

Each test named here failed on the code before its fix and passes after it (both run, in this container).

- C14 · P1 · orchestrator.track_progress · a task whose checks can never pass (a broken check command, a
  missing tool, or C15) was sent back for ever: 216 rounds in 4 minutes with the fakes, and with real agents
  the owner's usage without end (the default has no time limit) · every change of a task's status counted as
  progress and reset the stall count, so a task going round review → changes → review looked busy and the
  stall ladder (lead replans twice, CEO ruling, honest stop with REPORT.md) never started · only a step
  forward counts: a new task, or a task reaching a stage it had not reached before (STAGES); a task moved up
  to the manager starts afresh · test_c14_a_task_sent_back_again_and_again_is_not_progress (unit),
  test_c14_a_task_whose_checks_can_never_pass_ends_with_an_honest_stop (end to end, fake knob
  `broken_checks`; ~30 s after the fix, "run did not finish in time" before it).
- C15 · P1 · gitops.run_checks · on Python 3.12+ (what the installer sets up) a project's first task, its
  skeleton, failed its check for ever when the check was `python -m unittest …` or pytest, because no tests
  exist yet · test runners exit 5 with "no tests ran" when they find none, and any non-zero exit failed the
  check · exit 5 whose last line says "no tests ran" is noted, not failed (a real failure that exits 5 still
  fails) · test_c15_a_test_runner_that_finds_no_tests_yet_is_not_a_failure. This is also why the cloud's
  E2E tests looped: they pass on Python 3.12 now (full suite 160 OK under 3.12).
- CI · the cloud lint step failed on the two findings triaged as noise in the handoff (S108 in __main__,
  B905 in orchestrator); both are now recorded in `.github/campaign/ruff.toml`.

### Earlier fixes — passing suite reported in handoff; old-code proof still pending

Tests are in crew/tests/test_campaign.py; each test's name starts with the ledger id.

- C1 · lessons · built-in lessons gained weight at every update · the seed "version" was the file's date
  and re-seeding reinforced existing lessons · version = content hash; re-seeding only adds missing seeds
  (`_seed`, never reinforces) · test_c1_*.
- C2 · lessons · an unknown effort level in the record broke refine, CEO lessons, Settings → Lessons ·
  `EFFORT_ORDER.index()` on any stored value · unknown levels rank last, teach nothing · test_c2_*.
- C3 · orchestrator · a damaged or locked cost memo failed the whole project on the next assignment ·
  unguarded json.loads/DB read · empty model on any error, numbers only · test_c3_*.
- C4 · orchestrator · an error after delivery turned a delivered project into "failed" · retrospective and
  cost model unguarded · both guarded, logged · test_c4_*.
- C5 · agents · a Codex turn that raised left its seat busy for ever · no exception handling in the turn
  thread · error result emitted, seat freed · test_c5_*.
- C6 · agents · one odd stream-json message silenced a Claude seat (no more events, no exit) · exception in
  `_handle` ended the reader · guarded per message · test_c6_*.
- C7 · gitops · a check that left a server running hung the merge (Windows: for ever); on Linux it ran into
  the time limit and failed a passing check · output through a pipe held by the child · output to a file,
  own process group, the group ended afterwards · test_c7_* (2).
- C8 · connections · a connections.json of the wrong shape crashed every Claude start · shape not checked ·
  only the right shapes read; a damaged file is kept aside before a save · test_c8_*.
- C9 · cli · LATEST without an encoding; setup/doctor with no time limits and a bare program name (not
  found on Windows) · encodings, timeouts, full paths from agents.which · (by reading).
- C10 · mcp_server (and devices_mcp) · a JSON line that is not an object killed the team-tool server ·
  answered with "invalid request" · test_c10_*.
- C11 · claude_cli · a damaged claude-cli.json stopped the daily Claude Code update · guarded · test_c11_*.
- C13 · web (terminal live view) · any web page could post "owner" messages to a running team or stop it,
  and a rebinding page could read the chat · no CSRF/Host check · X-Crew header on writes, Host must be
  this computer · test_c13_*.
- A1 · settings · a damaged or refused crew.toml stopped Crew from opening · load() raised everywhere ·
  set aside with a dated name, last good copy (.bak) or defaults, owner told (toast + Settings card) ·
  test_a1_* (2).
- A2/A3 · server · bodies that are not objects, fields of the wrong type → 500 · no shape checks · 400 with
  plain words · test_a2_a3_*.
- A4 · runs/orchestrator · after an app restart a quiet project looked stopped; Continue started a second
  orchestrator · activity-based liveness · heartbeat thread; resume refused while alive (app and CLI) ·
  test_a4_* (2).
- A5 · runs · a request starting with "-" never started; very long requests exceed Windows' command line ·
  request passed as an argument · request file · test_a5_*.
- A6 · runs · the app kept the run log open (Windows lock) · handle not closed · closed after the start ·
  (by reading).
- A7 · cli · two projects in the same second with the same words shared a folder · id from time + words ·
  claimed with an exclusive mkdir, numbered · test_a7_*.
- A8 · updater · a locked file stopped the update half-way (half a program); pip failure after the swap
  raised; raw zip/network errors · file-by-file copy · staged copies, swap with rollback, pip never fatal,
  plain UpdateError, one install at a time, leftovers tidied · test_a8_* (5).
- A10 · server · start-with-Windows / icon refresh: PowerShell strings unquoted, results unchecked ·
  quoted, checked, explained · (Windows only: by reading).
- A11 · server.upkeep · one failing step skipped the rest (update check, skills) · one try for all ·
  per-step guard · test_a11_*.
- A12 · launcher/server · console output decoded as cp1252 · utf-8 with replace · (Windows: by reading).
- A13 · launcher · a damaged running.json broke the start · shape guard · test_a13_*.
- A14 · server.App · a non-object app.json or a locked skill file stopped Crew from opening · guarded ·
  test_a14_*.
- A15 · chat (ChatGPT) · errors left the chat "still answering"; Stop showed an error; deleting while
  answering wrote into a deleted chat · no try/finally, stop not tracked; and a race: the answer thread
  checked that the chat existed, the delete ran, then the answer was written (found by test_a15_deleting…
  on its first run) · give-up path, "Stopped.", answers written only while their chat exists, checked in
  the same SQL step as the writing (ChatDB.add_message), chat row deleted before its messages ·
  test_a15_* (3).
- A16 · chat (Claude) · an error while finishing an answer left the chat busy for good · `_finish`
  unguarded · give-up path keeps what was said · test_a16_*.
- A17 · chats list · chats older than the newest 300 could not be found · search in the page only ·
  server-side search (?q=) with LIKE escaping · test_a17_*.
- A18 · chat.send · a model of the other product accepted, failing later with a raw error · refused
  plainly before anything is recorded · test_a18_*.
- A19 · workflows · a deleted chat kept its workflow "running" up to 6 hours · missing chat not noticed ·
  treated as finished · test_a19_a20_*.
- A20 · workflows · bad schedules gave raw errors; banned models accepted until run time · validated on
  save · test_a19_a20_*.
- A21 · skills · a "when" with ": " or " #" broke the skill's front matter; Urdu names had no id · quoted
  description, hashed id · test_a21_*.
- A22 · phone · missing program / timeouts → raw 500 · PhoneError in plain words · test_a22_*.
- A23 · settings.save_secret · a key pasted with a line break wrote extra lines · stripped, refused if
  multi-line, type-checked · test_a23_*.
- A24 · chat.save_attachment · Windows device names (CON, NUL, COM1 …) · prefixed · test_a24_*.
- F1–F6 · front end and crew.cmd · see the candidates list · fixed in the page code (F1 voice handlers,
  F2 a restart mid-answer ends the wait, F3 untitled chats, F4 refused list saves roll back, F5 the
  history dialog closes through its own close, F6 crew.cmd keeps Crew's exit code) · to be checked in the
  browser sweep (Windows cmd: by reading).
- A25 · server routes · Skills → "Add Anthropic's skills" never showed progress, "done" or its error ·
  GET /api/skills/anthropic was answered by the earlier GET /api/skills/<id> route (404 "No such skill") ·
  the fixed routes come first; "anthropic" reserved as a created skill's id · test_a25_* (also checks that
  no plain route is hidden by an earlier pattern, for every route).
- A26 · server routes · "Sign in" for a subscription whose name has @ or + (Settings allows both) → 404 ·
  the route's pattern was narrower than the name rule · pattern widened to settings.ACCOUNT_NAME's set ·
  (by reading; covered by the route scan above only for plain routes).
- Also closed while scanning: an `os.devnull` fallback opened without UTF-8 (__main__); `pid_alive` with a
  number no process can have (OverflowError); the icon record read as an object without a check.

Follow-ups noted, not yet done:
- C12 · lessons · derived CEO effort lessons keep the numbers of the first time they were written (a
  lesson "reinforced" by a later, different number keeps the old text).

### Candidates from the full reading pass (round 1) — to verify, fix and test

Priority: P1 = data loss / Crew does not open / a project or chat stuck for good; P2 = a feature fails or a
raw error; P3 = latent risk, polish.

Engine (crewlib)
- C1 · P1 · lessons · seed lessons are re-added after every update (seed "version" = the file's mtime,
  which every install changes), and `add()` reinforces them, so seed weights grow with every update and
  crowd out the team's own lessons · version by content hash; re-seeding adds only missing seeds.
- C2 · P2 · lessons.effort_stats · `EFFORT_ORDER.index()` raises for a stored effort outside the list
  (older rows, hand edits) → refine, CEO lessons and Settings → Lessons fail · rank unknown efforts last.
- C3 · P1 · orchestrator.lessons_cost_model · unguarded `json.loads` + DB errors on every assignment tick:
  a damaged memo row or a locked memory.db stops the whole project ("failed") · fall back to {}.
- C4 · P2 · orchestrator.retrospective · `update_cost_model` unguarded: an error after delivery turns a
  delivered project into "failed" · guard.
- C5 · P1 · agents.CodexSeat._turn · no exception handling: any error (program gone, file race) kills the
  turn thread; the seat stays busy and no result arrives · catch and report an error result.
- C6 · P2 · agents.ClaudeSeat._read_stdout · an exception in `_handle` ends the reader (no exit event) ·
  guard per message.
- C7 · P1 · gitops.run_checks · output through pipes with `shell=True`: on Windows a check that leaves a
  server running keeps the pipe open, so the run hangs even after the timeout → the merge job never ends
  and nothing more is merged · output to a file, own process group, kill the group on timeout.
- C8 · P2 · connections.load · valid JSON of the wrong type (a list, `"mcp": null`) crashes every Claude
  start · accept only the right shapes.
- C9 · P3 · cli · `LATEST` read/written without an encoding; doctor/setup subprocesses without timeouts.
- C10 · P3 · mcp_server · a JSON line that is not an object crashes the team-tool server.
- C11 · P3 · claude_cli.state · non-object JSON crashes the daily Claude Code update.

App (crewapp)
- A1 · P1 · settings.load · a damaged or hand-edited crew.toml (syntax error or a refused value) raises
  everywhere: Crew does not open at all (message box) · never raise for a bad file: set it aside with a
  dated name, use the last good copy (crew.toml.bak) or defaults, tell the owner (notice + Settings).
- A2 · P2 · server._body · a JSON body that is not an object → AttributeError → 500 on every write route ·
  refuse with 400 and a plain message.
- A3 · P2 · handlers · wrong-typed fields (title/text numbers, schedule a string, a settings section a
  list …) → 500 · validate types where the values are used.
- A4 · P1 · runs.running · after an app restart a quiet project (all subscriptions at their limit, or a
  long step) shows as not running; "Continue" then starts a SECOND orchestrator on the same project ·
  heartbeat from the orchestrator; resume refuses while it is alive.
- A5 · P1 · runs.start · the request is a command-line argument: text starting with "-" is read as an
  option (the project never starts: "starting" forever); very long requests exceed Windows' command line ·
  pass the request through a file.
- A6 · P3 · runs._spawn · the run log's file handle is never closed in the app (a Windows lock).
- A7 · P2 · cli.new_run_id · two projects started in the same second with the same first words share one
  folder · unique ids.
- A8 · P1 · updater.install · files are replaced one by one; a locked file (Windows) stops it half-way →
  half a program; a pip timeout after the swap raises before the restart → mixed versions; download/zip
  errors come back as raw 500s · stage, swap with rollback, pip never fatal, plain errors.
- A10 · P2 · server.set_start_with_windows / refresh_windows_icons · PowerShell strings not escaped (an
  apostrophe in the user folder breaks them); result not checked; a timeout → 500 · quote, check, explain.
- A11 · P2 · App.upkeep · one failing step (e.g. a decode error in heal_shortcuts) ends the thread: the
  update check and the skills install are skipped · guard each step.
- A12 · P3 · launcher/server · `text=True` subprocess output decoded as cp1252 on Windows · utf-8, replace.
- A13 · P2 · launcher.recorded · non-object JSON / wrong types in running.json crash the start · guard.
- A14 · P1 · App.__init__ · a non-object app.json or a failing skills-pack rebuild (a locked file on
  Windows) stops Crew from opening · guard.
- A15 · P1 · chat.CodexSession · exceptions other than OSError leave the chat "Still answering" for good;
  Stop on a ChatGPT chat shows "Something went wrong" instead of "Stopped"; deleting it while it answers
  raises · try/finally, track the stop.
- A16 · P1 · chat.ClaudeSession._finish · an exception while collecting new files (a file removed during
  the scan) leaves the chat busy for good · robust scan; always clear busy.
- A17 · P2 · chats list capped at 300 and searched in the page · older chats cannot be found or reopened
  after months of use · search all chats on the server.
- A18 · P2 · chat.send · a model of the other product (gpt-… in a Claude chat, claude-… in a ChatGPT chat)
  is accepted and fails later with a raw error · refuse with a plain message.
- A19 · P2 · workflows._watch_chat · a deleted chat keeps its workflow "running" for up to 6 hours (runs
  blocked) · treat a missing chat as finished.
- A20 · P3 · workflows._clean · bad schedule values give raw Python errors; banned models accepted until
  run time · validate with plain messages.
- A21 · P2 · skills.create · the description is written as a bare YAML value: a "when" with ": " or " #"
  breaks the skill for Claude Code · quote it; Urdu names get a proper id.
- A22 · P3 · phone._adb · timeouts and a missing program surface as raw 500s · plain PhoneError.
- A23 · P3 · settings.save_secret · a pasted key with a line break writes extra lines · strip / refuse.
- A24 · P3 · chat.save_attachment · Windows reserved names (CON, NUL, AUX, COM1 …) · prefix them.

Front end
- F1 · P2 · voice · a spoken conversation started on the start screen never hears its answer (the handlers
  stay with the page that was left) · hand them to the chat page.
- F2 · P2 · chat page · if Crew restarts in the middle of an answer the page waits for ever (spinner, no
  answer) · finish with a plain notice.
- F3 · P3 · chats page · a chat without a title breaks the list.
- F4 · P3 · settings · removing an allowed model/ban word shows it removed even when the save is refused.
- F5 · P3 · workflows · the history dialog is closed by removing it from the page (Escape listener leak).
- F6 · P3 · crew.cmd · `%errorlevel%` is read before `py` runs: the exit code is always 0.
