# Crew — your AI team, on your own computer

Crew turns your Claude and ChatGPT subscriptions into one workspace. Chat with
**Claude** or **ChatGPT** on their own, or hand a bigger job to the **Team**: a
manager plans, builders work in parallel, every piece is checked, and you get
the finished result.

Everything is in the [`crew`](crew) folder. Start with
[`crew/README.md`](crew/README.md): how to install it on Windows, what each
screen does, and its honest limits.

## Latest work (not released yet)

On branch `claude/jolly-wright-lz430a`, waiting for the owner's go-ahead to release as 2.3.1.

### What was done

1. **Sonnet 5.5 is the team's workhorse** (it replaces GPT-6 Sol, which leaves Crew). Opus 5.5 manages and
   checks, GPT-6 Astra is the CEO, Fable 5.1 its backup. The workhorse agents run on the Claude subscriptions; the
   ChatGPT subscription runs the CEO and ChatGPT chats. Saved settings, chats and workflows move over by themselves.
2. **Bug-hunting campaign, round 6 (the team engine):** 7 bugs fixed (C24–C30), e.g. a reviewer sent back to a
   subscription that had just run out, a task stuck waiting for a seat that could not take it, the project's own
   git hooks refusing every merge, a settings file saved by Notepad treated as damaged.
3. **Round 8 (every screen of the app):** 10 bugs fixed (F10–F19), e.g. a security hole that let a crafted link
   in an answer run code inside Crew, a double press of Enter starting the team twice, forms that lost what was
   typed (including a pasted API key), a double-click on the computer screen arriving as four clicks.
4. **Round 10 (Windows paths, by reading):** launcher, installer, start-up; one fix (C30).
5. Every fix has a test that fails without it; GitHub runs the checks on every push to this branch.

### What is left

1. **Run the full test suite once more** (the last complete run passed 216 tests; the run after the screen fixes
   was interrupted). Each of those fixes passed its own tests.
2. **Release 2.3.1 — only with the owner's go-ahead:** version number and plain release notes, publish to the
   `Crew-AI` branch, rehearse the one-click update (download, install, restart, reconnect).
3. **On the owner's Windows PC after the update** (could not be run here): open Crew from the desktop icon; start
   one small team project and check that the Sonnet 5.5 agents start; try the Computer page's double-click.
4. **One open question:** if a subscription reaches a limit that applies only to Opus, Crew pauses the whole
   subscription, and with it the Sonnet agents on it. Deciding this needs one real limit message from the owner's
   subscription.
5. **The campaign's last step:** a full clean round (the remaining checklists of rounds 3, 4, 5, 7 and 9, then
   every check once more until nothing new is found).

The full record is in [`DEBUG-CAMPAIGN.md`](DEBUG-CAMPAIGN.md); the handover notes in
[`CODEX-HANDOFF.md`](CODEX-HANDOFF.md).
