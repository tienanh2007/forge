---
name: adopt-project
description: Use when the user wants to bring an already-ongoing piece of work (existing ClickUp ticket with subtasks, open PRs, running Claude sessions) under forge management ("adopt ENG-100 into forge", "import this project", "track my existing PRs in forge").
---

# forge: adopt an ongoing project

You act as coordinator. Nothing is created until the user approves the mapping.

## Checklist

1. Gather: ClickUp parent ticket, repo path(s), GitHub `owner/repo`, Sonar key, slug.
2. Scan:
   ```bash
   forge adopt-scan --clickup-parent <ID> --repo <PATH> --github <O/R> --json
   ```
   Read the result (ClickUp subtasks with status, matching branches/PRs, CI state). Also list live
   sessions: `forge sessions --json` and ListAgents.
3. **Propose a mapping table to the user:**

   | ClickUp subtask | Task dir | PRs (stack order) | CI/Sonar | Session to adopt | Proposed state |
   |---|---|---|---|---|---|

   State rules: no PR -> `scoped`; open PR with failing/pending checks -> `in-progress`; all PRs green
   and awaiting review -> `in-review`; all merged -> `done`; ClickUp closed with no work -> `cancelled`.
   Flag PRs that match no subtask and subtasks that look too big (see `forge:split` sizing).
4. On approval, create:
   ```bash
   forge init-project <slug> --title "<T>" --clickup-parent <ID> --repo <PATH> --github <O/R> --sonar-key <K>
   forge new-task <slug> <CUSTOM_ID>-<slug> --title "<subtask name>" [--depends-on ...]
   forge set <task-key> --merge '{"clickup":{"id":"..","custom_id":"..","url":".."}}'
   forge prs add <task-key> --url <PR url> --stack-index <n>
   forge prs refresh <slug>
   ```
5. **Review pass (mandatory):** dispatch one subagent per task **in parallel** (Agent tool, one
   message) with: `Follow the forge:review-task skill (<plugin root>/skills/review-task/SKILL.md) for
   task <key>. Read-only on GitHub/ClickUp/CI; write only via the forge CLI.` Each reviewer drafts the
   task's `spec.md` + tests and raises real issues (CI root causes, unresolved threads, Sonar, spec
   gaps). Then draft the project `spec.md`/`tests.md` from the task specs. Summarise the issues
   (`forge tree <slug>`, UI Issues view) and **show drafted specs to the user for confirmation**.
6. Map existing sessions: for each session the user confirms owns a task,
   `forge set <task-key> --merge '{"session":{"id":"<uuid>","name":"<name>","worktree":"<worktree or null>"}}'`
   so it becomes the owner (future `forge message`/relay goes to it). Then send it (SendMessage) a
   note: `You are now the forge worker for <key>; task folder <path>. Invoke forge:work and continue.`
   Tasks without a session stay unassigned for a fresh `forge:dispatch`.
7. Set states: `forge set-state <key> <state> --note "adopted"`; record yourself as coordinator
   (`forge set <slug> --merge '{"coordinator_session":{...}}'`), `state` `active`.
8. Report: the tree (`forge tree <slug>`), what still needs specs/tests, and what can be dispatched.
