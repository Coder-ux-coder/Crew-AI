# Crew — your AI team, on your own computer

Crew turns your Claude and ChatGPT subscriptions into one workspace. Chat with
**Claude** or **ChatGPT** on their own, or hand a bigger job to the **Team**: a
manager plans, builders work in parallel, every piece is checked, and you get
the finished result.

Everything is in the [`crew`](crew) folder. Start with
[`crew/README.md`](crew/README.md): how to install it on Windows, what each
screen does, and its honest limits.

## Latest work (not released yet)

On branch `claude/jolly-wright-lz430a`, waiting for the owner's go-ahead to release as 2.3.1:

- **Sonnet 5.5 is now the team's workhorse** (it replaces GPT-6 Sol, which leaves Crew). Opus 5.5 manages and
  checks, GPT-6 Astra is the CEO. Your settings move over by themselves; nothing else changes.
- **Bug-hunting campaign, rounds 6, 8 and 10:** 17 more bugs fixed in the team engine and the app, among them a
  security hole in how answers were displayed, a double press of Enter starting the team twice, and forms that lost
  what you typed. The full record is in [`DEBUG-CAMPAIGN.md`](DEBUG-CAMPAIGN.md); the handover notes in
  [`CODEX-HANDOFF.md`](CODEX-HANDOFF.md).
