"""ClickUp REST v2."""
import json
import re
import urllib.parse

from forge_lib import util
from forge_lib.errors import IntegrationError

BASE = "https://api.clickup.com/api/v2"
CUSTOM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def parse_task_ref(ref: str) -> str:
    """Task id or custom id from a raw id or a ClickUp URL (…/t/<id> or …/t/<team>/<custom-id>)."""
    ref = ref.strip()
    if ref.startswith("http"):
        path = urllib.parse.urlparse(ref).path.rstrip("/")
        return path.rsplit("/", 1)[-1]
    return ref


def is_custom_id(ref: str) -> bool:
    return bool(CUSTOM_ID_RE.match(ref))


def _request(method: str, path: str, params: dict | None = None, payload: dict | None = None) -> dict:
    token = util.get_config("CLICKUP_TOKEN")
    if not token:
        raise IntegrationError("CLICKUP_TOKEN is not set (env or ~/.config/forge/env)")
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": token}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    status, text = util.http_request(method, url, headers=headers, body=body)
    if status >= 400:
        raise IntegrationError(f"ClickUp {method} {path}: HTTP {status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise IntegrationError(f"ClickUp {method} {path}: invalid JSON") from e


def team_id() -> str:
    configured = util.get_config("CLICKUP_TEAM_ID")
    if configured:
        return configured
    teams = _request("GET", "/team").get("teams") or []
    if not teams:
        raise IntegrationError("ClickUp: no teams visible to this token")
    return str(teams[0]["id"])


def get_task(task_id: str, include_subtasks: bool = True) -> dict:
    ref = parse_task_ref(task_id)
    params = {"include_subtasks": "true" if include_subtasks else "false"}
    if is_custom_id(ref):
        params.update({"custom_task_ids": "true", "team_id": team_id()})
    return _request("GET", f"/task/{urllib.parse.quote(ref)}", params)


def create_task(list_id: str, name: str, description: str = "", parent: str | None = None) -> dict:
    payload = {"name": name, "markdown_description": description}
    if parent:
        payload["parent"] = parent
    return _request("POST", f"/list/{urllib.parse.quote(str(list_id))}/task", payload=payload)


def update_status(task_id: str, status: str) -> dict:
    return _request("PUT", f"/task/{urllib.parse.quote(str(task_id))}", payload={"status": status})


def list_statuses(list_id: str) -> list[str]:
    data = _request("GET", f"/list/{urllib.parse.quote(str(list_id))}")
    return [s.get("status") for s in data.get("statuses") or [] if s.get("status")]


def status_of(task: dict) -> str | None:
    status = task.get("status")
    return status.get("status") if isinstance(status, dict) else status


def task_summary(task: dict) -> dict:
    return {
        "id": task.get("id"), "custom_id": task.get("custom_id"), "name": task.get("name"),
        "status": status_of(task),
        "url": task.get("url"),
        "assignees": [a.get("username") or a.get("email") for a in task.get("assignees") or []],
        "subtasks": [task_summary(s) for s in task.get("subtasks") or []],
    }


def ref_of(task: dict) -> dict:
    """The {id, custom_id, url} reference forge stores in project/task json."""
    return {"id": task.get("id"), "custom_id": task.get("custom_id"), "url": task.get("url")}
