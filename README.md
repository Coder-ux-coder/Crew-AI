# Crew — your AI team, on your own computer

Crew turns your Claude and ChatGPT subscriptions into one workspace. Chat with
**Claude** or **ChatGPT** on their own, or hand a bigger job to the **Team**: a
manager plans, builders work in parallel, every piece is checked, and you get
the finished result.

Everything is in the [`crew`](crew) folder. Start with
[`crew/README.md`](crew/README.md): how to install it on Windows, what each
screen does, and its honest limits.

## Latest work (not released yet)

On branch `claude/friendly-sagan-bj2ykb` (it carries everything from `claude/jolly-wright-lz430a`), waiting for the
owner's go-ahead to release as 2.3.1.

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
5. **Fifth continuation (every branch read; a clean round begun):** 14 bugs fixed, e.g. on Windows the team's
   checks ran in the wrong shell and could never pass (C32); Urdu answers, messages and titles read left to right
   with English words out of order (F22); the phone tool could tap where a button *used* to be (A65); after an
   update the app could send the owner to another program on a neighbouring port (F21); a chat could keep
   "working…" for ever after a restart or a Wi-Fi drop (F20); file names in Urdu escaped the safety scan (C33);
   behind an office proxy the browser, computer and phone tools failed (A59). Fixes from a parallel session that
   never reached this line are brought over.
6. Every fix has a test that fails without it; the full suite passes (240 tests); GitHub runs the checks on every
   push to this branch.

### What is left

1. **Release 2.3.1 — only with the owner's go-ahead:** version number and plain release notes, publish to the
   `Crew-AI` branch, rehearse the one-click update (download, install, restart, reconnect).
2. **On the owner's Windows PC after the update** (could not be run here): open Crew from the desktop icon; start
   one small team project and check that its checks run (Git's bash) and the Sonnet 5.5 agents start; try the
   Computer page's double-click; write a message in Urdu.
3. **Questions for the owner:** a parallel session recorded "no Urdu" (voice and dictation in Urdu removed), while
   later instructions list Urdu dictation, so nothing was removed here — which is right? And if a subscription
   reaches a limit that applies only to Opus, Crew pauses the whole subscription (and the Sonnet agents on it);
   deciding this needs one real limit message.
4. **The campaign's last step:** carry on the clean round until nothing new is found (the owner asked earlier for
   the campaign to be run three times).

The full record is in [`DEBUG-CAMPAIGN.md`](DEBUG-CAMPAIGN.md); the handover notes in
[`CODEX-HANDOFF.md`](CODEX-HANDOFF.md).
