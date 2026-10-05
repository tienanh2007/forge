"""SonarCloud quality gate for a PR."""
import base64
import json
import urllib.parse

from forge_lib import util
from forge_lib.errors import IntegrationError

BASE = "https://sonarcloud.io"


def dashboard_url(project_key: str, pr_number: int) -> str:
    q = urllib.parse.urlencode({"id": project_key, "pullRequest": pr_number})
    return f"{BASE}/summary/new_code?{q}"


def quality_gate(project_key: str, pr_number: int) -> dict:
    token = util.get_config("SONAR_TOKEN")
    if not token:
        raise IntegrationError("SONAR_TOKEN is not set (env or ~/.config/forge/env)")
    q = urllib.parse.urlencode({"projectKey": project_key, "pullRequest": pr_number})
    auth = base64.b64encode(f"{token}:".encode()).decode()
    status, body = util.http_request("GET", f"{BASE}/api/qualitygates/project_status?{q}",
                                     headers={"Authorization": f"Basic {auth}"})
    result = {"status": "NONE", "conditions": [], "url": dashboard_url(project_key, pr_number)}
    if status == 404 or (status >= 400 and "not found" in body.lower()):
        return result
    if status != 200:
        raise IntegrationError(f"sonar quality gate {project_key} PR {pr_number}: HTTP {status}")
    try:
        ps = json.loads(body).get("projectStatus") or {}
    except json.JSONDecodeError as e:
        raise IntegrationError(f"sonar quality gate {project_key}: invalid JSON") from e
    result["status"] = ps.get("status") or "NONE"
    result["conditions"] = [{"metric": c.get("metricKey"), "status": c.get("status"),
                             "actual": c.get("actualValue"), "threshold": c.get("errorThreshold")}
                            for c in ps.get("conditions") or []]
    return result
