# Autonomous coordination

The CEO can create tasks, revise active tasks, change checks and make binding decisions.
`team_control` accepts a batch of stop, pause/resume agent, cancel/retry/reassign task,
and change agent model requests. The coordinator applies these between git operations,
stops the writer and checkpoints its branch before changing ownership or scope.
`team_controls` and the project's control receipts show whether requests are queued,
completed or failed. Reviews and merges finish before their task is revised.

Every agent turn gets current project state, roster, task board, decisions, handoffs
and its earlier private conversation with the owner. The CEO gets its previous
questions and answers too. Context is bounded; `team_history` searches and pages the
retained records, `team_context` refreshes state, and `team_memory_save` records shared
facts. Private conversations remain visible to the owner and their recipient.

Choose individual agents in Settings → Models & effort → Agents: subscription, model,
worker/manager tier, and lead/member role. Both Claude Code and Codex can build and
manage. Automatic workers alternate through the compatible worker model pool, including
Sonnet and GPT-6.1 Sol. Models must be available on the chosen CLI and subscription;
Crew does not turn a model name into access to that model. Add custom model IDs in
settings. User bans and the allowed Claude model list still apply.

Usage follows task needs, without fixed token percentages. Chat limits are optional;
zero means adaptive communication. Concerns can be raised when evidence appears in
any phase, and CEO rulings have no default count limit. The CEO can also complete the
plan and request delivery after the board is closed. The prompt writer is optional and off by default.
Managers may take routine work directly when assigned or when workers are unavailable.
Refinement, optional rewriting, judging and leadership recovery use the selected
provider. A new project folder receives its own repository even inside another one.

An excluded optional CEO backup is disabled without resetting the rest of the settings.
The recovery button validates an archived damaged settings file, saves current settings
as a backup, and restores the archived choices while retaining the archive itself.

Chats can switch Claude Code/Codex in the composer and retain their conversation.
The new CLI receives the recorded conversation. Sidebar chat deletion has a visible
button and confirmation. Sending a follow-up to a completed project reopens the
existing board and conversations, including when its previous coordinator is exiting.
Owner connections and device tools are passed to Codex as well as Claude Code; legacy
SSE connections need a compatible HTTP/stdio endpoint for Codex.

Verification covers isolated databases, actual git checkpoints, cancellation of a real
child process, and desktop/phone browser interactions with the real HTTP app. Browser
model calls and project launches are simulated to avoid spending subscriptions or
changing the owner's installation. Full fake-agent orchestration tests run in CI.
Live subscription authentication and every third-party MCP service need a separate
live check. This update does not merge itself, install itself, or change Crew's
own source automatically.
