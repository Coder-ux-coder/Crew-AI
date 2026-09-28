# Crew — the full debugging campaign

My working brief for a complete, repeated bug hunt through every part of Crew. It is deliberately
exhaustive. It says what "working" means for every part of the product, how each part is likely to break,
which scenarios must be run, how every fix is proved, and how the work itself is protected from memory
loss over a long campaign. It is updated as the campaign runs; the campaign state and the bug ledger are
the source of truth for what has been done.

---------------------------------------------------------------------------------------------------------------

## 0. Campaign state (updated after every step — read this first after any break or context summary)

- Status: RUNNING — the owner gave the go-ahead on 2026-09-28 ("execute this prompt and do whatever it says").
- Passes: the owner asked (2026-09-28) for the whole campaign to be run three times: finish pass 1 (all
  rounds), then run every round again as pass 2, then again as pass 3. Now: PASS 1 of 3.
- Round: 1 — tooling and static sweep (in progress).
- Done: brief written; the whole codebase read line by line (crewlib, crewapp, static, tests, install);
  ~40 candidate defects noted in section 13; baseline test run started.
- Next: finish the baseline test run → ruff/pyflakes/compileall triage → eslint over all front-end
  modules → custom scans (encodings, creationflags, subprocess timeouts, icons, element ids, API paths,
  CSS variables) → fix the confirmed defects with regression tests → whole-app browser sweep.
- Owner's decisions: 2026-09-28 — no Urdu. Urdu was removed from the app (Settings → Voice language list and
  hints, the automatic Urdu voice, texts and README) and from this brief (reference setup, right-to-left work,
  Urdu test cases). General handling of non-English text stays (e.g. B-01 protects Crew's own "→"/"✔").
- Open questions: none.
- Where the work goes: the session's development branch `claude/funny-turing-8ra205` (the harness
  requires it); the owner merges or releases to `Crew-AI` from there. Version bump (2.3.1) and release
  notes are prepared on the development branch; nothing is pushed to `Crew-AI` without the owner's say.

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
  the same Wi-Fi, a slow or proxied office network, and voice dictation in English;
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
   quoting (single quotes doubled); paths with spaces, apostrophes and non-English letters; `pythonw.exe` has no console
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
   - an API fuzzer from the server's route table: every route with missing, empty, wrong-type, huge, non-ASCII,
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
  possible; dictation in English; Crew started from the desktop icon and with Windows.

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
  apostrophes, non-English letters, very long names; path traversal in anything that serves or writes files.
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
  focus and keyboard (Enter, Shift+Enter, Escape, Tab); long names.
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
- Markdown: tables, code, links (safe), images, long text — and no HTML injection.
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
  and team-rules editors; voice; look; phone pairing; lessons; updates/check-up; a file the engine
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
6. A very long chat; 20 attachments; an oddly named file (spaces, symbols, accents); a 300 MB file → works or
   is refused clearly.
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

- O-01 · voice/settings · the owner does not want Urdu · — · removed the Urdu dictation language and its
  hints (Settings → Voice), the automatic Urdu voice when reading aloud (Arabic text still gets an Arabic
  voice), Urdu mentions in the agents' tool descriptions and the README; a language saved earlier that is no
  longer listed still shows as chosen, so no setting is lost · existing tests use other non-English text.
- B-01 · team tools, device tools (Windows, ChatGPT agents) · a team message containing "→" or "✔" (symbols
  Crew itself writes: "Task #3 merged… ✔", private-message arrows) made every tool call of a GPT-6 Sol agent
  fail; non-English text for computer_type arrived garbled or killed the tool server · Codex starts tool
  servers with only the environment variables named in their config, so PYTHONUTF8 was lost and Python used
  the console code page (cp1252) for stdio · tool servers (mcp_server, devices_mcp, hook) switch stdin/stdout
  to UTF-8 themselves; Codex tool-server env also carries PYTHONUTF8/PYTHONIOENCODING; a JSON line that is
  not an object is answered "invalid request" instead of killing the server ·
  test_campaign.ToolServersSpeakUtf8 (both fail before the fix).
