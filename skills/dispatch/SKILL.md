---
name: dispatch
description: Use when forge tasks are scoped and approved and need worker sessions started or resumed ("dispatch the ready tasks", "start workers", "kick off T3", "resume the worker for ENG-105").
---

# forge: dispatch workers

## Checklist

1. List what can start: `forge ready <project> --json` (scoped tasks whose deps are handed-back/done).
2. Check capacity: `forge tree <project>`; count tasks in `dispatched|in-progress` vs `max_parallel`.
   Only dispatch up to the free slots. Use `--force` only if the user explicitly asks.
3. Preview once when unsure: `forge dispatch <key> --dry-run`.
4. Dispatch each: `forge dispatch <key> [--message "<extra context for the worker>"]`.
   - First dispatch starts `claude --bg -n forge-<key-with-dashes> -w <worktree>` (forge then records the real session id from `claude agents --json`)
     in the task's repo, in its own git worktree, running `forge:work`.
   - Exit 1 = refused (deps not done or at `max_parallel`); report the reason, don't force.
5. Confirm with `forge sessions` and report to the user a table: key, session name, state, and
   how to watch: `claude attach <session-id>` (or open agent view with `claude agents`).
6. Remind the user: the UI (`forge ui`, http://localhost:7777) shows tasks/PRs/issues and takes comments.

## Re-dispatch vs SendMessage

- `forge dispatch <key> --message M` / `forge message <key> M` on a task that already has a session:
  - Session **not active** -> resumes it with `claude --bg --resume <uuid> "<M>"`.
  - Session **active** -> exit code **3**, prints `ACTIVE: use SendMessage to <name>`; the message is
    stored in the task inbox (author coordinator). You must then deliver it live with the
    **SendMessage** tool to `<name>` (e.g. `forge-billing-v2-ENG-101-api`). Never start a second
    session for the same task.
- When a dependency is handed back, run `forge ready` again and dispatch newly unblocked tasks.

## Keep relaying

Suggest the user runs `/loop 5m /forge:relay` in this coordinator session so user comments reach
workers and handbacks get noticed.
