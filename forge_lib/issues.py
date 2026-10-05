"""Issues raised to the user: severity, a tracked state with history, and the recorded decision."""
from forge_lib import state, util
from forge_lib.errors import UsageError

SEVERITIES = ("blocker", "decision", "question", "fyi")
NEEDS_USER = ("blocker", "decision", "question")
STATES = ("open", "awaiting-user", "decided", "in-progress", "resolved", "wontfix")
CLOSED = ("resolved", "wontfix")
PENDING_ACTION = ("decided", "in-progress")  # user decided, worker has not finished acting on it
BY = ("user", "worker", "coordinator", "reviewer")
DECIDERS = ("user", "coordinator")
TRANSITIONS = {
    "open": ("awaiting-user", "decided", "in-progress", "resolved", "wontfix"),
    "awaiting-user": ("open", "decided", "in-progress", "resolved", "wontfix"),
    "decided": ("awaiting-user", "in-progress", "resolved", "wontfix"),
    "in-progress": ("awaiting-user", "decided", "resolved", "wontfix"),
    "resolved": ("open",),
    "wontfix": ("open",),
}
MIGRATED_NOTE = "migrated"


def status_for(st: str) -> str:
    """Legacy `status` kept in sync with `state` for older readers (gate, counts, hooks)."""
    return st if st in CLOSED else "open"


def default_state(severity: str) -> str:
    return "awaiting-user" if severity in NEEDS_USER else "open"


def next_states(st: str | None) -> list[str]:
    return list(TRANSITIONS.get(st or "open", ()))


def _check(value, allowed, what):
    if value not in allowed:
        raise UsageError(f"{what} must be one of {', '.join(allowed)} (got {value!r})")


def _entry(st: str, by: str, note: str = "", comment_id: str | None = None, at: str | None = None) -> dict:
    return {"state": st, "at": at or util.now_iso(), "by": by, "note": note or "", "comment_id": comment_id}


def migrate_item(item: dict, default_by: str = "worker") -> bool:
    """Give a pre-state issue a state + seeded history in place. Returns True if it changed."""
    if item.get("state") in STATES:
        return False
    status = item.get("status") or "open"
    st = status if status in CLOSED else default_state(item.get("severity"))
    item["state"] = st
    item["status"] = status_for(st)
    item.setdefault("history", [_entry(st, default_by, MIGRATED_NOTE, at=item.get("created"))])
    item.setdefault("decision", None)
    return True


def _migration_by(key: str) -> str:
    # Issues on a never-dispatched task were raised by a review (forge:adopt-project), else by its worker.
    try:
        return "worker" if (state.load_task(key).get("session") or {}).get("id") else "reviewer"
    except Exception:  # noqa: BLE001 - migration must never break a read
        return "worker"


def migrate_items(key: str, items: list[dict]) -> bool:
    """Migrate a loaded issues list in place (called by state.load_json_items)."""
    todo = [i for i in items if i.get("state") not in STATES]
    if not todo:
        return False
    by = _migration_by(key)
    for item in todo:
        migrate_item(item, by)
    return True


def migrate_file(key: str) -> bool:
    """Persist the migration of a task's issues.json (used by `forge render`). Returns True if written."""
    path = state.items_file(key, "issues")
    raw = util.read_json(path)
    if not raw or not any(i.get("state") not in STATES for i in raw.get("items", [])):
        return False
    items = raw["items"]
    migrate_items(key, items)
    util.atomic_write_json(path, {"schema": state.SCHEMA, "items": items})
    return True


def add(key: str, title: str, severity: str, body: str, st: str | None = None, by: str = "worker",
        note: str = "") -> dict:
    _check(severity, SEVERITIES, "severity")
    _check(by, BY, "--by")
    st = st or default_state(severity)
    _check(st, STATES, "state")
    state.load_task(key)
    items = state.load_json_items(key, "issues")
    issue_id = util.next_id(items, "I-")
    rel = f"issues/{issue_id}.md"
    text = body if body.lstrip().startswith("#") else f"# {issue_id}: {title}\n\n{body}"
    util.atomic_write_text(state.task_dir(key) / rel, text.rstrip() + "\n")
    now = util.now_iso()
    item = {"id": issue_id, "title": title, "severity": severity, "status": status_for(st), "state": st,
            "file": rel, "artifact_url": None, "created": now, "updated": now, "resolution": "",
            "decision": None, "history": [_entry(st, by, note or "raised", at=now)]}
    items.append(item)
    state.save_json_items(key, "issues", items)
    return item