- B-02 · device tools behind an office proxy · with a system web proxy set (office network), every browser_*,
  computer_* and phone_* tool of chats and agents failed ("The Crew app is not running…") · devices_mcp reached
  the app on 127.0.0.1 through urllib's default opener, which sends loopback requests to the proxy unless the
  proxy's bypass list names them · a direct opener (no proxy) for the app, as the launcher already does ·
  test_campaign.DeviceToolsIgnoreTheOfficeProxy.
- B-03 · chats · a chat could stay "Still answering" until Crew was restarted: a Claude answer whose end-of-turn
  bookkeeping failed (a file vanishing while Crew listed the files it made; the database busy while saving the
  context size), or a ChatGPT turn hitting any unexpected error (reading Codex's status file, an odd output
  line, the chat deleted mid-answer) · the end of a turn ran unguarded in a reader thread; the thread died and
  nothing reset the busy flag · every turn now ends in a guard that keeps what was said, explains the problem
  in the conversation and frees the chat; file listings (chat files, Library, new files) skip files that
  vanish; nothing is written for a chat deleted mid-answer; child output is drained without growing lists ·
  test_campaign.ChatsNeverStayBusy (3 tests; all fail before the fix).
- B-04 · updating · an update copied the program file by file: a file Windows would not let go of (antivirus,
  Explorer showing the icon) stopped it half way, leaving half an old and half a new program; a download cut
  short ended in a raw "File is not a zip file" error (500) with a temp folder left behind; pip slower than 15
  minutes raised after the files were replaced, so Crew never restarted · no staging, no rollback, pip errors
  not caught · the whole new version is unpacked and staged beside the program first (a bad download or a full
  disk stops there with nothing changed); files are then swapped with brief retries for Windows' momentary
  locks, and if one will not move every swapped file is put back; pip problems are logged and ignored; the app
  reports the plain reason (503) and does not restart; an automatic update that fails tells the open window
  (update_failed) instead of leaving "Updating Crew…" on screen for six minutes ·
  test_campaign.UpdatesAreAllOrNothing (3), UpdateFlowInTheApp (all fail before the fix).
- B-05 · updating (C11, C12) · "Update now" while a team project worked gave no warning; after an update the
  window waited for one exact version and, looking on neighbouring ports, went to any program that answered
  (no-cors probe), which could be something that is not Crew · the update asks first when a project is working
  (409 + confirm); the window waits for any version other than the one that was running and only follows a
  port whose /api/ping it can read (Crew lets pages on the same machine read its ping); the offline page does
  the same; service-worker cache bumped so the new offline page reaches installed copies ·
  test_campaign.UpdateFlowInTheApp.

---------------------------------------------------------------------------------------------------------------

## 13. Candidates noted while reading (to verify, then fix or dismiss)

Each is checked by running it where possible; confirmed ones move to the ledger (section 12).

