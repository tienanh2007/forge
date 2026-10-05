#!/usr/bin/env python3
"""forge local web UI: JSON API over $FORGE_HOME + static SPA. Binds 127.0.0.1 only."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent
STATIC_DIR = UI_DIR / "static"
for _p in (str(REPO_ROOT), str(UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _fsread  # noqa: E402

PR_REFRESH_SECS = 120
CLICKUP_REFRESH_SECS = 300
AGENTS_TTL_SECS = 30
SUBPROCESS_TIMEOUT_SECS = 30
LOG_TAIL_LINES = 80
MAX_BODY_BYTES = 1 << 20
AGENT_TAIL_DEFAULT, AGENT_TAIL_MAX = 150, 1000
BG_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
BIND_HOST = "127.0.0.1"
ALLOWED_ORIGIN_HOSTS = ("127.0.0.1", "localhost")
SECRET_ENV_VARS = ("CLICKUP_TOKEN", "SONAR_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
INACTIVE_AGENT_STATUSES = {"stopped", "exited", "completed", "done", "failed", "terminated", "dead", "killed"}
FS_MAX_FILE_BYTES = 2 * 1024 * 1024
FS_MAX_ENTRIES = 20000
FS_EXCLUDED = {".cache", "__pycache__"}
RENDERED_SOURCES = {"PRs.md": "prs.json", "issues.md": "issues.json", "tests.md": "tests.json",
                    "HANDBACK.md": "task.json", "status.md": "task.json",
                    "decisions.md": "every task's issues.json"}
CLOSED_ISSUE_STATES = ("resolved", "wontfix")
AUTHORED_MD = {"spec.md", "plan.md", "project.md", "log.md", "tests.md"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def log(msg: str) -> None:
    print(f"[forge-ui {time.strftime('%H:%M:%S')}] {redact(msg)}", file=sys.stderr, flush=True)


def redact(text: str) -> str:
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 6:
            text = text.replace(value, "***")
    return text


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Backend:
    """forge_lib when importable, else the private _fsread fallback."""

    def __init__(self, nudge_enabled: bool):
        self.nudge_enabled = nudge_enabled
        try:
            from forge_lib import state
            self.state, self.lib_state = state, True
        except ImportError:
            self.state, self.lib_state = _fsread, False
        try:
            from forge_lib import inbox
            self.inbox = inbox
        except ImportError:
            self.inbox = None
        try:
            from forge_lib import issues
            self.issues = issues
        except ImportError:
            self.issues = None
        try:
            from forge_lib import prs
            self.prs = prs
        except ImportError:
            self.prs = None
        try:
            from forge_lib import dispatch
            self.dispatch = dispatch
        except ImportError:
            self.dispatch = None
        try:
            from forge_lib import agents, errors
            self.agents, self.errors = agents, errors
        except ImportError:
            self.agents = self.errors = None
        try:
            from forge_lib.integrations import clickup
            self.clickup = clickup
        except ImportError:
            self.clickup = None
        self.refresh_lock = threading.Lock()
        self.clickup_lock = threading.Lock()
        self.clickup_cache = self._load_clickup_cache()
        self.agents_cache = (0.0, None)
        log(f"backend: state={'forge_lib' if self.lib_state else '_fsread fallback'} "
            f"inbox={'forge_lib' if self.inbox else 'fallback'} prs={'forge_lib' if self.prs else 'unavailable'} "
            f"clickup={'forge_lib' if self.clickup else 'unavailable'} home={self.home()}")

    # ---- paths / validation -------------------------------------------------
    def home(self) -> Path:
        return Path(self.state.forge_home())

    def check_key(self, key: str | None, *, project: bool = False, required: bool = True) -> str | None:
        if not key:
            if required:
                raise ApiError(400, "missing key")
            return None
        try:
            segments = _fsread.split_key(key)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        if project and len(segments) != 1:
            raise ApiError(400, "expected a project slug")
        home = self.home().resolve()
        path = self.key_dir(key).resolve()
        if not path.is_relative_to(home):
            raise ApiError(400, "key escapes FORGE_HOME")
        marker = "project.json" if len(segments) == 1 else "task.json"
        if not (path / marker).is_file():
            raise ApiError(404, f"not found: {key}")
        return key

    def key_dir(self, key: str) -> Path:
        return _fsread.task_dir(key, self.home())

    # ---- reads ---------------------------------------------------------------
    def items(self, key: str, kind: str) -> list[dict]:
        return self.state.load_json_items(key, kind) or []

    def tree(self, key: str | None = None):
        node = self.state.tree(key)
        for root in node if isinstance(node, list) else [node]:
            for child in self.walk(root):
                if "depends_on" not in child:  # not part of the contract node shape; the UI needs it
                    try:
                        child["depends_on"] = self.state.load_task(child["key"]).get("depends_on", [])
                    except Exception:
                        child["depends_on"] = []
        return node

    @staticmethod
    def walk(node: dict):
        for child in node.get("children") or []:
            yield child
            yield from Backend.walk(child)

    def project_keys(self, project: str | None) -> list[str]:
        slugs = [project] if project else [p.get("slug") for p in self.state.list_projects() if p.get("slug")]
        return slugs

    def task_keys(self, project: str | None) -> list[tuple[str, dict]]:
        out = []
        for slug in self.project_keys(project):
            try:
                node = self.tree(slug)
            except Exception as exc:  # a broken project must not break the whole listing
                log(f"tree({slug}) failed: {exc}")
                continue
            out.extend((n["key"], n) for n in self.walk(node))
        return out

    def project_summary(self, project: dict) -> dict:
        slug = project.get("slug")
        states: dict[str, int] = {}
        counts = {"tasks": 0, "open_issues": 0, "awaiting_user": 0, "open_inbox": 0, "prs": 0, "prs_green": 0, "prs_failing": 0,
                  "prs_pending": 0, "tests": 0, "tests_green": 0}
        for key, node in self.task_keys(slug):
            counts["tasks"] += 1
            states[node.get("state") or "unknown"] = states.get(node.get("state") or "unknown", 0) + 1
            c = node.get("counts") or {}
            for field in ("open_issues", "awaiting_user", "open_inbox", "tests", "tests_green"):
                counts[field] += int(c.get(field) or 0)
            for pr in self.items(key, "prs"):
                counts["prs"] += 1
                health = pr_health(pr)
                counts[f"prs_{health}"] = counts.get(f"prs_{health}", 0) + 1
        return {**project, "counts": counts, "task_states": states,
                "clickup": (self.clickup_cache.get(slug) or {}).get("summary")}

    # ---- clickup -------------------------------------------------------------
    def _clickup_cache_path(self) -> Path:
        return self.home() / ".cache" / "clickup.json"

    def _load_clickup_cache(self) -> dict:
        data = _fsread.read_json(self._clickup_cache_path(), {}) or {}
        return data.get("parents", {}) if isinstance(data, dict) else {}

    def refresh_clickup(self, only_slug: str | None = None) -> None:
        if not self.clickup:
            return
        with self.clickup_lock:
            for project in self.state.list_projects():
                slug, parent = project.get("slug"), project.get("clickup_parent") or {}
                if not parent.get("id") or (only_slug and slug != only_slug):
                    continue
                try:
                    raw = self.clickup.get_task(parent["id"], include_subtasks=True)
                    self.clickup_cache[slug] = {"summary": self.clickup.task_summary(raw),
                                                "fetched_at": _fsread.now_iso()}
                except Exception as exc:
                    log(f"clickup refresh failed for {slug}: {exc}")
            try:
                _fsread.write_json_atomic(self._clickup_cache_path(), {"schema": 1, "parents": self.clickup_cache})
            except OSError as exc:
                log(f"writing clickup cache failed: {exc}")

    # ---- prs -----------------------------------------------------------------
    def refresh_prs(self, key: str | None) -> None:
        if not self.prs:
            raise ApiError(503, "forge_lib.prs not available; cannot refresh PR status")
        with self.refresh_lock:
            self.prs.refresh(key)

    # ---- sessions ------------------------------------------------------------
    def active_sessions(self, fresh: bool = False) -> set[str] | None:
        """Session ids listed by `claude agents --json` as live; None when unknown."""
        stamp, cached = self.agents_cache
        if not fresh and time.time() - stamp < AGENTS_TTL_SECS:
            return cached
        result = None
        list_agents = getattr(self.dispatch, "list_agents", None) if self.dispatch else None
        if self.nudge_enabled and list_agents:
            try:
                result = parse_active_agents(list_agents())
            except Exception as exc:
                log(f"list_agents failed: {exc}")
        elif self.nudge_enabled and shutil.which("claude"):
            try:
                proc = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True,
                                      timeout=SUBPROCESS_TIMEOUT_SECS)
                if proc.returncode == 0:
                    result = parse_active_agents(json.loads(proc.stdout or "[]"))
                else:
                    log(f"claude agents --json exited {proc.returncode}")
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                log(f"claude agents --json failed: {exc}")
        self.agents_cache = (time.time(), result)
        return result

    # ---- inbox ---------------------------------------------------------------
    def add_inbox(self, key: str, body: str, issue_id: str | None) -> dict:
        if self.inbox:
            msg = self.inbox.add(key, body, issue_id=issue_id, author="user", reply_to=None)
        else:
            msg = _fsread.inbox_add(key, body, issue_id=issue_id, author="user")
        msg = dict(msg)
        msg["nudge"] = self.nudge(key, msg)
        if msg["nudge"].get("delivered"):
            msg["delivered"] = True
        return msg

    def nudge(self, key: str, msg: dict) -> dict:
        if not self.nudge_enabled:
            return {"attempted": False, "reason": "nudging disabled"}
        session = (self.state.load_task(key) or {}).get("session")
        if not session or not session.get("id"):
            return {"attempted": False, "reason": "task has no session; left for coordinator relay"}
        about = f"issue {msg.get('issue_id')}" if msg.get("issue_id") else "the task"
        text = (f"New user comment {msg.get('id')} on {about}: {msg.get('body')}\n"
                f"Check your inbox (forge inbox list {key} --open), then ack/resolve it.")
        if self.agents:
            live = self.agents.live_agents()
            if live is None:
                return {"attempted": False, "reason": "could not determine session activity; left for relay"}
            agent = self.agents.find_agent(live, session)
            if agent:
                ok, detail = self.agents.send_live(agent.get("name") or session.get("name"), text,
                                                   self.state.load_task(key).get("repo"))
                if not ok:
                    return {"attempted": True, "delivered": False,
                            "reason": f"live bridge failed ({redact(detail)[:200]}); left for relay"}
                self.mark_delivered(key, msg.get("id"))
                return {"attempted": True, "delivered": True, "reason": "sent to live session"}
        else:
            active = self.active_sessions(fresh=True)
            if active is None:
                return {"attempted": False, "reason": "could not determine session activity; left for relay"}
            if session["id"] in active:
                return {"attempted": False, "reason": f"session {session.get('name')} is active; left for relay"}
        cmd = forge_cmd()
        if not cmd:
            return {"attempted": False, "reason": "forge CLI not found"}
        try:
            proc = subprocess.run(cmd + ["message", key, text], capture_output=True, text=True,
                                  timeout=SUBPROCESS_TIMEOUT_SECS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"forge message {key} failed: {exc}")
            return {"attempted": True, "delivered": False, "reason": "forge message failed to run"}
        if proc.returncode != 0:
            log(f"forge message {key} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
            return {"attempted": True, "delivered": False, "exit_code": proc.returncode,
                    "reason": redact((proc.stdout + proc.stderr).strip()[:300])}
        self.mark_delivered(key, msg.get("id"))
        return {"attempted": True, "delivered": True, "reason": "resumed session with message"}

    def mark_delivered(self, key: str, comment_id: str) -> None:
        fn = getattr(self.inbox, "mark_delivered", None) if self.inbox else None
        try:
            if fn:
                fn(key, comment_id)
            else:
                _fsread.mark_delivered(key, comment_id)
        except Exception as exc:
            log(f"mark_delivered {key} {comment_id} failed: {exc}")


def parse_active_agents(data) -> set[str]:
    entries = data if isinstance(data, list) else (data.get("agents") or data.get("items") or []) \
        if isinstance(data, dict) else []
    active = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or entry.get("state") or "").lower()
        if status in INACTIVE_AGENT_STATUSES:
            continue
        for field in ("session_id", "sessionId", "id", "session"):
            value = entry.get(field)
            if isinstance(value, dict):
                value = value.get("id")
            if isinstance(value, str):
                active.add(value)
    return active


def forge_cmd() -> list[str] | None:
    local = REPO_ROOT / "bin" / "forge"
    if local.is_file():
        return [sys.executable, str(local)]
    found = shutil.which("forge")
    return [found] if found else None


def pr_health(pr: dict) -> str:
    """green | failing | pending for one prs.json item (from cached status)."""
    status = pr.get("status") or {}
    checks = (status.get("checks") or {}).get("state")
    unresolved = (status.get("threads") or {}).get("unresolved") or 0
    sonar = (status.get("sonar") or {}).get("status")
    if checks in ("FAILURE", "ERROR") or sonar == "ERROR":
        return "failing"
    if checks == "SUCCESS" and unresolved == 0 and sonar in (None, "OK"):
        return "green"
    return "pending"


def tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])
    except FileNotFoundError:
        return ""


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def one(qs: dict, name: str) -> str | None:
    values = qs.get(name)
    return values[0] if values else None


# ---- files view (read-only, confined to FORGE_HOME) ------------------------------

def fs_resolve(home: Path, rel: str | None) -> tuple[Path, str]:
    """(absolute path, normalized rel) for a FORGE_HOME-relative path; ApiError on anything suspicious."""
    rel = (rel or "").strip()
    if "\x00" in rel or "\\" in rel or rel.startswith("/") or Path(rel).is_absolute():
        raise ApiError(400, "path must be relative to FORGE_HOME")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") or p in FS_EXCLUDED for p in parts):
        raise ApiError(400, "path not allowed")
    root = home.resolve()
    target = root.joinpath(*parts)
    try:
        resolved = target.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ApiError(404, f"not found: {'/'.join(parts)}") from None
    if not resolved.is_relative_to(root):
        raise ApiError(400, "path escapes FORGE_HOME")
    return resolved, "/".join(parts)


def fs_kind(path: Path) -> str:
    """state | rendered | authored | other."""
    name = path.name
    if path.is_dir():
        return "other"
    if path.suffix == ".json":
        return "state"
    if name == "tests.md":  # rendered in a task folder, authored scoping doc at project level
        return "rendered" if (path.parent / "task.json").exists() else "authored"
    if name in RENDERED_SOURCES:
        return "rendered"
    if name in AUTHORED_MD or (path.parent.name == "issues" and path.suffix == ".md"):
        return "authored"
    return "other"


def fs_source(path: Path) -> str | None:
    """The json a rendered file is generated from."""
    if fs_kind(path) != "rendered":
        return None
    if path.name == "status.md":
        return "task.json + tests/prs/issues/inbox json" if (path.parent / "task.json").exists() \
            else "project.json + task json"
    return RENDERED_SOURCES[path.name]


def fs_role(path: Path) -> str:
    if (path / "project.json").is_file():
        return "project"
    if (path / "task.json").is_file():
        return "task"
    return "dir"


def iso_mtime(st: os.stat_result) -> str:
    return datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fs_node(path: Path, rel: str, budget: list[int]) -> dict:
    st = path.stat()
    if not path.is_dir():
        return {"name": path.name, "rel": rel, "type": "file", "size": st.st_size, "mtime": iso_mtime(st),
                "kind": fs_kind(path)}
    children = []
    for entry in sorted(path.iterdir(), key=lambda e: e.name.lower()):
        if entry.name.startswith(".") or entry.name in FS_EXCLUDED or entry.is_symlink():
            continue
        budget[0] -= 1
        if budget[0] < 0:
            raise ApiError(413, f"tree too large (> {FS_MAX_ENTRIES} entries); request a subfolder")
        children.append(fs_node(entry, f"{rel}/{entry.name}" if rel else entry.name, budget))
    return {"name": path.name if rel else "", "rel": rel, "type": "dir", "size": None, "mtime": iso_mtime(st),
            "kind": "other", "role": fs_role(path), "children": children}


def api_fs_tree(be: Backend, qs, body):
    path, rel = fs_resolve(be.home(), one(qs, "path"))
    if not path.is_dir():
        raise ApiError(400, f"not a directory: {rel}")
    return fs_node(path, rel, [FS_MAX_ENTRIES])


def api_fs_file(be: Backend, qs, body):
    path, rel = fs_resolve(be.home(), one(qs, "path"))
    if not path.is_file():
        raise ApiError(400, f"not a file: {rel}")
    st = path.stat()
    out = {"rel": rel, "size": st.st_size, "mtime": iso_mtime(st), "kind": fs_kind(path), "source": fs_source(path)}
    if one(qs, "meta"):
        return out
    if st.st_size > FS_MAX_FILE_BYTES:
        raise ApiError(413, f"file too large to view ({st.st_size} bytes > {FS_MAX_FILE_BYTES})")
    try:
        out["content"] = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(415, "not a UTF-8 text file") from exc
    return out


# ---- route handlers ----------------------------------------------------------

def api_projects(be: Backend, qs, body):
    return [be.project_summary(p) for p in be.state.list_projects()]


def api_tree(be: Backend, qs, body):
    return be.tree(be.check_key(one(qs, "key"), required=False))


def api_project(be: Backend, qs, body):
    slug = be.check_key(one(qs, "slug"), project=True)
    active = be.active_sessions()
    return {"project": be.state.load_project(slug), "tree": be.tree(slug),
            "clickup": (be.clickup_cache.get(slug) or {}).get("summary"),
            "clickup_fetched_at": (be.clickup_cache.get(slug) or {}).get("fetched_at"),
            "active_sessions": sorted(active) if active is not None else None}


def api_task(be: Backend, qs, body):
    key = be.check_key(one(qs, "key"))
    if "/" not in key:
        raise ApiError(400, "expected a task key, got a project slug")
    base = be.key_dir(key)
    active = be.active_sessions()
    return {"task": be.state.load_task(key), "tests": be.items(key, "tests"), "prs": be.items(key, "prs"),
            "issues": be.items(key, "issues"), "inbox": be.items(key, "inbox"),
            "spec_md": read_text(base / "spec.md"), "log_tail": tail(base / "log.md", LOG_TAIL_LINES),
            "tree": be.tree(key), "active_sessions": sorted(active) if active is not None else None,
            "task_states": list(getattr(be.state, "STATES", ()))}


def api_prs(be: Backend, qs, body):
    project = be.check_key(one(qs, "project"), project=True, required=False)
    out = []
    for key, node in be.task_keys(project):
        for pr in be.items(key, "prs"):
            out.append({"task_key": key, "task_title": node.get("title"), "task_state": node.get("state"),
                        "project": key.split("/")[0], "health": pr_health(pr), **pr})
    return out


def api_issues(be: Backend, qs, body):
    project = be.check_key(one(qs, "project"), project=True, required=False)
    status, severity, st = one(qs, "status"), one(qs, "severity"), one(qs, "state")
    out = []
    for key, node in be.task_keys(project):
        for issue in be.items(key, "issues"):
            if status and issue.get("status") != status:
                continue
            if st == "active" and issue.get("state", issue.get("status")) in CLOSED_ISSUE_STATES:
                continue
            if st and st not in ("active", "all") and issue.get("state") != st:
                continue
            if severity and issue.get("severity") != severity:
                continue
            out.append({"task_key": key, "task_title": node.get("title"), "project": key.split("/")[0], **issue})
    return out


def api_issue(be: Backend, qs, body):
    key = be.check_key(one(qs, "key"))
    issue_id = one(qs, "id") or ""
    if not _fsread.ISSUE_ID_RE.match(issue_id):
        raise ApiError(400, "invalid issue id")
    issue = next((i for i in be.items(key, "issues") if i.get("id") == issue_id), None)
    if issue is None:
        raise ApiError(404, f"issue {issue_id} not found on {key}")
    inbox = be.items(key, "inbox")
    ids = {m.get("id") for m in inbox if m.get("issue_id") == issue_id}
    grew = True
    while grew:  # include replies to thread messages even if they lack issue_id
        extra = {m.get("id") for m in inbox if m.get("reply_to") in ids} - ids
        grew = bool(extra)
        ids |= extra
    task = be.state.load_task(key)
    next_states = be.issues.next_states(issue.get("state")) if be.issues else []
    return {"issue": issue, "next_states": next_states, "body_md": be.state.read_issue_body(key, issue_id),
            "thread": [m for m in inbox if m.get("id") in ids],
            "task": {"key": key, "title": task.get("title"), "state": task.get("state"),
                     "session": task.get("session")}}


def api_post_inbox(be: Backend, qs, body):
    key = be.check_key(body.get("key"))
    if "/" not in key:
        raise ApiError(400, "inbox messages attach to a task key")
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "body is required")
    issue_id = body.get("issue_id") or None
    if issue_id is not None:
        if not isinstance(issue_id, str) or not _fsread.ISSUE_ID_RE.match(issue_id):
            raise ApiError(400, "invalid issue_id")
        if not any(i.get("id") == issue_id for i in be.items(key, "issues")):
            raise ApiError(404, f"issue {issue_id} not found on {key}")
    return be.add_inbox(key, text.strip(), issue_id)


def _issue_target(be: Backend, body: dict) -> tuple[str, str]:
    if not be.issues or not be.lib_state:
        raise ApiError(503, "forge_lib.issues not available")
    key = be.check_key(body.get("key"))
    if "/" not in key:
        raise ApiError(400, "issues attach to a task key")
    issue_id = body.get("id")
    if not isinstance(issue_id, str) or not _fsread.ISSUE_ID_RE.match(issue_id):
        raise ApiError(400, "invalid issue id")
    if not any(i.get("id") == issue_id for i in be.items(key, "issues")):
        raise ApiError(404, f"issue {issue_id} not found on {key}")
    return key, issue_id


def _issue_call(be: Backend, fn, *args, **kw):
    from forge_lib import errors
    try:
        return fn(*args, **kw)
    except errors.NotFound as exc:
        raise ApiError(404, redact(str(exc))) from exc
    except errors.ForgeError as exc:
        raise ApiError(409, redact(str(exc))) from exc


def api_post_issue_decide(be: Backend, qs, body):
    key, issue_id = _issue_target(be, body)
    text = body.get("decision")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "decision is required")
    return _issue_call(be, be.issues.decide, key, issue_id, text.strip(), by="user", notify=True,
                       deliver=be.nudge_enabled)


def api_post_issue_state(be: Backend, qs, body):
    key, issue_id = _issue_target(be, body)
    st, note = body.get("state"), body.get("note") or ""
    if not isinstance(st, str) or not isinstance(note, str):
        raise ApiError(400, "state (and optional note) must be strings")
    return _issue_call(be, be.issues.set_state, key, issue_id, st, note=note.strip(), by="user")


def api_post_task_state(be: Backend, qs, body):
    if not be.lib_state:
        raise ApiError(503, "forge_lib.state not available")
    key = be.check_key(body.get("key"))
    if "/" not in key:
        raise ApiError(400, "expected a task key")
    st, note = body.get("state"), body.get("note") or ""
    if not isinstance(st, str) or not isinstance(note, str):
        raise ApiError(400, "state (and optional note) must be strings")
    if st not in be.state.STATES:
        raise ApiError(400, f"unknown state {st!r}")
    note = note.strip()
    if st == "handed-back":  # the UI is the one path that bypasses `forge handback`'s gate
        note = "via UI (gate skipped)" + (f": {note}" if note else "")
    from forge_lib import tasks
    before = be.state.load_task(key).get("clickup_sync")
    task = _issue_call(be, tasks.set_state, key, st, note or "set via UI", allow_handback=True)
    sync = task.get("clickup_sync")
    return {"task": task, "clickup_sync": sync, "clickup_pushed": sync is not None and sync != before}


def api_post_refresh(be: Backend, qs, body):
    key = be.check_key(body.get("key"), required=False)
    started = time.time()
    try:
        be.refresh_prs(key)
    except ApiError:
        raise
    except Exception as exc:
        log(f"prs.refresh({key}) failed: {exc}")
        raise ApiError(502, f"PR refresh failed: {redact(str(exc))[:500]}") from exc
    if key is None or "/" not in key:
        be.refresh_clickup(key)
    return {"ok": True, "key": key, "seconds": round(time.time() - started, 2), "at": _fsread.now_iso()}


# ---- agents -------------------------------------------------------------------

def need_agents(be: Backend):
    if not be.agents:
        raise ApiError(503, "forge_lib.agents not available")
    return be.agents


def api_agents(be: Backend, qs, body):
    return need_agents(be).overview(be.check_key(one(qs, "project"), project=True, required=False))


def api_agent(be: Backend, qs, body):
    agents = need_agents(be)
    key = be.check_key(one(qs, "key"))
    try:
        limit = max(1, min(int(one(qs, "limit") or AGENT_TAIL_DEFAULT), AGENT_TAIL_MAX))
    except ValueError as exc:
        raise ApiError(400, "limit must be an integer") from exc
    return agents.detail(key, limit)


def api_post_agent_message(be: Backend, qs, body):
    agents = need_agents(be)
    key = be.check_key(body.get("key"))
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "text is required")
    if not be.nudge_enabled:
        raise ApiError(409, "messaging disabled (--no-nudge)")
    try:
        return agents.send(key, text.strip())
    except be.errors.ForgeError as exc:
        raise ApiError(409, redact(str(exc))) from exc


def api_post_agent_open(be: Backend, qs, body):
    agents = need_agents(be)
    key = be.check_key(body.get("key"))
    r = agents.row(key, agents.live_agents())
    bg_id = r.get("bg_id")
    if r.get("session_kind") != "background" or not bg_id or not BG_ID_RE.match(str(bg_id)):
        raise ApiError(409, "only background sessions with a bg id can be attached; "
                            "use the message box (or the session's own terminal) instead")
    if not be.nudge_enabled:
        raise ApiError(409, "disabled (--no-nudge)")
    cmd = f"claude attach {bg_id}"
    try:
        proc = subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{cmd}"',
                               "-e", 'tell application "Terminal" to activate'],
                              capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(502, f"osascript failed: {exc}") from exc
    if proc.returncode != 0:
        raise ApiError(502, f"osascript exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return {"ok": True, "command": cmd}


def api_post_agent_dispatch(be: Backend, qs, body):
    need_agents(be)
    key = be.check_key(body.get("key"))
    if "/" not in key:
        raise ApiError(400, "expected a task key")
    if (be.state.load_task(key) or {}).get("session"):
        raise ApiError(409, "task already has a session; message it instead")
    if not be.nudge_enabled:
        raise ApiError(409, "dispatch disabled (--no-nudge)")
    try:
        r = be.dispatch.dispatch(key)
    except be.errors.ForgeError as exc:
        raise ApiError(502, redact(str(exc))[:500]) from exc
    return {"code": r.code, "output": r.output, "dispatched": r.code == 0, **r.data}


GET_ROUTES = {"/api/projects": api_projects, "/api/tree": api_tree, "/api/project": api_project,
              "/api/task": api_task, "/api/prs": api_prs, "/api/issues": api_issues, "/api/issue": api_issue,
              "/api/agents": api_agents, "/api/agent": api_agent,
              "/api/fs/tree": api_fs_tree, "/api/fs/file": api_fs_file}
POST_ROUTES = {"/api/inbox": api_post_inbox, "/api/refresh": api_post_refresh,
               "/api/issue/decide": api_post_issue_decide, "/api/issue/state": api_post_issue_state,
               "/api/task/state": api_post_task_state,
               "/api/agent/message": api_post_agent_message, "/api/agent/open": api_post_agent_open,
               "/api/agent/dispatch": api_post_agent_dispatch}


class Handler(BaseHTTPRequestHandler):
    server_version = "forge-ui"
    backend: Backend = None  # set in main()

    def log_message(self, fmt, *args):
        if os.environ.get("FORGE_UI_ACCESS_LOG"):
            log(fmt % args)

    def _send(self, status: int, payload: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

    def _dispatch(self, routes: dict, body: dict) -> None:
        url = urlparse(self.path)
        handler = routes.get(url.path)
        if handler is None:
            self._json(404, {"error": f"no such endpoint: {url.path}"})
            return
        try:
            self._json(200, handler(self.backend, parse_qs(url.query), body))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": redact(str(exc))})
        except ValueError as exc:
            self._json(400, {"error": redact(str(exc))})
        except Exception as exc:
            log(f"{self.command} {url.path} failed:\n{traceback.format_exc()}")
            self._json(500, {"error": f"internal error: {redact(str(exc))[:300]}"})

    def _local_request(self) -> bool:
        """Refuse cross-origin callers and non-local Host headers (DNS rebinding) on file reads."""
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ALLOWED_ORIGIN_HOSTS:
            return False
        host = self.headers.get("Host") or ""
        return urlparse(f"//{host}").hostname in ALLOWED_ORIGIN_HOSTS

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/fs/") and not self._local_request():
            self._json(403, {"error": "cross-origin or non-local request refused"})
            return
        if path.startswith("/api/"):
            self._dispatch(GET_ROUTES, {})
        else:
            self._static(path)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ALLOWED_ORIGIN_HOSTS:
            self._json(403, {"error": "cross-origin request refused"})
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413 if length > 0 else 400, {"error": "bad Content-Length"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "JSON body must be an object"})
            return
        self._dispatch(POST_ROUTES, body)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))


def poller(be: Backend, stop: threading.Event) -> None:
    last_pr = last_cu = 0.0
    while not stop.is_set():
        now = time.time()
        if be.prs and now - last_pr >= PR_REFRESH_SECS:
            last_pr = now
            try:
                be.refresh_prs(None)
            except Exception as exc:
                log(f"background PR refresh failed: {exc}")
        if be.clickup and now - last_cu >= CLICKUP_REFRESH_SECS:
            last_cu = now
            try:
                be.refresh_clickup()
            except Exception as exc:
                log(f"background ClickUp refresh failed: {exc}")
        stop.wait(5)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="forge local web UI")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-poll", action="store_true", help="disable background PR/ClickUp refresh")
    parser.add_argument("--no-nudge", action="store_true", help="never resume sessions on new comments")
    parser.add_argument("--fixtures", action="store_true",
                        help="build a fixture FORGE_HOME in a temp dir and serve it (implies --no-poll --no-nudge)")
    args = parser.parse_args(argv)

    if args.fixtures:
        import dev_fixtures
        home = dev_fixtures.build(Path(tempfile.mkdtemp(prefix="forge-fixtures-")))
        os.environ["FORGE_HOME"] = str(home)
        args.no_poll = args.no_nudge = True
        log(f"fixture mode: FORGE_HOME={home}")

    Handler.backend = Backend(nudge_enabled=not args.no_nudge)
    stop = threading.Event()
    if not args.no_poll:
        threading.Thread(target=poller, args=(Handler.backend, stop), daemon=True, name="forge-poller").start()
    httpd = ThreadingHTTPServer((BIND_HOST, args.port), Handler)
    httpd.daemon_threads = True
    log(f"serving http://{BIND_HOST}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
