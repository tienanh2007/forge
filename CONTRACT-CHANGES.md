
## plugin agent
- `forge:relay` keeps `$FORGE_HOME/.cache/relay-notified.json` (`{"<task key>":"<state>"}`) to avoid
  re-announcing handed-back/blocked tasks each loop. Cache-only; safe to delete. No CLI change needed.
- Skills read project fields via `cat "$(forge path <slug>)/project.json"` rather than assuming
  `forge show <slug>` supports project slugs.

## lib agent (forge_lib / bin/forge)

Additive or clarifying; every contract shape still works.

- **prs.json `status.error`**: if a GitHub/Sonar fetch fails, `status` keeps the previous fields, adds
  `"error": "<message>"`, and updates `fetched_at`. The gate fails that PR with `status fetch failed: ...`.
- **`forge prs refresh`** skips PRs whose last known state is MERGED/CLOSED unless you pass `--all`. In Python
  that's `prs.refresh(key_or_project=None, include_closed=False)`. The gate always refreshes every PR of its task.
  `prs.problems(item, sonar_required)` and `prs.is_green(...)` hold the per-PR gate logic (the tree counts use them).
- **Gate CI rule**: rollup `NONE` (no checks reported) fails with the reason "no CI checks reported".
- **dispatch**: the dependency and `max_parallel` refusals apply only to a *first* dispatch. Re-dispatch and
  `forge message` skip them, so a blocked worker can always be resumed. Re-dispatch sets state `dispatched`
  unless the task is `in-progress`, `in-review` or `coordinating`. "Active" means the session id is listed
  by `claude agents --json` (no `--all`) with a state/status other than completed/done/failed/stopped/exited.
  If `claude agents` fails, the session counts as inactive.
- **`forge message`** also accepts `--dry-run`, and exits 1 if the task was never dispatched.
- **Tree nodes** have an extra `depends_on` field. A project node's `counts` are totals over all its tasks,
  and its `session` is `coordinator_session`. A task node's counts cover that task only.
- **`inbox ack` / `inbox resolve`** also set `delivered=true`.
- **`hook session-start`** also briefs a session whose id matches a `project.coordinator_session.id`
  (it prints a tree summary).
- **`init-project`**: when `--github` is omitted, it is read from the repo's `origin` remote.
  When `CLICKUP_TOKEN` is set, `--clickup-parent` is resolved through the ClickUp API; otherwise the id,
  custom id or URL is stored as given.
- **`new-task --depends-on`** with a key that doesn't exist only warns on stderr; that task is simply not ready.
- **`FORGE_ENV_FILE`** env var overrides the `~/.config/forge/env` path (used by tests).
- **Exit codes**: 0 ok, 1 error/refused/gate fail/not found, 3 dispatch target is ACTIVE, 2 argparse usage error.
- **adopt-scan output**: `{"schema":1,"project":{slug,title,clickup_parent,repos,prs,candidate_sessions},
  "tasks":[{task_dir,title,clickup,clickup_status,prs,candidate_sessions}],"sessions":[...all claude agents...],
  "warnings":[...],"generated":ts}`. Candidate sessions are agents whose name or cwd contains the ClickUp custom
  id (case-insensitive). Only one level of subtasks is scanned.

## ui agent
- Additive response fields only: `/api/projects` items add `counts` (aggregated), `task_states`, `clickup`;
  `/api/project` adds `clickup_fetched_at`, `active_sessions` (null when unknown); `/api/task` adds `tree`,
  `active_sessions`; `/api/prs` items add `task_title`, `task_state`, `project`, `health`
  (`green|failing|pending`); `/api/issues` items add `task_title`, `project`; `/api/issue` adds `task`;
  `POST /api/inbox` response adds `nudge: {attempted, delivered?, reason}`.
- Tree nodes returned by the UI API are enriched with `depends_on` (from task.json) when the lib omits it.
- UI cache file `$FORGE_HOME/.cache/clickup.json` = `{"schema":1,"parents":{"<slug>":{"summary","fetched_at"}}}`.
- `server.py` extra flags: `--fixtures` (temp fixture home; implies no poll/no nudge), `--no-nudge`.

## agents (watch + message sessions)

- **`max_parallel` default is 5** (was 3): `init-project --max-parallel`, `tasks.DEFAULT_MAX_PARALLEL`.
- **`forge_lib/agents.py`** (new): `transcript_path(session_id, cwd=None)` (`cwd` may be a list of candidates),
  `transcript_tail(session_id, cwd=None, limit=150) -> [{at, role: user|assistant|cross|system,
  kind: text|tool_use|tool_result, text, tool, is_error, sender?}]` (tool_result entries carry role `assistant`;
  `sender` = cross-session `from-name`; `isMeta` rows are kept only for cross-session messages and Stop hook
  feedback), `send_live(name, text, cwd) -> (ok, detail)`, `send(key, text) -> {mode: sent|resumed|queued|failed,
  delivered, detail, message}`, `overview(project=None)`, `row(key, live_agents)`, `detail(key, limit)`,
  `live_agents()` (None when `claude agents` fails).
