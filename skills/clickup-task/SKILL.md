---
name: clickup-task
description: Use when a forge task needs its ClickUp subtask created under the parent ticket, or its ClickUp status/comment updated on a state transition (started, in review, blocked, handed back, done).
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
5. Record it:
   ```bash
   forge set <key> --merge '{"clickup":{"id":"<id>","custom_id":"<custom_id>","url":"<url>"}}'
   ```
   `custom_id` may be null for workspaces without custom ids - then use `T<n>` in branch names.
6. Log to `log.md`: `ClickUp subtask <custom_id> <url>`.

## Status updates on transitions

Statuses differ per list: read the valid ones once (`mcp__clickup__clickup_get_list` -> `statuses`)
and map by closest name:

| forge state | ClickUp status (closest match) |
|---|---|
| in-progress | in progress |
| blocked | blocked (else keep in progress + comment) |
| in-review | in review / code review |
| handed-back | in review (+ comment with handback summary) |
| done | done / complete / closed (coordinator only, after review) |
| cancelled | cancelled / closed |

Use `mcp__clickup__clickup_update_task` with `status`, then `mcp__clickup__clickup_create_task_comment`
for a one-paragraph note (PR links, summary, or the blocking issue title). Don't comment on every
minor change - only on transitions.

When mentioning a ClickUp task to the user, write it as a markdown link with the task name as text.
