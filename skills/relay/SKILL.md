---
name: relay
description: Use in the forge coordinator session to deliver pending user comments from the forge UI/inbox to worker sessions and surface handed-back or blocked tasks; designed to run on a loop ("/loop 5m /forge:relay", "relay messages to workers").
---

# forge: relay inbox -> workers

Idempotent and quiet: one pass, then stop. Say nothing to the user unless something happened.

Most user messages no longer need you: the UI and `forge send` deliver them directly (live sessions
through a `claude -p` SendMessage bridge, idle ones by resuming) and mark them delivered. What's left in
`forge inbox pending` is only what that path could not deliver (bridge failed, `claude agents` unavailable,
or a comment added with `forge inbox add`).

## Checklist

1. `forge inbox pending --json` - undelivered open user/coordinator messages across all projects.
2. `forge sessions --json` - which task sessions are currently active.
3. For each pending message `(key, C-id, body, issue_id)`:
   - Session **active** -> SendMessage to the session name (`forge-<key with / -> ->`), text:
     `forge inbox <key> <C-id>[ (issue <I-id>)]: <body>. Ack with forge inbox ack, act, then forge inbox resolve --reply.`
   - Session **inactive / none** -> `forge message <key> "New inbox message <C-id>; check inbox."`
     (resumes it; if it exits 3 the session just became active - use SendMessage instead).
   - Task has no session yet (never dispatched) -> leave it; the worker will see it on first start.
   - Then `forge inbox mark-delivered <key> <C-id>`.
4. Surface task changes: `forge tree --json` and collect tasks in `handed-back` or `blocked`.
   Compare with `$(forge home)/.cache/relay-notified.json` (`{"<key>":"<state>"}`, create if missing);
   for each new/changed entry tell the user one line (`ENG-105 handed back - run /forge:review-handback`,
   `T3 blocked: <issue title>`), then update the file.
5. Newly unblocked work: if any task was handed-back/done since last pass, run `forge ready` and tell
   the user which tasks can be dispatched (do not auto-dispatch unless the user said so).
6. Messages sent *to you* by workers via SendMessage (handback summaries, questions) - answer or
   route them: questions for the user become an issue on that task only if the worker has not
   already written one.

## Rules

- Never resolve a user's message on the worker's behalf.
- One pass per invocation; do not sleep or poll inside the skill.