- `send` on a task with no session raises `UsageError` **before** recording anything (nothing to deliver to).
  `send` on a project slug messages the live coordinator (no inbox; `mode` `failed` if the bridge fails).
- `FORGE_CLAUDE_PROJECTS_DIR` overrides `~/.claude/projects` (tests).
- **CLI**: `forge send <key> <text>` (exit 0 delivered, 1 queued/failed/not dispatched);
  `forge agents [<project>] [--json]`.
- **`util.run`** no longer lets children inherit stdin (`claude --bg` was appending the caller's piped stdin
  to the worker prompt).
- **UI API**: `GET /api/agents?project=S`, `GET /api/agent?key=K&limit=N` (row + `entries`),
  `POST /api/agent/message {key,text}`, `POST /api/agent/open {key}` (409 unless a background session with a
  bg id), `POST /api/agent/dispatch {key}` (409 if the task already has a session). `POST /api/inbox` now nudges
  a live session through the bridge instead of leaving it for relay. Views `#/agents` and `#/agent/<key>`.

## rendered status + files view

- **New rendered files**: task `status.md`, task `HANDBACK.md`, project `status.md` (see the layout in
  CONTRACT.md). `PRs.md` gains a `Problems` column (`prs.problems` with the task's `required_checks`, sonar
  key and the PR below in the stack).
- **Re-render triggers**: `state.save` and `state.save_json_items` call `render.on_change(key)`, which re-renders
  the task's `PRs.md` + `status.md`, every ancestor's `status.md` and the project `status.md`. So gate, prs
  refresh, set-state, set --merge, tests/issues/inbox, handback and dispatch all refresh them. A failure there
  only warns on stderr; the json write already happened.
- **`task.handback.gate`**: `forge handback` stores `{passed, reasons, last_run}` of the passing gate so
  `HANDBACK.md` shows the gate at handback time even after later gate runs.
- **CLI**: `forge render --all`; `forge render` now takes a key or `--all` (exactly one).
- **Python**: `render.write_status(key)`, `render.write_handback(key)`, `render.refresh_status(key)`,
  `render.render_all()`; `render.render_prs(key, items, task=None)`.
- **UI API**: `GET /api/fs/tree`, `GET /api/fs/file` (see CONTRACT.md UI section). View `#/files/<rel>`, linked
  from the nav, the project dashboard (Files tab + status/project/spec/plan links), the task page (Files link and
  per-section links to status.md, HANDBACK.md, spec.md, tests.md, PRs.md, issues.md, log.md) and the issue page.

## issue states + decision log (0.1.5)

- **issues.json** items gain `state`, `history`, `decision` (see CONTRACT.md); `status` stays in sync. Migration
  of old items happens in `state.load_json_items(key, "issues")` (in memory) and `issues.migrate_file(key)`
  (called by `forge render`, so `forge render --all` persists it).
- **Python**: `issues.set_state(key, id, state, note="", by="worker")`, `issues.decide(key, id, text, by="user",
  comment_id=None, notify=None, deliver=True) -> {issue, delivery}`, `issues.resolve(..., by="worker")`,
  `issues.add(..., st=None, by="worker")`, `issues.list_issues(key, st=None)`, `issues.next_states(state)`.
  `agents.send(key, text, author="user", issue_id=None)` (new optional args).
- **CLI**: `forge issues set-state|decide|list`; `add --state/--by`, `resolve --by`. `decide --comment-id` means the
  decision came from that inbox message, so by default no new message is posted (`--notify` forces it).
- **Gate** check 5: no issue `decided`/`in-progress`.
- **Rendered**: `issues.md` gains State and Decision columns; project `decisions.md` (new, re-rendered with the
  project `status.md`); task `status.md` gets "Issues by state"; project `status.md` gets "Awaiting you" +
  "Issues" columns and a link to `decisions.md`.
- **Tree counts** gain `awaiting_user` (issues in `awaiting-user`); `/api/projects` counts too.
- **UI**: `/api/issues?state=`, `/api/issue` `next_states`, `POST /api/issue/decide`, `POST /api/issue/state`
  (same localhost-origin + JSON guards as other POSTs). Project dashboard links `decisions.md` (+ Decisions tab).
