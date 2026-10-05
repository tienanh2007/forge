"""Render tests.md / PRs.md / issues.md / status.md / HANDBACK.md from forge json.

Output is deterministic: it only contains timestamps stored in the json, never "now".
"""
import re
import sys

from forge_lib import issues, keys, prs, state, util
from forge_lib.errors import ForgeError

NOTES_HEADING = "## Notes"
RENDERED = {"tests": "tests.md", "prs": "PRs.md", "issues": "issues.md"}
STATUS_MD = "status.md"
HANDBACK_MD = "HANDBACK.md"
DECISIONS_MD = "decisions.md"
HISTORY_TAIL = 5


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def extract_notes(existing_md: str) -> str:
    """The '## Notes' section (heading included) up to the next level-2 heading, or a blank one."""
    m = re.search(r"^## Notes[ \t]*$", existing_md, flags=re.M)
    if not m:
        return NOTES_HEADING + "\n"
    rest = existing_md[m.start():]
    nxt = re.search(r"^## (?!Notes)", rest[len(NOTES_HEADING):], flags=re.M)
    section = rest if not nxt else rest[:len(NOTES_HEADING) + nxt.start()]
    return section.rstrip() + "\n"


def render_tests(key: str, items: list[dict], existing_md: str = "") -> str:
    green = sum(1 for t in items if t.get("status") == "green")
    rows = [[t["id"], t.get("name"), t.get("type"), t.get("status"),
             f"`{t['command']}`" if t.get("command") else "", t.get("expected"),
             t.get("evidence") or t.get("skip_reason")] for t in items]
    body = _table(["ID", "Name", "Type", "Status", "Command", "Expected", "Evidence / skip reason"], rows) \
        if items else "_No tests yet._"
    return (f"# Tests — {key}\n\n<!-- Rendered from tests.json by forge; edit only the Notes section. -->\n\n"
            f"{green}/{len(items)} green\n\n{body}\n\n{extract_notes(existing_md)}")


def _pr_cells(item: dict) -> list:
    st = item.get("status") or {}
    checks = (st.get("checks") or {}).get("state", "?")
    threads = st.get("threads") or {}
    sonar = (st.get("sonar") or {}).get("status", "-")
    return [item.get("stack_index"), f"[{item.get('repo')}#{item.get('number')}]({item.get('url')})",
            item.get("title") or st.get("title"), f"{item.get('branch')} → {item.get('base')}",
            st.get("state", "?"), "yes" if st.get("is_draft") else "no", checks,
            f"{threads.get('unresolved', '?')} open / {threads.get('total', '?')}", sonar,
            st.get("error", "")]


def pr_problems(items: list[dict], task: dict | None) -> dict[int, list[str]]:
    """id(item) -> gate problems, judged with the task's sonar key and required checks."""
    task = task or {}
    sonar = bool(task.get("sonar_project_key"))
    required = task.get("required_checks") or []
    return {id(item): prs.problems(item, sonar, required, below) for item, below in prs.with_below(items)}


def _pr_label(item: dict) -> str:
    return f"{item.get('repo')}#{item.get('number')}"


def _strip_label(item: dict, problem: str) -> str:
    return problem.removeprefix(f"PR {_pr_label(item)}: ")


def render_prs(key: str, items: list[dict], task: dict | None = None) -> str:
    probs = pr_problems(items, task)
    items = sorted(items, key=lambda p: (p.get("repo", ""), p.get("stack_index", 0), p.get("number", 0)))
    rows = [_pr_cells(p) + ["; ".join(_strip_label(p, x) for x in probs[id(p)]) or "none"] for p in items]
    body = _table(["Stack", "PR", "Title", "Branch", "State", "Draft", "CI", "Threads", "Sonar", "Error",
                   "Problems"], rows) if items else "_No PRs yet._"
    return f"# PRs — {key}\n\n<!-- Rendered from prs.json by forge. Stack 0 = bottom. -->\n\n{body}\n"


def _decision_text(item: dict) -> str:
    d = item.get("decision")
    return f"{d.get('text')} ({d.get('by')}, {d.get('at')})" if d else ""


def render_issues(key: str, items: list[dict]) -> str:
    rows = [[i["id"], i.get("severity"), i.get("state"), i.get("status"),
             f"[{_cell(i.get('title'))}]({i.get('file')})", _decision_text(i),
             f"[link]({i['artifact_url']})" if i.get("artifact_url") else "", i.get("resolution")]
            for i in items]
    body = _table(["ID", "Severity", "State", "Status", "Title", "Decision", "Artifact", "Resolution"], rows) \
        if items else "_No issues._"
    return f"# Issues — {key}\n\n<!-- Rendered from issues.json by forge. -->\n\n{body}\n"


