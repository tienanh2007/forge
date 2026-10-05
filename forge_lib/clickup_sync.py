"""Mirror forge task states onto linked ClickUp tasks."""
import os

from forge_lib import keys, state, util
from forge_lib.errors import ForgeError, UsageError
from forge_lib.integrations import clickup

DEFAULT_STATUS_MAP = {
    "scoped": "planned", "dispatched": "in progress", "in-progress": "in progress",
    "coordinating": "in progress", "blocked": "blocked", "in-review": "review", "handed-back": "review",
    "done": "done", "cancelled": "cancelled",
}


def status_map(project: str) -> dict:
    """Project's `clickup_status_map` layered over the default."""
    custom = state.load_project(keys.project_of(project)).get("clickup_status_map") or {}
    return {**DEFAULT_STATUS_MAP, **custom}


def auto_enabled() -> bool:
    return os.environ.get("FORGE_NO_CLICKUP") != "1" and bool(util.get_config("CLICKUP_TOKEN"))


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def _append_log(key: str, line: str) -> None:
    path = state.task_dir(key) / "log.md"
    text = util.read_text(path)
    util.atomic_write_text(path, text + ("" if not text or text.endswith("\n") else "\n") + line + "\n")


def push(key: str, task: dict, target: str) -> dict:
    """Set the ClickUp status and record the outcome on the task; never raises."""
    try:
        clickup.update_status(task["clickup"]["id"], target)
        error = None
    except Exception as e:  # noqa: BLE001 - a ClickUp failure must not fail the forge transition
        error = str(e) or type(e).__name__
    task["clickup_sync"] = {"status": target, "at": util.now_iso(), "error": error}
    state.save(key, task)
    if error:
        _append_log(key, f"- {task['clickup_sync']['at']} ClickUp status -> {target!r} failed: {error}")
    return task


def after_transition(key: str, task: dict) -> dict:
    """Best-effort auto-sync after `set_state`; skips unlinked tasks and already-pushed statuses."""
    if not (task.get("clickup") or {}).get("id") or not auto_enabled():
        return task
    try:
        target = status_map(key).get(task.get("state"))
    except ForgeError:
        return task
    last = task.get("clickup_sync") or {}
    if not target or (_same(last.get("status"), target) and not last.get("error")):
        return task
    return push(key, task, target)


def link(key: str, ref: str) -> dict:
    from forge_lib import tasks
    task = state.load_task(key)
    resolved = tasks.resolve_clickup_ref(ref)
    if (task.get("clickup") or {}).get("id") != resolved.get("id"):
        task.pop("clickup_sync", None)
    task["clickup"] = resolved
    return state.save(key, task)


def _parent(project: str) -> dict:
    parent = state.load_project(project).get("clickup_parent") or {}
    ref = parent.get("id") or parent.get("custom_id")
    if not ref:
        raise UsageError(f"project {project} has no clickup_parent")
    return clickup.get_task(ref, include_subtasks=True)


def _matches(dir_name: str, custom_id: str | None) -> bool:
    return bool(custom_id) and (dir_name == custom_id or dir_name.startswith(custom_id + "-"))


def link_subtasks(project: str) -> list[dict]:
    """Link each forge task whose directory name starts with a parent subtask's custom id."""
    subtasks = _parent(project).get("subtasks") or []
    rows = []
    for key in state.all_task_keys(project):
        task = state.load_task(key)
        dir_name = key.rsplit("/", 1)[-1]
        sub = next((s for s in subtasks if _matches(dir_name, s.get("custom_id"))), None)
        if not sub:
            continue
        if (task.get("clickup") or {}).get("id"):
            rows.append({"key": key, "custom_id": sub.get("custom_id"), "action": "skipped (already linked)"})
            continue
        task["clickup"] = clickup.ref_of(sub)
        task.pop("clickup_sync", None)
        state.save(key, task)
        rows.append({"key": key, "custom_id": sub.get("custom_id"), "action": "linked"})
    return rows


def validate_map(project: str) -> list[str]:
    """Valid statuses of the parent's list; raises if any mapped status is not one of them."""
    list_id = (_parent(project).get("list") or {}).get("id")
    if not list_id:
        raise ForgeError(f"could not determine the ClickUp list of {project}'s clickup_parent")
    valid = clickup.list_statuses(list_id)
    unknown = sorted({v for v in status_map(project).values() if not any(_same(v, s) for s in valid)})
    if unknown:
        raise UsageError(f"clickup_status_map uses statuses not on list {list_id}: {', '.join(unknown)}; "
                         f"valid: {', '.join(valid)}")
    return valid


def sync(target_key: str, dry_run: bool = False) -> list[dict]:
    project = keys.project_of(target_key)
    validate_map(project)
    smap = status_map(project)
    key_list = state.all_task_keys(project) if keys.is_project_key(target_key) else \
        [target_key, *state.descendant_keys(target_key)]
    rows = []
    for key in key_list:
        task = state.load_task(key)
        ref = task.get("clickup") or {}
        if not ref.get("id"):
            continue
        target = smap.get(task.get("state"))
        row = {"key": key, "clickup_id": ref["id"], "custom_id": ref.get("custom_id"), "state": task.get("state"),
               "target": target}
        if dry_run:
            try:
                row["current"] = clickup.status_of(clickup.get_task(ref["id"], include_subtasks=False))
            except ForgeError as e:
                row["error"] = str(e)
        elif target:
            row["error"] = push(key, task, target)["clickup_sync"]["error"]
        rows.append(row)
    return rows