**Windows and encodings**
- C01 mcp_server/devices_mcp read and write stdio in the locale code page (cp1252) when Codex starts them
  (Codex passes MCP servers only a short list of environment variables, so PYTHONUTF8 is lost): team chat
  containing "→" or "✔" (Crew's own symbols) fails; non-English text for computer_type is garbled or kills
  the tool server. → B-01.
- C02 set_start_with_windows and refresh_windows_icons put paths in PowerShell single quotes without
  doubling apostrophes (launcher._ps exists); Start-with-Windows reports success even when PowerShell fails.
- C03 run_checks on Windows uses cmd.exe (team rules say checks run with bash; plan_ready itself suggests
  `test -s report.md`, which cmd lacks) → a check that can never pass on Windows.
- C04 run_checks timeout: subprocess.run kills only the shell; grandchildren (servers started by tests) keep
  running and, on Windows, keep the pipes open so the timeout itself can hang.
- C05 crew doctor / crew setup run `codex`/`claude` by bare name: on Windows an npm .cmd shim is not found
  (FileNotFoundError traceback); no timeouts.
- C06 launcher.command_line decodes PowerShell output as UTF-8 without errors="replace" → a non-ASCII
  command line makes a stuck Crew impossible to identify.
- C07 connections: stdio arguments split on spaces (`args.split()`), so a Windows path with spaces breaks.
- C08 devices_mcp calls the app through urllib with the system proxy: an office proxy breaks the agents'
  browser/computer/phone tools (the launcher already bypasses the proxy for this reason).
- C09 (dropped 2026-09-28: the owner does not want Urdu; Urdu was removed from the app and this brief).

**Updating**
- C10 updater.install copies file by file: a locked file (antivirus, Explorer on the icon) stops it half
  way → half a program; a cut download raises BadZipFile (500 with a raw message); temp folder left behind;
  a pip timeout raises after the files were replaced (no restart, raw error).
- C11 manual "Update now" while a project runs: no warning (auto-update already waits).
- C12 window reconnect after an update waits for `info.latest`; if the installed version differs, the
  window never recognises its own Crew (scans other ports instead).

**Projects (runs)**
- C13 the request is passed on the command line: on Windows a long request (> ~32k chars) cannot start;
  should go through --request-file.
- C14 two starts in the same second with the same first words share one run id and one folder.
- C15 "Continue" can start a second orchestrator for a project that is still running (the app only knows
  the processes it started itself; after an app restart, or when the team is idle waiting for a usage
  limit, the project looks stopped). Needs a heartbeat from the orchestrator.
- C16 the app-run.log file handle is leaked on every project start.
- C17 double submission: Enter twice on the home screen starts two projects / creates two chats.

**Team engine**
- C18 failover: copy_claude_session errors (disk, permissions) propagate and stop the whole project.
- C19 memory.db problems (locked, corrupt) in lessons_cost_model / scorecard stop the whole project from
  the build loop; retrospective errors after delivery mark a delivered project "failed".
- C20 resume after a subscription was renamed/removed: cfg.account(prev account) raises → the project
  cannot be continued.
- C21 CodexSeat._turn: an exception in the turn thread leaves the seat busy forever with no exit event.
- C22 config.restrict with explicit seats and no Claude seat on the chosen subscriptions → StopIteration.
- C23 connections.mcp_servers: one malformed connection entry (missing url) breaks every chat and agent.

**Chats**
- C24 CodexSession._turn: any exception (settings load, odd JSON line, status read) leaves the chat busy
  forever ("Still answering").
- C25 ClaudeSession._finish → _new_files: a file vanishing during the scan (FileNotFoundError) leaves the
  chat busy forever.
- C26 a corrupted message meta row makes the whole chat impossible to open (json.loads unguarded).
- C27 workflows: deleting a workflow's chat while it answers keeps the workflow "running" for 6 hours;
  runs interrupted by an app restart stay "running" in the history forever.

**API robustness (fuzz class)**
- C28 request bodies that are JSON but not objects (list, string, number) → AttributeError → 500.
- C29 non-ASCII pairing token/cookie → secrets.compare_digest TypeError → 500 on every request.
- C30 wrong-typed fields (title/text numbers, env/headers/args of the wrong type, schedule not an object)
  → 500s; to be enumerated by the fuzzer.
- C31 phone: adb timeouts surface as 500 "Something went wrong" instead of a plain message.

**Data files**
- C32 JSON state files that parse but are not objects (running.json, update.json, claude-cli.json,
  connections.json, skills-state.json, app.json) crash their readers.
- C33 settings: a hand-edited crew.toml with a syntax error or a wrong type makes /api/settings and the
  overview fail with a raw message; app start behaviour to be checked.
