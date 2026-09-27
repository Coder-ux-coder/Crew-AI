# Crew — your AI team, on your own computer

Crew turns your subscriptions (several Claude accounts and a ChatGPT account)
into one workspace that looks and works like Claude. Chat with **Claude** or
**ChatGPT** on their own, or hand a bigger job to the **Team**: a manager plans,
builders work in parallel, every piece is checked with fresh eyes, a CEO model
approves, and you get the finished result. You never see code or technical
screens unless you ask.

- **Three tiers, each model doing what it is best at:**

  | Tier | Model | Does | Share of tokens (target) |
  |---|---|---|---|
  | Workhorse | GPT-6 Sol | Routine, fully specified work — research, text and styling changes, small design tweaks, repetitive edits. Most tasks by count. | the rest (about 25–35%) |
  | Manager | Claude Opus 5.5 | Plans, checks every workhorse task, builds what needs high intelligence — security, design plans, shared foundations, hard problems. | 60–70% |
  | CEO | GPT-6 Astra | Used sparingly: reviews the plan (each task's tier and effort) and gives the final approval. Checks rather than builds. | about 5% |

  Each project shows how its tokens were actually shared against these targets.
  Without a ChatGPT subscription the managers do everything and Claude Fable
  5.1 stands in as CEO. Haiku, Sonnet, GPT-6 Luna and Terra are never used.
- **Effort chosen for you:** the CEO sets each task's effort at plan review
  and learns, project by project, which effort suits which work; for small
  jobs the manager decides, so the CEO is kept for checking.
- **A model scorecard that steers the work:** every finished piece is scored —
  which model built it, whether it passed the manager's first check, time and
  tokens. **Usage & scorecard → Model scorecard** shows each model's record by
  kind of work. A kind of work the workhorse keeps failing moves up to the
  manager automatically; a routine task that fails its check twice moves up at
  once; where the workhorse does as well as the manager, the lead and the CEO
  are told. Optional **head-to-head** comparisons (Settings → The team, or per
  project) have both models build the same part; a manager compares the two
  versions without knowing which is which and keeps the better one.
- **You choose the subscription when you want to:** each chat has a
  subscription button (Automatic, or e.g. claude-2); a team project can be
  limited to the subscriptions you tick; each workflow can name one.
- **No clock watching:** no default time limit — set a timer per project only
  if you want one.
- **Usage spread out:** work goes to whichever subscription has the most room;
  if one reaches its limit, another carries on — also in the middle of a chat.
- **Keeps itself current:** Claude Code is updated automatically, and Crew
  updates itself with one click, keeping all your data.

## Install on Windows (once, about 10 minutes)

1. **Download** this project from GitHub (green **Code** button →
   **Download ZIP**), then right-click the ZIP → **Extract All**.
2. In the extracted folder open **crew → install** and double-click
   **Install Crew.cmd**. If Windows warns that the publisher is unknown, choose
   **Run** (or **More info → Run anyway**).
3. Answer two questions (whether you use ChatGPT too, and whether Crew should
   start with Windows). If Windows asks for permission to install Python or
   Git, choose **Yes**.
4. Crew opens by itself and puts a **Crew** icon on your desktop. Sign in to
   your Claude account in **Settings → Subscriptions**; add your other
   subscriptions there too — each one signs in once.

**Already have an older Crew?** Run the new **Install Crew.cmd** once, the same
way. It installs over the old version and keeps everything: chats, projects,
sign-ins, API keys, workflows and settings. **From then on Crew updates itself:**
when a new version is ready, a banner says so — press **Update now** (or
**Settings → Updates & check-up**). Nothing to download, reinstall or re-enter.

To remove Crew, run **Uninstall Crew.cmd**.

## What you can do

| Screen | What it does |
|---|---|
| **New chat** | One box, like Claude. Choose **Claude**, **ChatGPT** or the **Team**; pick the model and the effort (named exactly as Anthropic and OpenAI name them: Claude *auto, low, medium, high, xhigh, max*; ChatGPT (GPT-6 Sol or Astra) *auto, low, medium, high, xhigh, max, ultra*). **Plan** mode: it plans first and changes nothing until you press **Approve**. Type **/** for commands (/compact, /context, /usage, /plan, skills such as /docx or /xlsx). The ring shows how full the conversation's context is. |
| **Chats** | Answers stream in with everything Claude Code shows: thinking, each step it takes, its to-do list, the helpers (sub-agents) it starts, files it makes (they open beside the chat), tokens and time. Attach files or pictures; the microphone types for you; the sound-wave button starts a spoken conversation. |
| **Projects** | The team at work: the team chat (you can write to them), and a panel with every agent and helper — tier, product, model, effort, what it is doing, tokens — plus estimates of time and tokens left, **who did the work** (each tier's share of the tokens against your targets), the plan with each task's tier, and your subscriptions. Optional timer. Stop and continue any time. |
| **Workflows** | Jobs you repeat — a morning briefing, a weekly investor round-up, a letter in your style — run with one click or on a schedule, by Claude, ChatGPT or the team. |
| **Library** | Files made for you in chats, and your screenshots and recordings (draw on them, or ask Claude about one). |
| **Skills** | Anthropic's official skills (Word, Excel, PowerPoint, PDF, design, writing — installed automatically), Claude Code's built-in ones, and Crew's and your own. |
| **Connections** | Any API key (Hunter.io, Google, OpenAI, …) and connected services (MCP servers), with one-click import from the Claude desktop app. Keys stay on your computer and are hidden from every chat, log and report. |
| **Usage & scorecard** | Each subscription's 5-hour and weekly limits with reset times, tokens per day, and the Claude Code version. The **Model scorecard** tab: each model's first-check pass rate overall and by kind of work, typical time and tokens, head-to-head results, and the routing rules Crew applies. |
| **Browser · Phone · Computer** | A real browser you and Claude share, your Samsung, and your Windows screen — side by side with your work, in a panel you can widen by dragging. |
| **Settings** | The few choices that matter, with technical ones under **Advanced**: subscriptions, models and effort, how the team works, instructions, voice (including Urdu), look (light/dark, colour, book or plain type), phone pairing, lessons learned (the team's, and the CEO's own record of which effort works for what), updates and check-up. |

## Use it on your Samsung

1. On the computer: **Settings → Use on your phone** → switch it on.
2. With the phone on the same Wi-Fi, point its camera at the code and tap the link.
3. In Chrome tap **⋮ → Add to Home screen** for a Crew icon.

Crew keeps running on the computer; the phone is a remote screen for it. The
phone's browser only allows the microphone on secure connections: at your desk,
connect the phone on the **Phone** page and Crew also opens on the phone at a
local, secure address; away from home, install the free **Tailscale** app on
both devices.

## Honest limits

- The Windows installer and the Windows-only parts (desktop icon, windowless
  start, sign-in windows) were written carefully but could not be run on a real
  Windows PC in the build environment, which is Linux. Everything else was
  tested there end to end with a real browser, simulated agents and a
  simulated phone. If a step fails, the installer says which one and how to
  fix it.
- ChatGPT through Codex (GPT-6 Sol and Astra) has not been tried with a real
  ChatGPT sign-in yet: the model names, effort levels and options were read
  from the Codex program itself and tested with a simulated Codex. If GPT-6
  Astra cannot run, the CEO's work passes automatically to Claude Fable 5.1.
- Scheduled workflows run while Crew is running (Settings → General → Start
  Crew with Windows keeps it available).
- Google may refuse sign-in inside automated browsers ("this browser may not be
  secure"). Sign in to Google in your normal browser instead, or use sites
  that do not need it.
- Typing on the phone through the connector supports English letters only; use
  the phone's own keyboard for Urdu. iPhones cannot be controlled this way
  (Apple does not allow it); Android phones such as Samsung can.
- Anthropic's Pro and Max plans assume ordinary, individual use. Use only your
  own subscriptions and never share sign-ins.

## For the technically curious: the command line

Everything the app does is also available as commands (`./crew` on macOS/Linux,
`crew` on Windows):

| You want to… | Type |
|---|---|
| open the app | `crew app` (`--phone` also allows paired phones) |
| start something new | `crew start "a website for my bakery with a menu and an order form"` |
| work on an existing folder | `crew start --repo path/to/folder "what to change"` |
| tell the team something | `crew say "use green as the main colour"` |
| see progress | `crew status` or `crew chat -f` |
| pause, then continue later | `crew stop` … `crew resume` |
| read the final report | `crew report` |
| see what past teams learned | `crew lessons` |
| check the installation | `crew doctor` |

Settings live in `~/.crew/crew.toml` (the app edits this file for you), team
rules in `~/.crew/team_rules.md`, assistant instructions in
`~/.crew/assistant.md`, API keys in `~/.crew/secrets.env` (blanked out of every
chat, log and report).

### How the team avoids the usual multi-agent problems

- **No freezing:** a watchdog restarts silent agents from where they were; a
  progress ledger forces a re-plan when nothing moves; nobody waits on anybody.
- **No slop:** each piece has written acceptance criteria, must be submitted
  with evidence, passes the tests, and is reviewed by a fresh agent before it is
  merged. Merges that break the tests are undone automatically.
- **No endless arguing:** chat is budgeted, the lead makes binding decisions,
  and disagreements are settled by a test or one ruling from the CEO model.
- **No corruption:** each agent works in its own copy; only the orchestrator
  combines work; everything is saved so a stopped project resumes exactly.
- **Solo or team is chosen for you.** Small jobs, or jobs that do not split
  well, are built by one agent and checked by the others — the fastest way to
  reviewed, high-quality work. Jobs with several independent parts get the full
  team. (On a small test job, one agent took 9 minutes and the full team 34.)

The design and the research behind it are in `ARCHITECTURE.md`.

### Tests

`python3 -m unittest discover -s tests` runs the unit tests, full simulated team
runs (with injected freezes, crashes, limit hits, rejected reviews, merge
conflicts, and stop/resume) and the app's own tests.
