---
name: clickup-task
description: Use when a forge task needs its ClickUp subtask created under the parent ticket, or a ClickUp comment posted on a state transition (started, in review, blocked, handed back, done). forge itself syncs ClickUp status.
---

# forge: ClickUp subtask

Prefer the ClickUp MCP tools (`mcp__clickup__*`; load them with ToolSearch if deferred). Fallback:
ClickUp REST v2 with `Authorization: $CLICKUP_TOKEN` (from env or `~/.config/forge/env`) - never
print or store the token.

## Create the subtask (once per task)

1. `forge show <key> --json`. If `clickup` is already set, stop.
2. Find the parent ticket: the parent task's `clickup` (if `parent_key` is set and has one), else the
   project's `clickup_parent` (`cat "$(forge path <slug>)/project.json"`). If neither exists, ask the coordinator/user
   whether to proceed without ClickUp; log the answer.
3. `mcp__clickup__clickup_get_task` on the parent -> take its `list.id` (and `custom_id` for context).
4. Create: `mcp__clickup__clickup_create_task` with `list_id`, `parent=<parent id>`,
   `name="<task title>"`, description in markdown:
   - 3-6 line summary of `spec.md` (scope + acceptance criteria).
   - `forge task: <key>` and the task folder path.
   - Depends on: the depends_on keys (and their ClickUp links if known).
   Assign to the user if the parent is assigned to them.
   Fallback: `POST https://api.clickup.com/api/v2/list/<list_id>/task` body
   `{"name":...,"description":...,"parent":"<parent id>"}`.
5. Record it (this is what enables forge's automatic status sync):
   ```bash
   forge clickup link <key> <id-or-url>
   ```
   `custom_id` may be null for workspaces without custom ids - then use `T<n>` in branch names.
6. Log to `log.md`: `ClickUp subtask <custom_id> <url>`.

## Status updates are automatic

Do not set ClickUp status yourself. Every `forge set-state` / `forge handback` pushes the mapped
status to the linked task (`clickup.id`) and records the outcome as `clickup_sync` on task.json; a
failure is logged to `log.md` and never blocks the transition. The map is the project's
`clickup_status_map` (default: scoped->planned, dispatched/in-progress/coordinating->in progress,
blocked->blocked, in-review/handed-back->review, done->done, cancelled->cancelled).
If `clickup_sync.error` is set, tell the coordinator; `forge clickup sync <key>` re-pushes.

## Comments on transitions

On a real transition (started, in review, blocked, handed back, done) post a one-paragraph
`mcp__clickup__clickup_create_task_comment` (PR links, summary, or the blocking issue title). Don't
comment on every minor change.

When mentioning a ClickUp task to the user, write it as a markdown link with the task name as text.
