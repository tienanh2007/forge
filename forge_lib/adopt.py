"""Propose a forge project for existing work (ClickUp tree + PRs + sessions). Read-only."""
import re
from pathlib import Path

from forge_lib import dispatch, util
from forge_lib.errors import ForgeError
from forge_lib.integrations import clickup, github


def slugify(text: str, max_len: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:max_len].strip("-") or "task"


def _prs_for(repo: str | None, custom_id: str | None, warnings: list[str]) -> list[dict]:
    if not repo or not custom_id:
        return []
    try:
        return github.search_prs(repo, f"{custom_id} in:title")
    except ForgeError as e:
        warnings.append(str(e))
        return []


def _candidates(sessions: list[dict], custom_id: str | None) -> list[dict]:
    if not custom_id:
        return []
    needle = custom_id.lower()
    return [s for s in sessions
            if needle in f"{s.get('name', '')} {s.get('cwd', '')}".lower()]


def scan(clickup_parent: str, repo: str | None = None, github_repo: str | None = None) -> dict:
    warnings: list[str] = []
    parent = clickup.get_task(clickup_parent, include_subtasks=True)
    try:
        sessions = dispatch.list_agents(include_all=True)
    except ForgeError as e:
        warnings.append(str(e))
        sessions = []
    tasks = []
    for n, sub in enumerate(parent.get("subtasks") or [], start=1):
        cid = sub.get("custom_id")
        tasks.append({
            "task_dir": f"{cid or f'T{n}'}-{slugify(sub.get('name'))}",
            "title": sub.get("name"), "clickup": clickup.ref_of(sub),
            "clickup_status": (sub.get("status") or {}).get("status") if isinstance(sub.get("status"), dict)
            else sub.get("status"),
            "prs": _prs_for(github_repo, cid, warnings),
            "candidate_sessions": _candidates(sessions, cid),
        })
    pid = parent.get("custom_id")
    return {
        "schema": 1,
        "project": {
            "slug": slugify(f"{pid or ''} {parent.get('name', '')}"), "title": parent.get("name"),
            "clickup_parent": clickup.ref_of(parent),
            "repos": [{"path": str(Path(repo).expanduser().resolve()) if repo else None, "github": github_repo,
                       "sonar_project_key": None, "base_branch": "main"}],
            "prs": _prs_for(github_repo, pid, warnings),
            "candidate_sessions": _candidates(sessions, pid),
        },
        "tasks": tasks,
        "sessions": sessions,
        "warnings": warnings,
        "generated": util.now_iso(),
    }