def _state_md(st: str | None) -> str:
    return f"**{st}**" if st == "awaiting-user" else str(st)


def _states_text(counts: dict) -> str:
    """Non-zero issue counts by state, awaiting-user in bold."""
    parts = [(f"**{n} {st}**" if st == "awaiting-user" else f"{n} {st}") for st, n in counts.items() if n]
    return " · ".join(parts) or "none"


def _session_text(session: dict | None) -> str:
    if not session or not (session.get("name") or session.get("id")):
        return "—"
    name, sid = session.get("name"), session.get("id")
    return f"{name} (`{sid}`)" if name and sid else f"`{name or sid}`"


def _gate_text(gate: dict | None) -> str:
    gate = gate or {}
    if not gate.get("last_run"):
        return "not run"
    return f"{'PASS' if gate.get('passed') else 'FAIL'} (last run {gate['last_run']})"


def _pr_health(items: list[dict], probs: dict[int, list[str]]) -> str:
    if not items:
        return "no PRs"
    green = sum(1 for p in items if not probs[id(p)])
    return f"{green}/{len(items)} green"


def _task_facts(key: str, task: dict) -> dict:
    tests = state.load_json_items(key, "tests")
    pr_items = state.load_json_items(key, "prs")
    all_issues = state.load_json_items(key, "issues")
    open_issues = [i for i in all_issues if i.get("status") == "open"]
    by_state = {st: sum(1 for i in all_issues if i.get("state") == st) for st in issues.STATES}
    return {
        "issue_states": by_state,
        "tests": tests, "prs": pr_items, "probs": pr_problems(pr_items, task), "open_issues": open_issues,
        "blockers": sum(1 for i in open_issues if i.get("severity") == "blocker"),
        "open_inbox": len(state.open_inbox(key)),
        "tests_green": sum(1 for t in tests if t.get("status") == "green"),
    }


def render_status(key: str, task: dict, facts: dict, children: list[tuple[str, dict]]) -> str:
    gate = task.get("gate") or {}
    tests, pr_items, probs = facts["tests"], facts["prs"], facts["probs"]
    summary = _table(["Field", "Value"], [
        ["State", task.get("state")],
        ["Owner session", _session_text(task.get("session"))],
        ["Gate", _gate_text(gate)],
        ["Tests", f"{facts['tests_green']}/{len(tests)} green"],
        ["Open issues", f"{len(facts['open_issues'])} ({facts['blockers']} blocker)"],
        ["Issues by state", _states_text(facts["issue_states"])],
        ["Open inbox", facts["open_inbox"]],
        ["PRs", _pr_health(pr_items, probs)],
        ["Updated", task.get("updated")],
    ])
    parts = [f"# Status — {key}\n\n<!-- Rendered from task.json + tests/prs/issues/inbox json by forge. "
             f"Edit via the forge CLI. -->\n\n**{task.get('title', '')}**\n\n{summary}\n"]
    reasons = gate.get("reasons") or []
    if gate.get("last_run"):
        listed = "\n".join(f"- {r}" for r in reasons) if reasons else "_Gate passed._"
        parts.append(f"## Gate reasons\n\n{listed}\n")
    if pr_items:
        lines = []
        for item in sorted(pr_items, key=lambda p: (p.get("repo", ""), p.get("stack_index", 0))):
            found = probs[id(item)]
            head = f"- [{_pr_label(item)}]({item.get('url')}) {_cell(item.get('title'))}"
            lines.append(head + (" — green" if not found else "")
                         + "".join(f"\n  - {_strip_label(item, x)}" for x in found))
        parts.append("## PR problems\n\n" + "\n".join(lines) + "\n")
    if facts["open_issues"]:
        parts.append("## Open issues\n\n" + "\n".join(
            f"- [{i['id']}]({i.get('file')}) ({i.get('severity')}, {_state_md(i.get('state'))}) "
            f"{_cell(i.get('title'))}"
            for i in facts["open_issues"]) + "\n")
    history = (task.get("state_history") or [])[-HISTORY_TAIL:][::-1]
    if history:
        parts.append("## Recent state changes\n\n" + _table(
            ["At", "State", "Note"], [[h.get("at"), h.get("state"), h.get("note")] for h in history]) + "\n")
    if children:
        rows = [[f"[{k.rsplit('/', 1)[-1]}](tasks/{k.rsplit('/', 1)[-1]}/status.md)", t.get("state"),
                 _gate_text(t.get("gate"))] for k, t in children]
        parts.append("## Children\n\n" + _table(["Task", "State", "Gate"], rows) + "\n")
    return "\n".join(parts)


