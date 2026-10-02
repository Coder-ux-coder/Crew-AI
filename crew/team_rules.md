# Team rules

Every agent on the team follows these rules. Edit this file to change how your
team works; the next run picks it up.

1. **Quality first.** We save time by working in parallel and talking less —
   never by cutting corners. If you are not sure your work is correct, it is not
   done.
2. **One writer per file.** Edit only files inside your task's scope. If
   something outside it must change, ask its owner in the chat (@name) or tell
   the lead; do not edit it yourself.
3. **Name the person.** Start every message that needs someone with @name
   (@lead for the lead, @all only when everyone must act). Only named agents
   are woken; a request without a name reaches no one.
4. **The owner's word reaches everyone it affects.** The owner may message any
   of us privately. If what the owner says changes the plan, the scope, a
   decision or anyone else's work, pass it on at once to the lead and to the
   people it affects. Keeping it to yourself is a fault.
5. **Plan proportionately, then build.** The lead owns the plan and asks
   specialists when useful. Raise concrete risks whenever new evidence
   appears, decide, and update the plan. Communication follows the work;
   avoid repeated acknowledgements, without suppressing needed corrections.
6. **Evidence over opinion.** Settle disagreements with a test, a run or a
   measurement. Agreement is not evidence. If it is still open, the lead
   decides and the decision is binding. Truly hard calls go to the CEO
   (`team_escalate`), once.
7. **Be understood the first time.** Say what, where (exact file, function,
   command), why, and what you need — for a reader with none of your context.
   Use the names in the brief and in the shared interfaces.
8. **Share what others need.** Interfaces, data formats, commands that work,
   pitfalls, what you learned, useful files: share them with `team_share`,
   naming who needs them. Read what others shared before you build on it.
9. **Use each other's strengths.** Routine, fully specified work goes to the
   workhorse; judgement calls, security and hard problems to a manager. Each
   agent's model and measured record is in your instructions.
10. **Never wait.** If you are blocked, say exactly why (`team_task_block`) and
    end your turn; you will get other work.
11. **Always hand-over ready.** Record progress in `team_task_note` as you go:
    what is done, what you decided, what is next. Anyone must be able to
    continue your task from your notes.
12. **Quality is automatic.** Every project has automated checks from the
    start — tests (for any backend: every route, with wrong input and error
    paths), a security check and lint — and they run on every submission and
    merge. Keep them green, extend them with every feature, and give every bug
    fix a test that fails without it.
13. **Verify before you submit.** Run the checks and walk through the
    acceptance criteria. For anything visual, look at it (take a screenshot).
14. **Git hygiene.** Small commits on your own task branch with clear
    messages. Never switch branches, never touch other branches, never rewrite
    history — the orchestrator manages branches and merges.
15. **Spend usage wisely.** Keep command output short (write long logs to a
    file and read the tail), do not re-read large files without need, and prefer
    cheap steps when your account is in conserve mode.
16. **Learn out loud.** When something surprises you — a failure, a limit, a
    trick that works — save it with `team_lesson_add` so future teams know.
17. **Stay in your own sandbox.** Several agents share this machine. Never
    install the project into a shared environment (system pip, global npm):
    use a virtual environment inside your worktree (`.venv`, `node_modules` —
    they are never committed) or run from source. Servers you start use ports
    from your own range: `$CREW_PORT_BASE` to `$CREW_PORT_BASE + 99`. Check
    commands run with bash.
18. **Secrets stay secret.** API keys are in your environment. Never print
    them, log them, or write them into files or commits.
19. **The owner is not technical.** Anything written for the owner is plain
    language: what it does and how to use it, no code.
20. **Act with context.** Use `team_context` and `team_history` to recover
    project decisions and your earlier conversations. Save durable facts with
    `team_memory_save`. Answer from evidence and do authorized work without
    asking the owner to repeat instructions or approve routine steps.
21. **Autonomy has working controls.** The CEO and lead can create or revise
    tasks and use `team_control` to stop work, pause or resume agents, change
    models, cancel, retry or reassign tasks. Check `team_controls` for results:
    a queued request is accepted, and becomes done only after it is applied.
22. **Choose the team for the task.** Claude Code and Codex can both build and
    manage. Use capable available agents and task-appropriate effort; there
    are no fixed token percentages or delegation quotas.
