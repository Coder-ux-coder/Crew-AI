# Crew — your AI team, on your own computer

Crew turns your Claude and ChatGPT subscriptions into one workspace. Chat with
**Claude** or **ChatGPT** on their own, or hand a bigger job to the **Team**: a
manager plans, builders work in parallel, every piece is checked, and you get
the finished result.

Everything is in the [`crew`](crew) folder. Start with
[`crew/README.md`](crew/README.md): how to install it on Windows, what each
screen does, and its honest limits. [`crew/ARCHITECTURE.md`](crew/ARCHITECTURE.md)
explains how it works inside.

Crew updates itself from this branch, `Crew-AI`: the version and what is new in
it are in [`crew/VERSION.json`](crew/VERSION.json). Every change is checked
automatically before it is published — the full test suite (simulated teams,
and the app itself in a real browser) and the code checks
([`.github/workflows/checks.yml`](.github/workflows/checks.yml)).