def _pr_status_cells(item: dict) -> list:
    st = item.get("status") or {}
    threads = st.get("threads") or {}
    return [f"[{_pr_label(item)}]({item.get('url')})", item.get("title") or st.get("title"),
            st.get("state", "?"), (st.get("checks") or {}).get("state", "?"),
            (st.get("sonar") or {}).get("status", "-"),
            f"{threads.get('unresolved', '?')} open / {threads.get('total', '?')}", st.get("fetched_at")]


def render_handback(key: str, task: dict, facts: dict) -> str:
    head = (f"# Handback — {key}\n\n<!-- Rendered from task.json handback by forge; "
            f"written by `forge handback`. -->\n\n**{task.get('title', '')}**\n")
    hb = task.get("handback")
    if not hb:
        return f"{head}\n_Not handed back yet._ Current state: {task.get('state')}.\n"
    snap = hb.get("gate")
    gate = snap or task.get("gate") or {}
    gate_md = _gate_text(gate) + ("" if snap else " — current gate (no snapshot stored at handback)")
    gate_md += "".join(f"\n- {r}" for r in gate.get("reasons") or [])
    tests, pr_items = facts["tests"], facts["prs"]
    ordered = sorted(pr_items, key=lambda p: (p.get("repo", ""), p.get("stack_index", 0)))
    prs_md = _table(["PR", "Title", "State", "CI", "Sonar", "Threads", "Fetched"],
                    [_pr_status_cells(p) for p in ordered]) if pr_items else "_No PRs._"
    tests_md = f"{facts['tests_green']}/{len(tests)} green"
    if tests:
        tests_md += "\n\n" + _table(["ID", "Name", "Status", "Evidence / skip reason"],
                                    [[t["id"], t.get("name"), t.get("status"),
                                      t.get("evidence") or t.get("skip_reason")] for t in tests])
    return (f"{head}\nHanded back {hb.get('at')} · current state: {task.get('state')}\n\n"
            f"## Summary\n\n{(hb.get('summary') or '').strip()}\n\n"
            f"## Docs\n\n{(hb.get('docs') or '').strip()}\n\n"
            f"## Gate at handback\n\n{gate_md}\n\n## PRs\n\n{prs_md}\n\n## Tests\n\n{tests_md}\n")


def render_project_status(slug: str) -> str:
    project = state.load_project(slug)
    rows = []
    for key in state.descendant_keys(slug):
        task = state.load_task(key)
        facts = _task_facts(key, task)
        rel = "/".join(f"tasks/{seg}" for seg in key.split("/")[1:])
        depth = key.count("/") - 1
        waiting = facts["issue_states"]["awaiting-user"]
        rows.append([f"{'↳ ' * depth}[{key.split('/', 1)[1]}]({rel}/status.md)", task.get("state"),
                     _session_text(task.get("session")), _gate_text(task.get("gate")),
                     facts["blockers"], f"**{waiting}**" if waiting else 0, _states_text(facts["issue_states"]),
                     _pr_health(facts["prs"], facts["probs"]),
                     f"{facts['tests_green']}/{len(facts['tests'])}", facts["open_inbox"]])
    body = _table(["Task", "State", "Owner session", "Gate", "Open blockers", "Awaiting you", "Issues", "PRs",
                   "Tests", "Open inbox"], rows) if rows else "_No tasks yet._"
    return (f"# Status — {slug}\n\n<!-- Rendered from project.json + task json by forge. "
            f"Edit via the forge CLI. -->\n\n**{project.get('title', '')}** · {project.get('state')}\n\n"
            f"Coordinator: {_session_text(project.get('coordinator_session'))} · "
            f"Decision log: [{DECISIONS_MD}]({DECISIONS_MD})\n\n{body}\n")


def _issue_link(key: str, item: dict) -> str:
    rel = "/".join(f"tasks/{seg}" for seg in key.split("/")[1:])
    file = item.get("file") or f"issues/{item['id']}.md"
    return f"[{item['id']}]({rel}/{file})"