def _mutate(key: str, issue_id: str, fn) -> dict:
    items = state.load_json_items(key, "issues")
    item = state.find_item(items, issue_id, "issue")
    fn(item)
    item["updated"] = util.now_iso()
    state.save_json_items(key, "issues", items)
    return item


def _move(item: dict, st: str, by: str, note: str = "", comment_id: str | None = None) -> None:
    item["state"] = st
    item["status"] = status_for(st)
    item.setdefault("history", []).append(_entry(st, by, note, comment_id))


def set_state(key: str, issue_id: str, st: str, note: str = "", by: str = "worker") -> dict:
    _check(st, STATES, "state")
    _check(by, BY, "--by")

    def apply(item):
        cur = item.get("state")
        if st not in TRANSITIONS.get(cur, ()):
            allowed = ", ".join(TRANSITIONS.get(cur, ())) or "none"
            raise UsageError(f"issue {issue_id}: cannot go {cur} -> {st} (allowed: {allowed})")
        _move(item, st, by, note)
    return _mutate(key, issue_id, apply)


def resolve(key: str, issue_id: str, resolution: str, wontfix: bool = False, by: str = "worker") -> dict:
    """Close an issue (from any state; re-resolving updates the resolution)."""
    _check(by, BY, "--by")
    st = "wontfix" if wontfix else "resolved"

    def apply(item):
        item["resolution"] = resolution
        _move(item, st, by, resolution)
    return _mutate(key, issue_id, apply)


def decide(key: str, issue_id: str, text: str, by: str = "user", comment_id: str | None = None,
           notify: bool | None = None, deliver: bool = True) -> dict:
    """Record a decision (state `decided`) and tell the worker `Decision on I-n: TEXT`.

    notify defaults to True unless comment_id is given (the decision then already sits in that inbox
    message). With a session and deliver=True the message goes through agents.send; otherwise it is only
    queued in the inbox. Returns {issue, delivery} (delivery None when not notified)."""
    if not text or not text.strip():
        raise UsageError("decision text is empty")
    _check(by, DECIDERS, "--by")
    text = text.strip()
    task = state.load_task(key)
    item = state.find_item(state.load_json_items(key, "issues"), issue_id, "issue")
    if item.get("state") in CLOSED:
        raise UsageError(f"issue {issue_id} is {item.get('state')}; reopen it before recording a decision")
    if comment_id:
        state.find_item(state.load_json_items(key, "inbox"), comment_id, "message")
    notify = comment_id is None if notify is None else notify
    delivery = None
    if notify:
        delivery = _notify(key, task, issue_id, f"Decision on {issue_id}: {text}", by, deliver)
        comment_id = comment_id or (delivery.get("message") or {}).get("id")

    def apply(it):
        it["decision"] = {"text": text, "by": by, "at": util.now_iso(), "comment_id": comment_id}
        _move(it, "decided", by, text, comment_id)
    return {"issue": _mutate(key, issue_id, apply), "delivery": delivery}


def _notify(key: str, task: dict, issue_id: str, text: str, author: str, deliver: bool) -> dict:
    from forge_lib import agents, inbox
    if deliver and (task.get("session") or {}).get("id"):
        return agents.send(key, text, author=author, issue_id=issue_id)
    msg = inbox.add(key, text, issue_id=issue_id, author=author)
    why = "delivery disabled" if not deliver else "task has no session"
    return {"mode": "queued", "delivered": False, "detail": f"{why}; queued in the inbox", "message": msg}


def set_artifact(key: str, issue_id: str, url: str) -> dict:
    return _mutate(key, issue_id, lambda item: item.update(artifact_url=url))


def open_issues(key: str) -> list[dict]:
    return [i for i in state.load_json_items(key, "issues") if i.get("status") == "open"]


def list_issues(key: str, st: str | None = None) -> list[dict]:
    """Issues of a task, or of every task under a project/task key, each with task_key."""
    from forge_lib import keys
    if st:
        _check(st, STATES, "--state")
    targets = ([] if keys.is_project_key(key) else [key]) + state.descendant_keys(key)
    if keys.is_project_key(key):
        state.load_project(key)
    else:
        state.load_task(key)
    return [{"task_key": k, **i} for k in targets for i in state.load_json_items(k, "issues")
            if not st or i.get("state") == st]
