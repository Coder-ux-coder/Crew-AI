# Handoff to Codex: the Crew debugging campaign

## Update, 2026-09-29, fifth continuation (read this first)

- **Branch:** everything is on `claude/friendly-sagan-bj2ykb`, which carries all of `claude/jolly-wright-lz430a`
  plus the work below; the cloud checks also run on pushes to it.
- **A correction to the update below:** `claude/funny-turing-8ra205` was **never merged** into jolly-wright (it is
  not an ancestor; `git log origin/claude/jolly-wright-lz430a..origin/claude/funny-turing-8ra205` lists its six
  commits). Its bugs are all covered now: B-01 and B-02 brought over as C31 and A59, B-05's reconnect as F21 (each
  with its own test); B-03 and B-04 were already fixed differently on this line (A15, A16, C5; A8). Its "no Urdu"
  change (voice and dictation in Urdu removed, recorded as the owner's decision on 2026-09-28) was **not**
  applied: the brief written after it lists Urdu dictation. Ask the owner.
- **Fixed in this continuation (each with a test that fails without its fix; DEBUG-CAMPAIGN.md section 12, "Fifth
  continuation"):** C31 (tool servers in UTF-8 on a Windows code page), A59 (device tools behind an office proxy),
  **C32 (P1 on Windows: the team's checks ran in cmd.exe although the agents write them for bash, so they could
  never pass; they now run in Git for Windows' bash)**, A60 (odd pairing codes), A61 (viewers that hang up), A63
  (quoted connection arguments with spaces), A65 (the phone's screen list from an earlier screen: taps in the wrong
  place), A66 (saving a key beside Notepad's older encoding), A67 (dot-only project ids), C33 (Urdu file names
  escaped the scan; clashes in names with spaces), F20 (the chat catches up when its connection comes back), F21
  (after an update only Crew is taken for Crew; a rolled-back Crew is recognised), F22 (Urdu reads right to left:
  answers, messages, the team conversation, titles, the message boxes).
- **Tests:** full suite 240 OK in 7 min 9 s (Python 3.11, Playwright + Chromium, 0 skipped) before F22 and A67;
  their own tests and the Markdown tests after. ruff (cloud config), eslint and `node --check` clean.
- **What is left:** (1) the release, only with the owner's go-ahead; (2) on the owner's Windows PC: a small team
  project (its checks now run in Git's bash), the desktop icon, the Computer page's double-click, a message in
  Urdu; (3) the owner's answers on Urdu and on the per-model weekly limit; (4) carry on the clean round.

## Update, 2026-09-29, fourth continuation

This file was written at the end of the third continuation. Since then (fourth continuation, Claude):

- **Branch:** everything is on `claude/jolly-wright-lz430a`, which carries all of `claude/exciting-heisenberg-6w6gge`
  (fast-forward) plus the work below. (It does not carry `claude/funny-turing-8ra205`: see the fifth continuation
  above.) The cloud checks
  (`.github/workflows/campaign.yml`) now also run on pushes to this branch.
- **The owner's model change:** Sonnet 5.5 (`claude-sonnet-5-5`) replaces GPT-6 Sol as the team's **workhorse**.
  Opus 5.5 stays the manager, GPT-6 Astra the CEO (Fable 5.1 its backup). Seats now carry their tier
  (`SeatSpec.tier`, seats table column `tier`, `tiers.seat_tier(seat)`); workhorse seats are Claude seats running
  `models.workhorse`, spread over the Claude subscriptions; ChatGPT subscriptions get no seats (they run the CEO and
  ChatGPT chats). `models.codex` is gone (an old file still loads); settings version 4 moves the owner's file once
  (Sonnet unbanned and allowed, `gpt-6-sol` banned, ChatGPT chats' default now GPT-6 Astra, Sol chats and
  workflows moved to Astra). See DEBUG-CAMPAIGN.md section 12, "Fourth continuation".
- **Fixed since (each with a test that fails before the fix):** C24–C30 (team engine: a reviewer sent back to a
  subscription that just ran out; a task held for a free seat that cannot take it; the project's own commit hooks
  refusing Crew's merges; a file held open on Windows; Notepad's byte-order mark; the CEO's effort record mixing
  models; crew.cmd's encoding) and F10–F19 (front end: **F10 was a P1 script-injection hole in the Markdown
  renderer**; F12 a double submission that started one team per key press; forms that lost what was typed; …).
- **Rounds:** 6 (team engine), 8 (front end) and 10 (Windows, by reading) are done.
- **What is left, in order:** (1) a full test run (see below); (2) the release — **only with the owner's
  go-ahead** (2.3.1 in `crew/VERSION.json` with plain notes, merge into `Crew-AI`, rehearse the installed updater);
  (3) on the owner's Windows PC: the desktop icon, one small team project with Sonnet 5.5 workhorse seats, the
  Computer page's double-click; (4) the open question below; (5) a full clean round: the remaining checklists of
  rounds 3, 4, 5, 7 and 9 (DEBUG-CAMPAIGN.md sections 7–9), then every check again until nothing new is found.
- **Minor notes, not fixed:** quality.scan checks files whose names git quotes (Urdu names) for secrets but not for
  risky lines; the agents' tier guide names the models as set now and does not follow a later change in
  Settings; ChatGPT's usage is not read during team projects (the CEO's runs are `--ephemeral`).
- **Tests:** the last complete run was 216 OK (after C29); every later fix's own test and the related browser tests
  pass. A full run after F10–F19 was started and interrupted: run it first. The browser tests need the Python
  `playwright` package (`pip install playwright`; Chromium is at /opt/pw-browsers).
- **Open question:** a subscription at a model's own weekly limit (an Opus-only cap, if a plan has one) is parked
  whole, which now also pauses the Sonnet workhorse seats on it. Needs a real limit event to decide.

---

This file hands over an unfinished job. It tells you what the owner asked for, what has been done and verified,
how I reasoned, and exactly what to do next. Read it once end to end, then read `DEBUG-CAMPAIGN.md`
(same folder). That file is the owner's brief, the campaign state and the bug ledger. It is the source of
truth, so keep it updated as you work.

Both files live at the repository root, outside `crew/`. Crew's updater only copies `crew/`, so these files
never reach the owner's computer. Keep it that way.

---

## 1. What the owner asked for

The owner (not a programmer; a government investment board's CEO in Punjab, Pakistan) uses **Crew**, a Python
app that runs Claude Code and Codex as a team of AI agents, plus chats, workflows, a phone/computer/browser
controller, and a one-click updater. They said Crew is "really buggy" and asked for an exhaustive, repeated bug
hunt across everything, with nothing ever lost.

The full brief is `DEBUG-CAMPAIGN.md`, sections 1–10. The non-negotiable ground rules (section 2) are:

1. **The owner's data survives.** Never delete or reset owner data. A damaged file is set aside with a dated
   name and the owner is told.
2. **Root cause, minimal fix.** Every fix is small and addresses the cause.
3. **Proof.** Every reproducible bug gets a regression test that fails before the fix and passes after it.
   The full suite stays green, and Python and JavaScript lint stay clean.
4. **Secrets are never printed, logged, echoed or committed.**
5. **Windows is the owner's platform.** Guard every Windows path. The known traps are listed in section 2
   of the brief.
6. **Releases go to branch `Crew-AI`.** That is what the owner's installed updater downloads. Releases are
   version 2.3.x with plain notes, and the updater is rehearsed before a release is called done.
7. **Commits carry the required trailers** and no model identifiers anywhere else.
8. **Report honestly:** what was verified by running it, what was only read (Windows), and what is still
   open.

The method is to run rounds (section 3): static sweep, then dynamic sweep, then line-by-line review, then
fix/test/ledger, then repeat until a round finds nothing new. Round 1 is the only round done so far.

## 2. Where things stand (at handoff)

- **Branch:** `claude/exciting-heisenberg-6w6gge`. All work is committed and pushed there.
- **Not merged to `Crew-AI`.** That branch is the release branch the owner's updater reads. Merging, bumping
  `crew/VERSION.json` to 2.3.1 and writing release notes need the **owner's explicit go-ahead**. Ask them
  first.
- **Tests:**
  - The full suite (`cd crew && python3 -m unittest discover -s tests`) passed: 156 tests, 1 skipped.
  - The new regression file, `crew/tests/test_campaign.py`, has 41 tests. All pass.
  - Every new test's name starts with its ledger id (`test_a8_…`, `test_c7_…`).
  - I did **not** go back and prove each test fails on the old code. The ledger design says each should, and
    many visibly would (for example, they call functions that did not exist before). Doing it properly is a
    good first job for you: `git stash` or check out the parent commit for one fix, run its test, and see
    it fail.
- **Lint is clean** with the brief's rule sets:
  - **ruff:** `F,E9,B,PLE,PLW,RUF,S602,S604,S605,S307,S301,S506,S108`, ignoring `RUF001-003`. What remains
    is triaged noise, listed in §7.
  - **eslint 10:** the bug rules from the brief, with browser globals and ES modules. `caughtErrors`/`args`
    unused are ignored because they are noise. Result: 0 problems.
  - **`node --check`:** passes on every JS file.
- **Round 1 status:**
  - Done: the reading pass, the static sweep, the custom scans, and all fixes with their tests.
  - **Not done:** the dynamic sweep, meaning the API fuzzer and the Playwright browser harness (§4).

## 3. The codebase in one page

```
crew/
  crewlib/            the team engine (runs as its own process per project: python -X utf8 -m crewlib start …)
    orchestrator.py   the state machine: refine → plan → build → deliver; seats, reviews, merges, CEO, lessons
    agents.py         drives Claude Code (stream-json) and Codex (exec --json); ClaudeSeat, CodexSeat, _kill_tree
    store.py          SQLite per project (runs/<id>/team.db), thread-safe with an RLock; heartbeat read here
    gitops.py         worktrees, merges, run_checks (the project's verification commands)
    lessons.py        ~/.crew/memory.db: lessons, seed lessons, effort record, cost memo
    cli.py            `crew start/resume/say/...`; claim_run_dir; request file; setup/doctor
    web.py            the small live view `crew start` opens in a browser (terminal use only)
    config.py, tiers.py, scheduler.py, tools.py, mcp_server.py, prompts.py, scorecard.py, usage.py, util.py …
    __init__.py       on Windows patches subprocess.Popen to add CREW_NO_WINDOW (no console flashes)
  crewapp/            the app: ThreadingHTTPServer on 127.0.0.1:8765 (+ LAN for a paired phone)
    server.py         App (startup, upkeep, auto-update) + Handler with ROUTES (first match wins!)
    chat.py           chats: ClaudeSession (one long-lived claude process), CodexSession (one exec per turn)
    runs.py           team projects started from the app (spawned processes), liveness, resume
    settings.py       ~/.crew/crew.toml read/write, damaged-file set-aside, secrets.env
    updater.py        one-click update from the GitHub zip of branch Crew-AI; staged swap with rollback
    workflows.py, skills.py, connections (in crewlib), phone.py (adb), computer.py (Win32), browser.py
    (Playwright), captures.py, launcher.py (Windows icons, pythonw, find/replace a stuck Crew), sse.py
    static/           plain ES-module front end: app.js, js/ui.js, js/pages/*.js, index.html, app.css, sw.js
  tests/              unittest; tests/fakes/fake_agent.py stands in for claude and codex (scenarios via
                      CREW_FAKE_SCENARIO JSON); test_app.AppServer runs the real server in-process
  crew.cmd            Windows launcher (.gitattributes keeps it CRLF; the "LF will be replaced" warning is fine)
  VERSION.json        currently 2.3.0
```

Things that bite:

- **`ROUTES` in `server.py` are matched in definition order.** A plain route placed after a pattern that
  also matches it is unreachable. That was bug A25, and the test `test_a25_…` now checks every plain route.
- **Tests change `CREW_HOME`.** Use the `TempHome` helper in `test_campaign.py`. `test_app.py` sets `ENV`
  at import, and other modules reapply it in `setUpClass`.
- **Patching `os.replace`, `shutil.copy2` or `time.sleep` patches them for the whole process.** The updater
  tests do the first two briefly, on purpose. Prefer patching a module attribute (for example
  `updater.subprocess`) over patching a global.
- **The fake Codex has a new knob,** `codex_delay: N`. It makes Codex wait N seconds before answering, so
  tests can press Stop meanwhile.

## 4. What to do next, in order

1. **Prove the "fails before" half of the rule.** For a sample of the tests, at least the P1 ones (C1, C3,
   C4, C5, C6, C7, A1, A4, A5, A8, A14, A15, A16), run the test against the parent commit of its fix and
   confirm it fails. Record this in the ledger.
2. **Round 1 dynamic sweep, part A: the API fuzzer.** Generate requests from `server.ROUTES`:
   - Send every route with missing, empty, wrong-type (number/list/object/null), huge, Urdu, path-like
     (`../`, `C:\`) and hostile values.
   - Use `test_app.AppServer` with the fakes.
   - Flag any 500, any traceback on stderr, any hang over 30 s, and any change to data that should not
     change.
   - Remember the write rules: non-GET `/api/*` calls need the `X-Crew: 1` header, and a foreign `Origin`
     is refused.

   Known places that probably still give 500s, from reading:
   - `browser_action`: `x`/`y`/`dy` that aren't numbers, reaching `float()` inside BrowserService.
   - `phone_action` swipe: a missing `x1` gives a KeyError, which comes back as a 400 whose message is just
     "'x1'".
   - `computer.act` with odd types.
   - The workflows `PUT` endpoint with a non-dict body.
3. **Round 1 dynamic sweep, part B: the browser harness.** Playwright is preinstalled at
   `/opt/pw-browsers/chromium`. Test every screen at 1280×650 and 390×844, in light and dark themes. Click
   every control. Record console errors, failed requests, horizontal overflow and clipped text, and take
   screenshots. Check the front-end fixes F1–F5 in a real browser; they were only checked by reading and
   lint.
4. **Rounds 2–11** as in the brief's table (section 3): API and security; models and routing; updating;
   chats; the team engine; workflows and skills; front end line by line; memory and continuity; Windows
   paths. Keep going until a round is clean.
5. **Follow-ups already found:**
   - **C12:** derived CEO effort lessons keep the numbers from the first time they were written. The
     `add()` reinforcement matches the old text, so the numbers go stale.
   - **ResourceWarnings:** Codex chat turns leave their stdout/stderr pipe objects to the GC
     (`chat.py`, CodexSession._run_turn). Harmless in CPython, but tidy it: close `proc.stdout` after the
     loop, and have the stderr reader close its pipe.
   - **Windows-only code was verified by reading only:** crew.cmd, launcher, set_start_with_windows, and
     taskkill. Say so in the final report.
6. **Release (only with the owner's go-ahead):**
   - Bump `crew/VERSION.json` to 2.3.1 with plain-language `notes` (the owner reads them in the update
     banner).
   - Merge into `Crew-AI`.
   - Rehearse the installed updater against GitHub: download, install, restart, reconnect. See
     `updater.install()` and `restart_later()`.

## 5. Everything that was fixed (short form; the ledger has symptom, root cause, fix and test)

**Engine (`crewlib`):**

- **C1 lessons:** seed lessons were re-added (reinforced) at every update, because their version was the
  file's date. They now use a content hash, and `_seed()` never reinforces.
- **C2 lessons:** an unknown effort level in the record crashed `effort_stats`. Unknown levels now rank last.
- **C3 orchestrator:** a damaged or locked cost memo failed the project. `lessons_cost_model()` now returns
  `{}`.
- **C4 orchestrator:** a failing retrospective or cost model turned a delivery into "failed". Both are now
  guarded.
- **C5 agents:** a Codex turn exception left the seat busy forever. It now emits an error result.
- **C6 agents:** one odd stream-json message killed the Claude reader. The reader is now guarded per
  message.
- **C7 gitops:** checks ran through pipes, so a check that left a server running hung the merge. Checks now
  write to a temp file in their own process group, and the whole group is killed afterwards.
- **C8 connections:** a wrong-shape `connections.json` crashed Claude starts. It is now shape-checked, and
  a damaged file is copied aside before a save.
- **C9 cli:**
  - `LATEST` is now read and written with an encoding.
  - `setup` and `doctor` use full program paths (`agents.which`) and time limits.
  - The `taskkill` in `_kill_tree` got a timeout.
- **C10 mcp_server and devices_mcp:** a JSON line that isn't an object is now answered with "invalid
  request" instead of crashing.
- **C11 claude_cli:** a damaged state file no longer blocks the daily Claude Code update.
- **C13 web.py (security):** the terminal live view accepted `/api/say` and `/api/stop` from any page, so a
  site could inject "owner" words into a running team. It now requires the `X-Crew` header and a local
  Host.

**App (`crewapp`):**

- **A1 settings:** a damaged or refused `crew.toml` stopped Crew from opening. Now:
  - The bad file is set aside as `crew.toml.damaged-<date>`.
  - The last good copy (`.bak`) or the defaults are used.
  - What happened is recorded in `settings-problem.json` and shown as a toast and a Settings card.
- **A2/A3 server:** bodies that aren't objects, and fields of the wrong type, now get a 400 with plain
  words instead of a 500.
- **A4 runs and orchestrator:** a heartbeat thread in the orchestrator writes `alive` to the store every
  15 s. The app and `crew resume` refuse to start a project that is still alive, so there is never a second
  orchestrator.
- **A5 runs:** the request is passed through `request.txt`, so a request starting with "-" or one longer
  than Windows' command-line limit still starts.
- **A6 runs:** the run-log handle is closed after spawning.
- **A7 cli:** two projects started in the same second got the same folder. Folders are now claimed with an
  exclusive mkdir and numbered.
- **A8 updater:**
  - Every file is first staged as `name.new`, then swapped in with the old one kept as `name.old`.
  - If any swap fails (a locked file on Windows), everything rolls back.
  - Other protections:
    - A pip failure is never fatal.
    - Every failure is a plain `UpdateError` (a ValueError, so the API returns 400).
    - Only one install can run at a time.
    - `_leftovers()` tidies up after an interrupted update.
    - Files are swapped in sorted order.
- **A10 server:** start-with-Windows and the icon refresh now quote paths for PowerShell (`launcher._ps`)
  and check their results. Windows-only; verified by reading.
- **A11 upkeep:** each step is guarded separately, so one failure no longer skips the rest.
- **A12:** child output is decoded as UTF-8 with replace (Windows).
- **A13 launcher:** a damaged `running.json` no longer breaks startup.
- **A14 App init:** a non-object `app.json` or a locked skill pack no longer stops Crew from opening.
- **A15 chat (ChatGPT):**
  - Errors no longer leave the chat "still answering"; they go through `Session._give_up()`.
  - Stop now shows "*Stopped.*".
  - Deleting a chat while it answers leaves nothing behind. This was a real race: the answer thread checked
    the chat existed, the delete ran, then the answer was written. It is fixed with
    `ChatDB.add_message()` (an INSERT…WHERE EXISTS in one SQL step) and by deleting the chat row before its
    messages.
- **A16 chat (Claude):** an exception in `_finish` now goes through `_give_up()` instead of leaving the chat
  busy.
- **A17:** chat search runs on the server (`/api/chats?q=`, LIKE with escaping), so all chats can be found,
  not just the newest 300.
- **A18:** a model of the other product (gpt-… in a Claude chat, or the reverse) is refused plainly before
  anything is recorded.
- **A19 workflows:** a deleted chat ends its workflow run instead of leaving it "running" for up to 6 hours.
- **A20 workflows:** schedules and the ban list are validated when a workflow is saved, not when it runs.
- **A21 skills:** the description is written as a quoted string, so ": " and " #" no longer break it. Urdu
  names get a hashed id.
- **A22 phone:** a missing adb or a timeout becomes a `PhoneError` with plain words.
- **A23:** secrets pasted with a line break are stripped or refused.
- **A24:** Windows device names (CON, NUL, COM1…) in uploads get a prefix.
- **A25 server:** `GET /api/skills/anthropic` was shadowed by `GET /api/skills/<id>`, so the "Add
  Anthropic's skills" progress, done message and errors never showed. Its routes are now registered first,
  and "anthropic" is reserved as a created skill's id.
- **A26 server:** the sign-in route didn't accept names with @ or +, which Settings allows. The pattern was
  widened.
- **A27 browser:** `localhost:5173` in the address bar did a Google search, and local addresses were opened
  over https. `browser.normalize_address()` now handles both.

**Front end:**

- **F1:** a spoken conversation started on the start screen now hands its handlers to the chat page
  (`store.pendingSend.voice`).
- **F2:** if Crew restarts mid-answer, the chat page ends the wait with a plain note (`resync`).
- **F3:** untitled chats show as "Untitled chat".
- **F4:** Settings lists roll back when a save is refused.
- **F5:** the workflow history dialog closes through its own close (no leaked Escape listener).
- **F6:** `crew.cmd` keeps Crew's exit code (`exit /b` with no number).
- **Also:**
  - The update overlay closes when an automatic update fails (`update_failed` event).
  - The settings-problem toast and card.
  - Unused imports removed.

**Small closes from the scans:**

- `__main__` opens its devnull fallback with UTF-8.
- `pid_alive` treats an OverflowError as "not alive".
- The icon record is checked to be an object.

## 6. How I reasoned (so you can keep the same standard)

- **Every fix follows "never lose data, never stop Crew from opening, never leave something waiting
  forever, always explain in plain words".** When a file can't be read, keep it under a dated name and fall
  back. When a thread might die, catch at its top and report a result. When two actors race, make the check
  and the write one step (SQL `WHERE EXISTS`, exclusive mkdir, a lock).
- **Tests reach the real code paths.**
  - They drive the real server (`AppServer`) with the scripted fakes when possible.
  - They simulate Windows failures by patching (a `PermissionError` on `os.replace` for a locked file,
    `TimeoutExpired` for pip).
  - They avoid global patches of `time.sleep`.
- **Things that can only be verified on Windows are marked "(by reading)" in the ledger.** Don't claim more.
- **The owner reads the messages.** Keep them plain, specific and actionable, like the existing ones.
- **What surprised me:**
  - Route shadowing (A25).
  - The delete race (A15), which the new test found on its first run.
  - The terminal live view's missing CSRF check (C13).

  Expect more of the same kinds: look for ordering assumptions, check-then-act races, and local servers
  without origin checks.

## 7. Lint noise already triaged (don't "fix" these without a reason)

- **ruff:**
  - `RUF100`: the `noqa` markers are for rules outside this rule set.
  - `PLW1510`: those `subprocess.run` calls check the return code on purpose.
  - `RUF005`, `RUF012` (constant class tables), `RUF059`, `PLW2901`: style only.
  - `B007` in gitops, scorecard and updater: the loop variables are unused on purpose.
  - `B905` `zip("ab", versions)`: `versions` always has two items.
  - `RUF021` in server `_probe_login`: the precedence is intended.
  - `S108`: `/tmp` is only a fallback when the Crew folder can't be written.
- **eslint:** unused `catch (e)` names and unused arguments are ignored. The iframe reload was rewritten as
  `f.setAttribute('src', f.src)`.

## 8. Commands

```sh
cd crew
python3 -m unittest discover -s tests                 # full suite (~4 min)
python3 -m unittest tests.test_campaign               # the campaign's regression tests (~15 s)
ruff check --select F,E9,B,PLE,PLW,RUF,S602,S604,S605,S307,S301,S506,S108 \
  --ignore RUF001,RUF002,RUF003,RUF100,PLW1510,RUF005 crewlib crewapp
cd crewapp/static && for f in app.js sw.js $(find js -name '*.js'); do node --check "$f"; done
```

The eslint config I used is the brief's bug rules plus browser globals. Recreate it as a flat config with
`no-unused-vars: ["error", {caughtErrors: "none", args: "none"}]`.

Commit rules: small commits with plain messages. Push to your working branch, never to `Crew-AI` without
the owner's go-ahead. Never commit secrets. Keep `DEBUG-CAMPAIGN.md` section 0 and the ledger (section 12)
up to date after every fix; that file is the campaign's memory.
