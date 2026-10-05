"""Claude Code hook entry points. Callers must never let exceptions escape (see cli)."""
import json

from forge_lib import gate, issues, prs, state, tasks, util

MAX_CONSECUTIVE_BLOCKS = 5
SPEC_HEAD_LINES = 60
LOG_TAIL_LINES = 30


def _set_blocks(key: str, count: int) -> None:
    task = state.load_task(key)
    g = task.setdefault("gate", {})
    if g.get("consecutive_blocks", 0) != count:
        g["consecutive_blocks"] = count
        state.save(key, task)


def _msg_line(m: dict) -> str:
    issue = f", issue {m['issue_id']}" if m.get("issue_id") else ""
    return f"- {m['id']} ({m.get('author')}{issue}): {m.get('body', '').strip()}"


def stop(payload: dict) -> str:
    """Return the hook stdout: a block decision JSON, or '' to allow the stop."""
    key = tasks.task_for_session(payload.get("session_id") or "")
    if not key:
        return ""
    task = state.load_task(key)
    reason = None
    open_msgs = state.open_inbox(key)
    if open_msgs:
        reason = ("You have unacknowledged forge inbox messages. Read and act on them, then "
                  f"`forge inbox ack {key} <id>` (or `forge inbox resolve {key} <id> --reply ...`):\n"
                  + "\n".join(_msg_line(m) for m in open_msgs))
    elif task.get("state") == "in-review":
        result = gate.run(key)
        if not result["passed"]:
            reason = ("Task is in-review but the forge gate fails:\n"
                      + "\n".join(f"- {r}" for r in result["reasons"])
                      + "\nKeep fixing / monitoring CI until it passes. If you need the user, write an issue "
                        f"(`forge issues add {key} ...`) and `forge set-state {key} blocked --note ...`.")
    if not reason:
        _set_blocks(key, 0)
        return ""
    blocks = (state.load_task(key).get("gate") or {}).get("consecutive_blocks", 0)
    if blocks >= MAX_CONSECUTIVE_BLOCKS:
        _set_blocks(key, 0)
        tasks.set_state(key, "blocked", note=f"stop hook blocked {blocks} times in a row; allowing stop. "
                                             + reason.splitlines()[0])
        return ""
    _set_blocks(key, blocks + 1)
    return json.dumps({"decision": "block", "reason": reason})


def _task_briefing(key: str) -> str:
    task = state.load_task(key)
    d = state.task_dir(key)
    out = [f"# forge task {key}", f"State: {task.get('state')} · Task folder: {d}",
           f"Title: {task.get('title')}", f"Repo: {task.get('repo')} ({task.get('github')}), "
                                          f"base {task.get('base_branch')}"]
    spec = util.read_text(d / "spec.md").splitlines()[:SPEC_HEAD_LINES]
    if spec:
        out += ["", "## spec.md (head)", *spec]
    log = util.read_text(d / "log.md").splitlines()[-LOG_TAIL_LINES:]
    if log:
        out += ["", "## log.md (tail)", *log]
    msgs = state.open_inbox(key)
    if msgs:
        out += ["", "## Open inbox (ack or resolve each)", *(_msg_line(m) for m in msgs)]
    open_issues = issues.open_issues(key)
    if open_issues:
        out += ["", "## Open issues", *(f"- {i['id']} [{i['severity']}] {i['title']}" for i in open_issues)]
    pr_items = state.load_json_items(key, "prs")
    if pr_items:
        out += ["", "## PRs", *("- " + prs.summary_line(p) for p in pr_items)]
    g = task.get("gate") or {}
    if g.get("last_run"):
        out += ["", f"## Gate (last run {g['last_run']}): {'PASS' if g.get('passed') else 'FAIL'}",
                *(f"- {r}" for r in g.get("reasons") or [])]
    return "\n".join(out) + "\n"


def _coordinator_briefing(slug: str) -> str:
    def walk(node, depth):
        c = node["counts"]
        yield (f"{'  ' * depth}- {node['key']} [{node['state']}] issues:{c['open_issues']} "
               f"inbox:{c['open_inbox']} prs:{c['prs_green']}/{c['prs']} tests:{c['tests_green']}/{c['tests']}")
        for ch in node["children"]:
            yield from walk(ch, depth + 1)
    node = state.tree(slug)
    return "\n".join([f"# forge project {slug} (you are its coordinator)", f"Folder: {node['path']}",
                      *walk(node, 0)]) + "\n"


def session_start(payload: dict) -> str:
    sid = payload.get("session_id") or ""
    if key := tasks.task_for_session(sid):
        return _task_briefing(key)
    if slug := tasks.project_for_coordinator(sid):
        return _coordinator_briefing(slug)
    return ""
