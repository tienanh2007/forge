# forge UI

Local dashboard over `$FORGE_HOME` — stdlib Python server + vanilla JS SPA (no build step).

```bash
python3 ui/server.py [--port 7777] [--no-poll] [--no-nudge]   # or: forge ui
python3 ui/server.py --fixtures      # builds a fixture FORGE_HOME in a temp dir (implies --no-poll --no-nudge)
python3 ui/dev_fixtures.py [dir]     # just build the fixture home
```

Binds `127.0.0.1` only. Uses `forge_lib` when importable; otherwise falls back to the read-only
`_fsread.py` (inbox appends only; PR refresh returns 503). A background thread refreshes PR status
every 120s (`forge_lib.prs.refresh`) and ClickUp parent summaries every 300s (cached in memory and
`$FORGE_HOME/.cache/clickup.json` as `{"schema":1,"parents":{"<slug>":{"summary","fetched_at"}}}`).

| Endpoint | Returns |
|---|---|
| `GET /api/projects` | project.json + `counts`, `task_states`, `clickup` |
| `GET /api/tree?key=K` | tree node (all projects when `key` omitted); nodes carry `depends_on` |
| `GET /api/project?slug=S` | `{project, tree, clickup, clickup_fetched_at, active_sessions}` |
| `GET /api/task?key=K` | `{task, tests, prs, issues, inbox, spec_md, log_tail, tree, active_sessions}` |
| `GET /api/prs?project=S` | `[{task_key, task_title, task_state, project, health, ...pr}]` |
| `GET /api/issues?project=&status=&severity=` | `[{task_key, task_title, project, ...issue}]` |
| `GET /api/issue?key=K&id=I-1` | `{issue, body_md, thread, task}` |
| `POST /api/inbox {key, body, issue_id?}` | created msg + `nudge` result |
| `POST /api/refresh {key?}` | `{ok, key, seconds, at}` (502 on integration failure) |

**Nudge:** after `POST /api/inbox`, if the task has a session that `claude agents --json` does not list
as active, the server runs `forge message <key> <text>` and marks the message delivered. Active or
unknown sessions are left `delivered=false` for the coordinator relay.

POSTs require `Content-Type: application/json` and reject non-localhost `Origin`s. Keys are validated
per segment and must resolve inside `FORGE_HOME`. Set `FORGE_UI_ACCESS_LOG=1` for request logs.
