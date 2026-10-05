"""Filesystem-backed state under $FORGE_HOME: projects, tasks, item lists, tree."""
import os
from pathlib import Path

from forge_lib import keys, util
from forge_lib.errors import NotFound, UsageError

SCHEMA = 1
DEFAULT_HOME = "~/agent-projects"
ITEM_KINDS = ("tests", "prs", "issues", "inbox")
STATES = ("scoped", "dispatched", "in-progress", "blocked", "in-review", "handed-back",
          "done", "coordinating", "cancelled")
PROJECT_STATES = ("scoping", "active", "done")
FINISHED = ("handed-back", "done")
RUNNING = ("dispatched", "in-progress")


def forge_home() -> Path:
    return Path(os.environ.get("FORGE_HOME") or DEFAULT_HOME).expanduser()


def cache_dir() -> Path:
    return forge_home() / ".cache"


def task_dir(key: str) -> Path:
    return keys.key_to_path(forge_home(), key)


def project_dir(slug: str) -> Path:
    return task_dir(keys.project_of(slug))


def meta_file(key: str) -> Path:
    return task_dir(key) / ("project.json" if keys.is_project_key(key) else "task.json")


def exists(key: str) -> bool:
    return meta_file(key).exists()


def load(key: str) -> dict:
    """project.json for a slug, task.json for a task key."""
    data = util.read_json(meta_file(key))
    if data is None:
        kind = "project" if keys.is_project_key(key) else "task"
        raise NotFound(f"{kind} not found: {key}")
    return data


def save(key: str, data: dict) -> dict:
    data["schema"] = SCHEMA
    data["updated"] = util.now_iso()
    util.atomic_write_json(meta_file(key), data)
    from forge_lib import render
    render.on_change(key)
    return data


def load_project(slug: str) -> dict:
    if not keys.is_project_key(slug):
        raise UsageError(f"not a project slug: {slug}")
    return load(slug)


def load_task(key: str) -> dict:
    if keys.is_project_key(key):
        raise UsageError(f"not a task key: {key}")
    return load(key)


def list_projects() -> list[dict]:
    home = forge_home()
    if not home.is_dir():
        return []
    out = []
    for d in sorted(home.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "project.json").exists():
            out.append(util.read_json(d / "project.json"))
    return out


def child_keys(key: str) -> list[str]:
    tasks = task_dir(key) / keys.TASKS
    if not tasks.is_dir():
        return []
    return [f"{key}/{d.name}" for d in sorted(tasks.iterdir())
            if d.is_dir() and (d / "task.json").exists()]


def descendant_keys(key: str) -> list[str]:
    """All task keys below key (depth-first, parents before children)."""
    out = []
    for child in child_keys(key):
        out.append(child)
        out.extend(descendant_keys(child))
    return out


def all_task_keys(project: str | None = None) -> list[str]:
    slugs = [project] if project else [p["slug"] for p in list_projects()]
    return [k for s in slugs for k in descendant_keys(s)]


def items_file(key: str, kind: str) -> Path:
    if kind not in ITEM_KINDS:
        raise UsageError(f"unknown item kind: {kind}")
    return task_dir(key) / f"{kind}.json"


def load_json_items(key: str, kind: str) -> list[dict]:
    data = util.read_json(items_file(key, kind), {"schema": SCHEMA, "items": []})
    items = data.get("items", [])
    if kind == "issues":
        from forge_lib import issues
        issues.migrate_items(key, items)  # in memory; persisted by the next save / forge render
    return items


def save_json_items(key: str, kind: str, items: list[dict]) -> None:
    util.atomic_write_json(items_file(key, kind), {"schema": SCHEMA, "items": items})
    from forge_lib import render
    render.render_kind(key, kind)
    render.on_change(key)


def read_issue_body(key: str, issue_id: str) -> str:
    for item in load_json_items(key, "issues"):
        if item["id"] == issue_id:
            return util.read_text(task_dir(key) / item.get("file", f"issues/{issue_id}.md"))
    raise NotFound(f"issue {issue_id} not found in {key}")


def find_item(items: list[dict], item_id: str, what: str) -> dict:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise NotFound(f"{what} {item_id} not found")


def open_inbox(key: str) -> list[dict]:
    return [m for m in load_json_items(key, "inbox")
            if m.get("author") in ("user", "coordinator") and m.get("status") == "open"]


def task_counts(key: str, task: dict) -> dict:
    from forge_lib import prs
    tests = load_json_items(key, "tests")
    pr_items = load_json_items(key, "prs")
    sonar = bool(task.get("sonar_project_key"))
    issue_items = load_json_items(key, "issues")
    return {
        "open_issues": sum(1 for i in issue_items if i.get("status") == "open"),
        "awaiting_user": sum(1 for i in issue_items if i.get("state") == "awaiting-user"),
        "open_inbox": len(open_inbox(key)),
        "prs": len(pr_items),
        "prs_green": sum(1 for p, below in prs.with_below(pr_items)
                         if prs.is_green(p, sonar, task.get("required_checks"), below)),
        "tests": len(tests),
        "tests_green": sum(1 for t in tests if t.get("status") == "green"),
    }


def _task_node(key: str) -> dict:
    task = load_task(key)
    return {
        "key": key, "title": task.get("title", ""), "state": task.get("state"), "kind": "task",
        "path": str(task_dir(key)), "session": task.get("session"), "clickup": task.get("clickup"),
        "depends_on": task.get("depends_on", []),
        "counts": task_counts(key, task),
        "children": [_task_node(c) for c in child_keys(key)],
    }


def _sum_counts(nodes: list[dict], total: dict) -> dict:
    for n in nodes:
        for k, v in n["counts"].items():
            total[k] += v
        _sum_counts(n["children"], total)
    return total


def tree(key: str | None = None):
    """Nested tree. No key -> list of project nodes. Project counts are totals over all its tasks."""
    if key is None:
        return [tree(p["slug"]) for p in list_projects()]
    if not keys.is_project_key(key):
        return _task_node(key)
    project = load_project(key)
    children = [_task_node(c) for c in child_keys(key)]
    zero = dict.fromkeys(("open_issues", "awaiting_user", "open_inbox", "prs", "prs_green", "tests", "tests_green"), 0)
    return {
        "key": key, "title": project.get("title", ""), "state": project.get("state"), "kind": "project",
        "path": str(task_dir(key)), "session": project.get("coordinator_session"),
        "clickup": project.get("clickup_parent"), "depends_on": [],
        "counts": _sum_counts(children, zero), "children": children,
    }
