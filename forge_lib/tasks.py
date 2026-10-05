"""Project and task creation, state transitions, lookups."""
import re
import sys
from pathlib import Path

from forge_lib import keys, render, state, util
from forge_lib.errors import IntegrationError, UsageError
from forge_lib.integrations import clickup

GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
INHERITED = ("repo", "github", "sonar_project_key", "base_branch")
DEFAULT_MAX_PARALLEL = 5
# Workers and the coordinator must share a mode, or each cross-session message is held for approval.
DEFAULT_PERMISSION_MODE = "auto"


def _warn(msg: str) -> None:
    print(f"forge: warning: {msg}", file=sys.stderr)


def detect_github(repo_path: str) -> str | None:
    try:
        proc = util.run(["git", "-C", repo_path, "remote", "get-url", "origin"], timeout=10)
    except IntegrationError:
        return None
    m = GITHUB_REMOTE_RE.search(proc.stdout.strip()) if proc.returncode == 0 else None
    return f"{m.group(1)}/{m.group(2)}" if m else None


def resolve_clickup_ref(ref: str) -> dict:
    """Fetch the ClickUp task when a token is configured; otherwise store what the ref tells us."""
    task_ref = clickup.parse_task_ref(ref)
    if util.get_config("CLICKUP_TOKEN"):
        try:
            return clickup.ref_of(clickup.get_task(task_ref, include_subtasks=False))
        except IntegrationError as e:
            _warn(f"could not fetch ClickUp task {task_ref}: {e}")
    custom = clickup.is_custom_id(task_ref)
    return {"id": None if custom else task_ref, "custom_id": task_ref if custom else None,
            "url": ref if ref.startswith("http") else None}


def _write_stub(path: Path, text: str) -> None:
    if not path.exists():
        util.atomic_write_text(path, text)


def init_project(slug: str, title: str, repo: str, github: str | None = None, sonar_key: str | None = None,
                 base_branch: str = "main", max_parallel: int = DEFAULT_MAX_PARALLEL, clickup_parent: str | None = None,
                 permission_mode: str = DEFAULT_PERMISSION_MODE) -> dict:
    keys.validate_segment(slug)
    if state.exists(slug):
        raise UsageError(f"project already exists: {slug}")
    repo_path = str(Path(repo).expanduser().resolve())
    now = util.now_iso()
    project = {
        "schema": state.SCHEMA, "slug": slug, "title": title, "state": "scoping",
        "clickup_parent": resolve_clickup_ref(clickup_parent) if clickup_parent else None,
        "repos": [{"path": repo_path, "github": github or detect_github(repo_path),
                   "sonar_project_key": sonar_key, "base_branch": base_branch}],
        "max_parallel": max_parallel, "permission_mode": permission_mode, "coordinator_session": None, "created": now, "updated": now,
    }
    d = state.project_dir(slug)
    (d / keys.TASKS).mkdir(parents=True, exist_ok=True)
    state.save(slug, project)
    _write_stub(d / "project.md", f"# {title}\n\n## Goal\n\n## Context\n\n## Decisions\n")
    for name in ("spec", "tests", "plan"):
        _write_stub(d / f"{name}.md", f"# {title} — {name}\n")
    return project


def _inherited(parent_key: str) -> dict:
    if keys.is_project_key(parent_key):
        repos = state.load_project(parent_key).get("repos") or [{}]
        first = repos[0]
        return {"repo": first.get("path"), "github": first.get("github"),
                "sonar_project_key": first.get("sonar_project_key"),
                "base_branch": first.get("base_branch") or "main"}
    parent = state.load_task(parent_key)
    return {k: parent.get(k) for k in INHERITED}


def new_task(parent_key: str, task_dir_name: str, title: str, depends_on: list[str] | None = None,
             **overrides) -> dict:
    """Create a task under a project or task. overrides: repo/github/sonar_project_key/base_branch."""
    keys.validate_segment(task_dir_name)
    state.load(parent_key)
    key = f"{parent_key}/{task_dir_name}"
    if state.exists(key):
        raise UsageError(f"task already exists: {key}")
    fields = _inherited(parent_key)
    for k, v in overrides.items():
        if v is not None:
            fields[k] = str(Path(v).expanduser().resolve()) if k == "repo" else v
    deps = [d for d in (depends_on or []) if d]
    for dep in deps:
        keys.split_key(dep)
        if not state.exists(dep):
            _warn(f"dependency does not exist yet: {dep}")
    now = util.now_iso()
    task = {
        "schema": state.SCHEMA, "key": key, "title": title,
        "parent_key": None if keys.is_project_key(parent_key) else parent_key,
        "depends_on": deps, **fields, "clickup": None, "session": None, "state": "scoped",
        "state_history": [{"state": "scoped", "at": now, "note": ""}],
        "gate": {"last_run": None, "passed": False, "reasons": [], "consecutive_blocks": 0},
        "handback": None, "created": now, "updated": now,
    }
    d = state.task_dir(key)
    (d / "issues").mkdir(parents=True, exist_ok=True)
    state.save(key, task)
    for kind in state.ITEM_KINDS:
        util.atomic_write_json(state.items_file(key, kind), {"schema": state.SCHEMA, "items": []})
    _write_stub(d / "spec.md", f"# {title}\n\n## Goal\n\n## Scope\n\n## Acceptance\n")
    _write_stub(d / "log.md", f"# Log — {key}\n")
    render.render_task(key)
    return task


def set_state(key: str, new_state: str, note: str = "", allow_handback: bool = False) -> dict:
    if new_state not in state.STATES:
        raise UsageError(f"unknown state {new_state!r}; one of {', '.join(state.STATES)}")
    if new_state == "handed-back" and not allow_handback:
        raise UsageError("handed-back is only reachable via `forge handback` (the gate must pass)")
    task = state.load_task(key)
    task["state"] = new_state
    task.setdefault("state_history", []).append({"state": new_state, "at": util.now_iso(), "note": note})
    return state.save(key, task)


def merge(key: str, patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise UsageError("--merge must be a JSON object")
    data = state.load(key)
    return state.save(key, util.deep_merge(data, patch))


def deps_satisfied(task: dict) -> tuple[bool, list[str]]:
    """(all done, list of unsatisfied dependency descriptions)."""
    missing = []
    for dep in task.get("depends_on") or []:
        if not state.exists(dep):
            missing.append(f"{dep} (missing)")
        elif (st := state.load_task(dep).get("state")) not in state.FINISHED:
            missing.append(f"{dep} ({st})")
    return not missing, missing


def ready(project: str | None = None) -> list[dict]:
    out = []
    for key in state.all_task_keys(project):
        task = state.load_task(key)
        if task.get("state") == "scoped" and deps_satisfied(task)[0]:
            out.append(task)
    return out


def task_for_session(session_id: str) -> str | None:
    for key in state.all_task_keys():
        session = state.load_task(key).get("session") or {}
        if session_id and session.get("id") == session_id:
            return key
    return None


def project_for_coordinator(session_id: str) -> str | None:
    for p in state.list_projects():
        if session_id and (p.get("coordinator_session") or {}).get("id") == session_id:
            return p["slug"]
    return None