def render_decisions(slug: str) -> str:
    """Project decision log: every recorded decision (newest first) + issues awaiting the user."""
    decided, waiting = [], []
    for key in state.descendant_keys(slug):
        task_label = key.split("/", 1)[1]
        for item in state.load_json_items(key, "issues"):
            entries = [h for h in item.get("history") or [] if h.get("state") == "decided"]
            if not entries and item.get("decision"):
                d = item["decision"]
                entries = [{"at": d.get("at"), "by": d.get("by"), "note": d.get("text")}]
            for h in entries:
                decided.append([h.get("at"), task_label, _issue_link(key, item), item.get("severity"),
                                f"{_cell(item.get('title'))}: {h.get('note')}", h.get("by"),
                                _state_md(item.get("state")), item.get("resolution")])
            if item.get("state") == "awaiting-user":
                since = ((item.get("history") or [{}])[-1]).get("at") or item.get("created")
                waiting.append([since, task_label, _issue_link(key, item), item.get("severity"),
                                _cell(item.get("title"))])
    decided.sort(key=lambda r: r[0] or "", reverse=True)
    waiting.sort(key=lambda r: r[0] or "")
    wait_md = _table(["Since", "Task", "Issue", "Severity", "Title"], waiting) if waiting \
        else "_Nothing is waiting for you._"
    log_md = _table(["Date", "Task", "Issue", "Severity", "Decision", "By", "Current state", "Resolution"],
                    decided) if decided else "_No decisions recorded yet._"
    return (f"# Decisions — {slug}\n\n<!-- Rendered from every task's issues.json by forge. "
            f"Record decisions with `forge issues decide` or the UI. -->\n\n"
            f"## Awaiting your decision\n\n{wait_md}\n\n## Decision log\n\n{log_md}\n")


def _write(path, text: str) -> None:
    if util.read_text(path, None) != text:
        util.atomic_write_text(path, text)


def render_kind(key: str, kind: str) -> None:
    if kind not in RENDERED:
        return
    path = state.task_dir(key) / RENDERED[kind]
    items = state.load_json_items(key, kind)
    if kind == "tests":
        text = render_tests(key, items, util.read_text(path))
    elif kind == "prs":
        text = render_prs(key, items, state.load_task(key))
    else:
        text = render_issues(key, items)
    _write(path, text)


def write_status(key: str) -> None:
    """status.md for a task, or the project-level status.md for a slug."""
    if keys.is_project_key(key):
        _write(state.project_dir(key) / STATUS_MD, render_project_status(key))
        _write(state.project_dir(key) / DECISIONS_MD, render_decisions(key))
        return
    task = state.load_task(key)
    children = [(c, state.load_task(c)) for c in state.child_keys(key)]
    _write(state.task_dir(key) / STATUS_MD, render_status(key, task, _task_facts(key, task), children))


def write_handback(key: str) -> None:
    task = state.load_task(key)
    _write(state.task_dir(key) / HANDBACK_MD, render_handback(key, task, _task_facts(key, task)))


def refresh_status(key: str) -> None:
    """Re-render status.md for key, its ancestors (children states) and the project."""
    k = key
    while k:
        write_status(k)
        k = keys.parent_key(k)


def on_change(key: str) -> None:
    """After project/task json under key is written: PRs.md problems depend on task.json, and status.md
    on everything. A render failure only warns - the mutation itself already succeeded."""
    try:
        if not keys.is_project_key(key):
            render_kind(key, "prs")
        refresh_status(key)
    except (ForgeError, OSError, ValueError) as e:
        print(f"forge: warning: re-rendering status for {key} failed: {e}", file=sys.stderr)


def render_task(key: str) -> None:
    issues.migrate_file(key)
    for kind in RENDERED:
        render_kind(key, kind)
    write_status(key)
    write_handback(key)


def render(key: str) -> list[str]:
    """Render a task (and its ancestors' status), or every task of a project. Returns the rendered task keys."""
    targets = state.descendant_keys(key) if keys.is_project_key(key) else [key]
    for k in targets:
        render_task(k)
    refresh_status(keys.parent_key(key) or key)
    return targets


def render_all() -> list[str]:
    """Regenerate every rendered file across FORGE_HOME. Returns project slugs + task keys."""
    out = []
    for p in state.list_projects():
        out.append(p["slug"])
        out.extend(render(p["slug"]))
    return out
