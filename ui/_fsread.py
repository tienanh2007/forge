"""Minimal fallback for the forge_lib API, used only when forge_lib is not importable.

Reads the $FORGE_HOME files exactly per CONTRACT.md; the only writes are inbox appends and
delivered flags (atomic, same schema).
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ISSUE_ID_RE = re.compile(r"^I-\d+$")
COMMENT_ID_RE = re.compile(r"^C-\d+$")
ITEM_KINDS = ("tests", "prs", "issues", "inbox")
OPEN_INBOX_AUTHORS = ("user", "coordinator")
DEFAULT_HOME = "~/agent-projects"

_write_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def forge_home() -> Path:
    return Path(os.environ.get("FORGE_HOME") or DEFAULT_HOME).expanduser()


def split_key(key: str) -> list[str]:
    if not isinstance(key, str) or not key:
        raise ValueError("key is required")
    segments = key.split("/")
    for segment in segments:
        if not SEGMENT_RE.match(segment) or segment == "tasks":
            raise ValueError(f"invalid key segment: {segment!r}")
    return segments


def task_dir(key: str, home: Path | None = None) -> Path:
    segments = split_key(key)
    path = (home or forge_home()) / segments[0]
    for segment in segments[1:]:
        path = path / "tasks" / segment
    return path


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_projects() -> list[dict]:
    home = forge_home()
    if not home.is_dir():
        return []
    projects = []
    for entry in sorted(home.iterdir()):
        if entry.name.startswith(".") or not (entry / "project.json").is_file():
            continue
        projects.append(read_json(entry / "project.json", {}))
    return projects


def load_project(slug: str) -> dict:
    if len(split_key(slug)) != 1:
        raise ValueError("project slug must be a single segment")
    data = read_json(task_dir(slug) / "project.json")
    if data is None:
        raise FileNotFoundError(f"project not found: {slug}")
    return data


def load_task(key: str) -> dict:
    data = read_json(task_dir(key) / "task.json")
    if data is None:
        raise FileNotFoundError(f"task not found: {key}")
    return data


def load_json_items(key: str, kind: str) -> list[dict]:
    if kind not in ITEM_KINDS:
        raise ValueError(f"unknown item kind: {kind}")
    data = read_json(task_dir(key) / f"{kind}.json", {})
    return data.get("items", []) if isinstance(data, dict) else []


def read_issue_body(key: str, issue_id: str) -> str:
    if not ISSUE_ID_RE.match(issue_id or ""):
        raise ValueError(f"invalid issue id: {issue_id!r}")
    base = task_dir(key)
    rel = next((i.get("file") for i in load_json_items(key, "issues") if i.get("id") == issue_id), None)
    path = (base / (rel or f"issues/{issue_id}.md")).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("issue file escapes task dir")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _is_pr_green(pr: dict) -> bool:
    status = pr.get("status") or {}
    checks_ok = (status.get("checks") or {}).get("state") == "SUCCESS"
    threads_ok = (status.get("threads") or {}).get("unresolved", 0) == 0
    sonar = (status.get("sonar") or {}).get("status")
    return checks_ok and threads_ok and sonar in (None, "OK")


def _counts(key: str) -> dict:
    issues = load_json_items(key, "issues")
    inbox = load_json_items(key, "inbox")
    prs = load_json_items(key, "prs")
    tests = load_json_items(key, "tests")
    return {
        "open_issues": sum(1 for i in issues if i.get("status") == "open"),
        "open_inbox": sum(
            1 for m in inbox if m.get("author") in OPEN_INBOX_AUTHORS and m.get("status") == "open"
        ),
        "prs": len(prs),
        "prs_green": sum(1 for p in prs if _is_pr_green(p)),
        "tests": len(tests),
        "tests_green": sum(1 for t in tests if t.get("status") == "green"),
    }


def _children(key: str, path: Path) -> list[dict]:
    sub = path / "tasks"
    if not sub.is_dir():
        return []
    return [
        _task_node(f"{key}/{entry.name}")
        for entry in sorted(sub.iterdir())
        if (entry / "task.json").is_file()
    ]


def _task_node(key: str) -> dict:
    path = task_dir(key)
    task = load_task(key)
    return {
        "key": key,
        "title": task.get("title", ""),
        "state": task.get("state", ""),
        "kind": "task",
        "path": str(path),
        "session": task.get("session"),
        "clickup": task.get("clickup"),
        "depends_on": task.get("depends_on", []),
        "counts": _counts(key),
        "children": _children(key, path),
    }


def _project_node(slug: str) -> dict:
    project = load_project(slug)
    path = task_dir(slug)
    zero = {"open_issues": 0, "open_inbox": 0, "prs": 0, "prs_green": 0, "tests": 0, "tests_green": 0}
    return {
        "key": slug,
        "title": project.get("title", slug),
        "state": project.get("state", ""),
        "kind": "project",
        "path": str(path),
        "session": project.get("coordinator_session"),
        "clickup": project.get("clickup_parent"),
        "counts": zero,
        "children": _children(slug, path),
    }


def tree(key: str | None = None):
    if key is None:
        return [_project_node(p["slug"]) for p in list_projects() if p.get("slug")]
    if len(split_key(key)) == 1:
        return _project_node(key)
    return _task_node(key)


def _next_id(items: list[dict], prefix: str) -> str:
    nums = [int(i["id"][len(prefix):]) for i in items if str(i.get("id", "")).startswith(prefix)
            and i["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def inbox_add(key, body, issue_id=None, author="user", reply_to=None) -> dict:
    path = task_dir(key) / "inbox.json"
    with _write_lock:
        data = read_json(path, {"schema": 1, "items": []})
        items = data.setdefault("items", [])
        msg = {
            "id": _next_id(items, "C-"),
            "issue_id": issue_id,
            "author": author,
            "body": body,
            "at": now_iso(),
            "reply_to": reply_to,
            "status": "open",
            "delivered": False,
        }
        items.append(msg)
        data["schema"] = 1
        write_json_atomic(path, data)
    return msg


def mark_delivered(key: str, comment_id: str) -> None:
    path = task_dir(key) / "inbox.json"
    with _write_lock:
        data = read_json(path, {"schema": 1, "items": []})
        for msg in data.get("items", []):
            if msg.get("id") == comment_id:
                msg["delivered"] = True
        write_json_atomic(path, data)
