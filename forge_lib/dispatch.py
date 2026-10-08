"""Start / resume the background Claude Code worker session that owns a task."""
import json
import shlex
import time
from dataclasses import dataclass, field

from forge_lib import inbox, keys, state, tasks, util
from forge_lib.errors import IntegrationError, UsageError

EXIT_OK, EXIT_REFUSED, EXIT_ACTIVE = 0, 1, 3
# "done"/"idle" = turn finished but the process is alive and reachable via SendMessage; resuming it would fork.
INACTIVE_STATES = {"failed", "stopped", "exited", "killed", "errored"}
# Re-dispatch keeps these states: the worker is mid-flight and owns them.
KEEP_STATES = ("in-progress", "in-review", "coordinating")
# Background sessions take a moment to appear in `claude agents --json`.
RESOLVE_ATTEMPTS, RESOLVE_DELAY_S = 10, 1.5
# Workers run in a different permission-mode class than the UI bridge and coordinators, so Claude Code
# would hold their inbound messages for manual approval; accept them for worker sessions only.
WORKER_SETTINGS = {"crossSessionInbound": "accept"}


@dataclass
class Result:
    code: int
    output: str
    cmd: list[str] | None = None
    cwd: str | None = None
    data: dict = field(default_factory=dict)


def session_for(key: str) -> dict:
    """Name/worktree are deterministic; `id` is filled from `claude agents` after launch
    (`claude --bg` ignores --session-id and assigns its own)."""
    name = "forge-" + key.replace("/", "-")
    return {"id": None, "name": name, "worktree": name}


def worker_prompt(key: str, message: str | None = None) -> str:
    prompt = (f"You are the forge worker for task {key}. Task folder: {state.task_dir(key)}. "
              "Invoke the forge:work skill and follow it.")
    return prompt + (f"\n\n{message}" if message else "")


def permission_mode(key: str) -> str | None:
    """The project's worker permission mode; projects created before the setting get the default.
    "default" (or empty) launches without the flag, i.e. Claude Code's own default mode."""
    mode = state.load_project(keys.project_of(key)).get("permission_mode", tasks.DEFAULT_PERMISSION_MODE)
    return None if mode in (None, "", "default") else mode


def _bg(key: str, session: dict) -> list[str]:
    mode = permission_mode(key)
    return (["claude", "--bg"] + (["--permission-mode", mode] if mode else [])
            + ["--settings", json.dumps(WORKER_SETTINGS), "-n", session["name"]])


def first_command(key: str, session: dict, message: str | None = None) -> list[str]:
    return _bg(key, session) + ["-w", session["worktree"], worker_prompt(key, message)]


def resume_command(key: str, session: dict, message: str | None = None) -> list[str]:
    return _bg(key, session) + ["--resume", session["id"], message or f"Resume task {key}; check inbox."]


def list_agents(include_all: bool = False) -> list[dict]:
    """`claude agents --json [--all]`; raises IntegrationError on failure."""
    proc = util.run(["claude", "agents", "--json"] + (["--all"] if include_all else []), timeout=60)
    if proc.returncode != 0:
        raise IntegrationError(f"claude agents failed: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise IntegrationError("claude agents: invalid JSON") from e
    return data if isinstance(data, list) else []


def _matches(agent: dict, session: dict) -> bool:
    sid = session.get("id")
    return bool(sid) and sid in (agent.get("sessionId"), agent.get("id"))


def is_active(session: dict) -> bool:
    """True if `claude agents --json` (active sessions only) lists it. Unknown -> False."""
    try:
        agents = list_agents()
    except IntegrationError:
        return False
    for a in agents:
        if _matches(a, session):
            return str(a.get("state") or a.get("status") or "").lower() not in INACTIVE_STATES
    return False


def resolve_session_id(session: dict, attempts: int = RESOLVE_ATTEMPTS, delay: float = RESOLVE_DELAY_S) -> dict:
    """Find the newest agent named session['name'] and record its real sessionId."""
    for i in range(attempts):
        try:
            named = [a for a in list_agents(include_all=True) if a.get("name") == session["name"]]
        except IntegrationError:
            named = []
        if named:
            newest = max(named, key=lambda a: a.get("startedAt") or 0)
            return {**session, "id": newest.get("sessionId") or newest.get("id"), "bg_id": newest.get("id")}
        if i < attempts - 1:
            time.sleep(delay)
    return session


def refusal(key: str, task: dict) -> str | None:
    ok, missing = tasks.deps_satisfied(task)
    if not ok:
        return "dependencies not handed-back/done: " + ", ".join(missing)
    project = state.load_project(keys.project_of(key))
    limit = int(project.get("max_parallel") or 0)
    running = [k for k in state.all_task_keys(project["slug"])
               if k != key and state.load_task(k).get("state") in state.RUNNING]
    if limit and len(running) >= limit:
        return f"project already has {len(running)} running task(s) (max_parallel={limit}): " + ", ".join(running)
    return None


def _run_claude(cmd: list[str], cwd: str | None) -> None:
    proc = util.run(cmd, cwd=cwd, timeout=120)
    if proc.returncode != 0:
        raise IntegrationError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")


def dispatch(key: str, message: str | None = None, dry_run: bool = False, force: bool = False,
             require_session: bool = False) -> Result:
    task = state.load_task(key)
    session = task.get("session")
    cwd = task.get("repo")
    if not session:
        if require_session:
            raise UsageError(f"task {key} has never been dispatched; use `forge dispatch`")
        if not force and (why := refusal(key, task)):
            return Result(EXIT_REFUSED, f"REFUSED: {why}")
        session = session_for(key)
        cmd = first_command(key, session, message)
    else:
        if is_active(session):
            text = f"ACTIVE: use SendMessage to {session['name']}"
            if not dry_run:
                inbox.add(key, message or f"Resume task {key}; check inbox.", author="coordinator")
            return Result(EXIT_ACTIVE, text, data={"session": session})
        cmd = resume_command(key, session, message)
    if dry_run:
        return Result(EXIT_OK, shlex.join(cmd), cmd=cmd, cwd=cwd, data={"session": session, "dry_run": True})
    if not cwd:
        raise UsageError(f"task {key} has no repo to run in")
    _run_claude(cmd, cwd)
    session = resolve_session_id(session)
    task = state.load_task(key)
    task["session"] = session
    state.save(key, task)
    if task.get("state") not in KEEP_STATES:
        tasks.set_state(key, "dispatched", note="resumed" if "--resume" in cmd else "first dispatch")
    return Result(EXIT_OK, f"DISPATCHED {key} as {session['name']} ({session['id'] or 'session id unresolved'})", cmd=cmd, cwd=cwd,
                  data={"session": session})


def sessions() -> list[dict]:
    """All Claude sessions (incl. completed) with the forge task key they belong to, if any."""
    by_id = {}
    for key in state.all_task_keys():
        s = state.load_task(key).get("session")
        if s:
            by_id[s["id"]] = key
    coordinators = {(p.get("coordinator_session") or {}).get("id"): p["slug"] for p in state.list_projects()}
    out = []
    for a in list_agents(include_all=True):
        sid = a.get("sessionId") or a.get("id")
        out.append({**a, "task_key": by_id.get(sid), "coordinator_of": coordinators.get(sid)})
    return out
