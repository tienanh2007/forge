"""Handback gate: tests, PRs (CI/threads/sonar), inbox, children."""
from forge_lib import issues, prs, render, state, tasks, util
from forge_lib.errors import UsageError

CHILD_OK = ("handed-back", "done", "cancelled")


def evaluate(key: str, refresh: bool = True) -> list[str]:
    """Gate failure reasons for a task (empty = pass). Refreshes PR status first unless refresh=False."""
    task = state.load_task(key)
    reasons = []
    tests = state.load_json_items(key, "tests")
    if not tests:
        reasons.append("no tests recorded in tests.json")
    for t in tests:
        if t.get("status") == "skipped" and not t.get("skip_reason"):
            reasons.append(f"test {t['id']} skipped without a skip_reason")
        elif t.get("status") not in ("green", "skipped"):
            reasons.append(f"test {t['id']} ({t.get('name')}) is {t.get('status')}")
    items = prs.refresh_task(key) if refresh else state.load_json_items(key, "prs")
    if not items:
        reasons.append("no PRs recorded in prs.json")
    required = task.get("required_checks") or []
    for item, below in prs.with_below(items):
        reasons.extend(prs.problems(item, bool(task.get("sonar_project_key")), required, below))
    for m in state.open_inbox(key):
        reasons.append(f"open inbox message {m['id']} from {m.get('author')}")
    for i in state.load_json_items(key, "issues"):
        if i.get("state") in issues.PENDING_ACTION:
            reasons.append(f"issue {i['id']} is {i['state']}: decision not acted on yet "
                           f"(resolve it with forge issues resolve)")
    for child in state.child_keys(key):
        child_state = state.load_task(child).get("state")
        if child_state not in CHILD_OK:
            reasons.append(f"child task {child} is {child_state}")
    return reasons


def run(key: str, refresh: bool = True) -> dict:
    """Evaluate and record the result in task.gate (consecutive_blocks is preserved)."""
    reasons = evaluate(key, refresh=refresh)
    task = state.load_task(key)
    gate = task.setdefault("gate", {})
    gate.update(last_run=util.now_iso(), passed=not reasons, reasons=reasons)
    gate.setdefault("consecutive_blocks", 0)
    state.save(key, task)
    return {"key": key, "passed": not reasons, "reasons": reasons, "last_run": gate["last_run"]}


def handback(key: str, summary: str, docs: str) -> dict:
    if not (summary or "").strip() or not (docs or "").strip():
        raise UsageError("handback requires non-empty --summary and --docs")
    result = run(key)
    if result["passed"]:
        task = state.load_task(key)
        gate_snapshot = {k: result[k] for k in ("passed", "reasons", "last_run")}
        task["handback"] = {"summary": summary, "docs": docs, "at": util.now_iso(), "gate": gate_snapshot}
        state.save(key, task)
        tasks.set_state(key, "handed-back", note="gate passed", allow_handback=True)
    render.write_handback(key)
    return result
