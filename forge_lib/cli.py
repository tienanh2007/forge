"""`forge` command line."""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from forge_lib import (adopt, agents, dispatch, gate, hooks, inbox, issues, keys, prs, render, state, tasks,
                       tests_store, util)
from forge_lib.errors import ForgeError, UsageError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _out(args, data, text=None) -> None:
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(text)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# --- command handlers: each returns an exit code (None = 0) ---

def cmd_home(args):
    print(state.forge_home())


def cmd_init_project(args):
    p = tasks.init_project(args.slug, args.title, args.repo, github=args.github, sonar_key=args.sonar_key,
                           base_branch=args.base_branch, max_parallel=args.max_parallel,
                           clickup_parent=args.clickup_parent)
    _out(args, p, str(state.task_dir(p["slug"])))


def cmd_new_task(args):
    t = tasks.new_task(args.parent_key, args.task_dir, args.title, depends_on=_csv(args.depends_on),
                       repo=args.repo, github=args.github, sonar_project_key=args.sonar_key,
                       base_branch=args.base_branch)
    _out(args, t, t["key"])


def cmd_list_projects(args):
    projects = state.list_projects()
    _out(args, projects, "\n".join(f"{p['slug']}\t{p.get('state')}\t{p.get('title')}" for p in projects))


def _tree_text(node, depth=0):
    c = node["counts"]
    sess = f" session={node['session']['name']}" if node.get("session") and node["session"].get("name") else ""
    lines = [f"{'  ' * depth}{node['key']} [{node['state']}] {node['title']}{sess} "
             f"(issues {c['open_issues']}, inbox {c['open_inbox']}, prs {c['prs_green']}/{c['prs']}, "
             f"tests {c['tests_green']}/{c['tests']})"]
    for ch in node["children"]:
        lines.extend(_tree_text(ch, depth + 1))
    return lines


def cmd_tree(args):
    t = state.tree(args.key)
    nodes = t if isinstance(t, list) else [t]
    _out(args, t, "\n".join(line for n in nodes for line in _tree_text(n)) or "(no projects)")


def cmd_show(args):
    data = state.load(args.key)
    d = state.task_dir(args.key)
    data = {**data, "path": str(d), "files": {n: str(d / n) for n in (
        ["project.md", "spec.md", "tests.md", "plan.md"] if keys.is_project_key(args.key) else
        ["spec.md", "tests.json", "tests.md", "prs.json", "PRs.md", "issues.json", "issues.md",
         "inbox.json", "log.md"])}}
    _out(args, data)


def cmd_path(args):
    state.load(args.key)
    print(state.task_dir(args.key))


def cmd_set(args):
    try:
        patch = json.loads(args.merge)
    except json.JSONDecodeError as e:
        raise UsageError(f"--merge is not valid JSON: {e}") from e
    _out(args, tasks.merge(args.key, patch), "ok")


def cmd_set_state(args):
    t = tasks.set_state(args.key, args.state, note=args.note or "")
    _out(args, t, f"{args.key}: {t['state']}")


def cmd_ready(args):
    ready = tasks.ready(args.project)
    _out(args, ready, "\n".join(f"{t['key']}\t{t['title']}" for t in ready))


def cmd_task_for_session(args):
    key = tasks.task_for_session(args.session_id)
    if not key:
        return 1
    _out(args, {"key": key, "session_id": args.session_id}, key)


def _print_dispatch(args, r: dispatch.Result):
    _out(args, {"code": r.code, "output": r.output, "cmd": r.cmd, "cwd": r.cwd, **r.data}, r.output)
    return r.code


def cmd_dispatch(args):
    return _print_dispatch(args, dispatch.dispatch(args.key, args.message, dry_run=args.dry_run,
                                                   force=args.force))


def cmd_message(args):
    return _print_dispatch(args, dispatch.dispatch(args.key, args.text, dry_run=args.dry_run,
                                                   require_session=True))


def cmd_sessions(args):
    rows = dispatch.sessions()
    _out(args, rows, "\n".join(f"{r.get('sessionId')}\t{r.get('state') or r.get('status')}\t{r.get('name')}\t"
                               f"{r.get('task_key') or r.get('coordinator_of') or '-'}" for r in rows))


def cmd_send(args):
    r = agents.send(args.key, args.text)
    msg_id = (r.get("message") or {}).get("id")
    label = {"sent": "SENT live", "resumed": "RESUMED session", "queued": "QUEUED for forge:relay",
             "failed": "FAILED"}[r["mode"]]
    _out(args, r, f"{label}{f' ({msg_id})' if msg_id else ''}: {r['detail']}")
    return 0 if r["delivered"] else 1


def _live_text(r: dict) -> str:
    live = r["live"]
    if live.get("live") is None:
        return "unknown"
    if not live["live"]:
        return "not live"
    return "/".join(str(v) for v in (live.get("state"), live.get("status")) if v) or "live"


def cmd_agents(args):
    rows = agents.overview(args.project)
    lines = [f"{r['key']}\t{r['state']}\t{(r.get('session') or {}).get('name') or '-'}\t{_live_text(r)}\t"
             f"{r.get('last_activity') or '-'}\t{r.get('attach') or ''}" for r in rows]
    _out(args, rows, "\n".join(["KEY\tSTATE\tSESSION\tLIVE\tLAST ACTIVITY\tATTACH"] + lines))


def cmd_tests_add(args):
    t = tests_store.add(args.key, args.name, args.type, args.command, args.expected)
    _out(args, t, t["id"])


def cmd_tests_set(args):
    t = tests_store.set_status(args.key, args.test_id, args.status, args.evidence, args.skip_reason)
    _out(args, t, f"{t['id']}: {t['status']}")


def cmd_prs_add(args):
    p = prs.add(args.key, args.url, args.stack_index)
    _out(args, p, f"{p['repo']}#{p['number']} (stack {p['stack_index']})")


def cmd_prs_refresh(args):
    prs.refresh(args.key, include_closed=args.all)
    print("ok")


def cmd_issues_add(args):
    if args.body_file and not Path(args.body_file).exists():
        raise UsageError(f"body file not found: {args.body_file}")
    body = util.read_text(args.body_file) if args.body_file else args.body
    i = issues.add(args.key, args.title, args.severity, body or "", st=args.state, by=args.by)
    _out(args, i, i["id"])


def cmd_issues_resolve(args):
    i = issues.resolve(args.key, args.issue_id, args.resolution, wontfix=args.wontfix, by=args.by)
    _out(args, i, f"{i['id']}: {i['state']}")


def cmd_issues_set_state(args):
    i = issues.set_state(args.key, args.issue_id, args.state, note=args.note or "", by=args.by)
    _out(args, i, f"{i['id']}: {i['state']}")


DELIVERY_LABELS = {"sent": "SENT live", "resumed": "RESUMED session", "queued": "QUEUED in inbox",
                   "failed": "FAILED"}


def cmd_issues_decide(args):
    r = issues.decide(args.key, args.issue_id, args.decision, by=args.by, comment_id=args.comment_id,
                      notify=args.notify)
    i, d = r["issue"], r["delivery"]
    text = f"{i['id']}: decided"
    if d:
        msg_id = (d.get("message") or {}).get("id")
        text += f"\n{DELIVERY_LABELS.get(d['mode'], d['mode'])}{f' ({msg_id})' if msg_id else ''}: {d['detail']}"
    else:
        text += " (worker not notified)"
    _out(args, r, text)


def cmd_issues_list(args):
    rows = issues.list_issues(args.key, st=args.state)
    _out(args, rows, "\n".join(f"{r['task_key']}\t{r['id']}\t{r.get('severity')}\t{r.get('state')}\t"
                               f"{r.get('title')}" for r in rows) or "(no issues)")


def cmd_issues_set_artifact(args):
    i = issues.set_artifact(args.key, args.issue_id, args.url)
    _out(args, i, f"{i['id']}: {i['artifact_url']}")


def cmd_inbox_add(args):
    m = inbox.add(args.key, args.body, issue_id=args.issue, author=args.author, reply_to=args.reply_to)
    _out(args, m, m["id"])


def cmd_inbox_list(args):
    msgs = inbox.list_items(args.key, open_only=args.open)
    _out(args, msgs, "\n".join(f"{m['id']}\t{m['author']}\t{m['status']}\t{m['body']}" for m in msgs))


def cmd_inbox_ack(args):
    m = inbox.ack(args.key, args.msg_id)
    _out(args, m, f"{m['id']}: {m['status']}")


def cmd_inbox_resolve(args):
    m = inbox.resolve(args.key, args.msg_id, reply=args.reply)
    _out(args, m, f"{m['id']}: {m['status']}")


def cmd_inbox_pending(args):
    msgs = inbox.pending()
    _out(args, msgs, "\n".join(f"{m['task_key']}\t{m['id']}\t{m['body']}" for m in msgs))


def cmd_inbox_mark_delivered(args):
    m = inbox.mark_delivered(args.key, args.msg_id)
    _out(args, m, f"{m['id']}: delivered")


def _gate_text(r: dict) -> str:
    return "PASS" if r["passed"] else "FAIL\n" + "\n".join(f"- {x}" for x in r["reasons"])


def cmd_gate(args):
    r = gate.run(args.key)
    _out(args, r, _gate_text(r))
    return 0 if r["passed"] else 1


def cmd_handback(args):
    r = gate.handback(args.key, args.summary, args.docs)
    _out(args, r, ("HANDED BACK " + args.key) if r["passed"] else _gate_text(r))
    return 0 if r["passed"] else 1


def cmd_hook(args):
    """Never crash the Claude session: log any error and exit 0 silently."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        out = hooks.stop(payload) if args.hook_name == "stop" else hooks.session_start(payload)
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
    except BaseException:  # noqa: BLE001 - hooks must swallow everything
        try:
            log = state.cache_dir() / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"--- {util.now_iso()} hook {args.hook_name}\n{traceback.format_exc()}\n")
        except BaseException:  # noqa: BLE001
            pass
    return 0


def cmd_render(args):
    if bool(args.all) == bool(args.key):
        raise UsageError("pass either a key or --all")
    done = render.render_all() if args.all else render.render(args.key)
    _out(args, done, "\n".join(done) or "(nothing to render)")


def cmd_adopt_scan(args):
    print(json.dumps(adopt.scan(args.clickup_parent, repo=args.repo, github_repo=args.github),
                     indent=2, ensure_ascii=False))


def cmd_ui(args):
    server = REPO_ROOT / "ui" / "server.py"
    if not server.exists():
        raise ForgeError(f"UI not installed: {server} does not exist")
    os.execvp(sys.executable, [sys.executable, str(server), "--port", str(args.port)])


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="forge", description="Run projects as trees of Claude Code worker tasks.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("home", cmd_home)
    sp = add("init-project", cmd_init_project)
    sp.add_argument("slug")
    sp.add_argument("--title", required=True)
    sp.add_argument("--clickup-parent")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--github")
    sp.add_argument("--sonar-key")
    sp.add_argument("--base-branch", default="main")
    sp.add_argument("--max-parallel", type=int, default=tasks.DEFAULT_MAX_PARALLEL)

    sp = add("new-task", cmd_new_task)
    sp.add_argument("parent_key")
    sp.add_argument("task_dir")
    sp.add_argument("--title", required=True)
    sp.add_argument("--depends-on")
    sp.add_argument("--repo")
    sp.add_argument("--github")
    sp.add_argument("--sonar-key")
    sp.add_argument("--base-branch")

    add("list-projects", cmd_list_projects)
    add("tree", cmd_tree).add_argument("key", nargs="?")
    add("show", cmd_show).add_argument("key")
    add("path", cmd_path).add_argument("key")
    sp = add("set", cmd_set)
    sp.add_argument("key")
    sp.add_argument("--merge", required=True)
    sp = add("set-state", cmd_set_state)
    sp.add_argument("key")
    sp.add_argument("state")
    sp.add_argument("--note")
    add("ready", cmd_ready).add_argument("project", nargs="?")
    add("task-for-session", cmd_task_for_session).add_argument("session_id")

    sp = add("dispatch", cmd_dispatch)
    sp.add_argument("key")
    sp.add_argument("--message")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp = add("message", cmd_message)
    sp.add_argument("key")
    sp.add_argument("text")
    sp.add_argument("--dry-run", action="store_true")
    add("sessions", cmd_sessions)
    sp = add("send", cmd_send, help="message a task's session (live bridge, else resume)")
    sp.add_argument("key")
    sp.add_argument("text")
    add("agents", cmd_agents).add_argument("project", nargs="?")

    def group(name):
        g = sub.add_parser(name).add_subparsers(dest=f"{name}_command", required=True)

        def gadd(sub_name, fn):
            sp_ = g.add_parser(sub_name, parents=[common])
            sp_.set_defaults(fn=fn)
            return sp_
        return gadd

    tests_ = group("tests")
    sp = tests_("add", cmd_tests_add)
    sp.add_argument("key")
    sp.add_argument("--name", required=True)
    sp.add_argument("--type", required=True, choices=tests_store.TYPES)
    sp.add_argument("--command", default="")
    sp.add_argument("--expected", default="")
    sp = tests_("set", cmd_tests_set)
    sp.add_argument("key")
    sp.add_argument("test_id")
    sp.add_argument("--status", required=True, choices=tests_store.STATUSES)
    sp.add_argument("--evidence")
    sp.add_argument("--skip-reason")

    prs_ = group("prs")
    sp = prs_("add", cmd_prs_add)
    sp.add_argument("key")
    sp.add_argument("--url", required=True)
    sp.add_argument("--stack-index", type=int)
    sp = prs_("refresh", cmd_prs_refresh)
    sp.add_argument("key", nargs="?")
    sp.add_argument("--all", action="store_true", help="also refresh merged/closed PRs")

    issues_ = group("issues")
    sp = issues_("add", cmd_issues_add)
    sp.add_argument("key")
    sp.add_argument("--title", required=True)
    sp.add_argument("--severity", required=True, choices=issues.SEVERITIES)
    sp.add_argument("--state", choices=issues.STATES,
                    help="default: awaiting-user for blocker/decision/question, open for fyi")
    sp.add_argument("--by", default="worker", choices=issues.BY)
    body = sp.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file")
    body.add_argument("--body")
    sp = issues_("resolve", cmd_issues_resolve)
    sp.add_argument("key")
    sp.add_argument("issue_id")
    sp.add_argument("--resolution", required=True)
    sp.add_argument("--wontfix", action="store_true")
    sp.add_argument("--by", default="worker", choices=issues.BY)
    sp = issues_("set-state", cmd_issues_set_state)
    sp.add_argument("key")
    sp.add_argument("issue_id")
    sp.add_argument("state", choices=issues.STATES)
    sp.add_argument("--note")
    sp.add_argument("--by", default="worker", choices=issues.BY)
    sp = issues_("decide", cmd_issues_decide)
    sp.add_argument("key")
    sp.add_argument("issue_id")
    sp.add_argument("--decision", required=True)
    sp.add_argument("--by", default="user", choices=issues.DECIDERS)
    sp.add_argument("--comment-id", help="inbox message the decision came from (then no new message by default)")
    sp.add_argument("--notify", action=argparse.BooleanOptionalAction, default=None,
                    help="post 'Decision on I-n: ...' to the worker (default: yes unless --comment-id)")
    sp = issues_("list", cmd_issues_list)
    sp.add_argument("key", help="task key or project slug")
    sp.add_argument("--state", choices=issues.STATES)
    sp = issues_("set-artifact", cmd_issues_set_artifact)
    sp.add_argument("key")
    sp.add_argument("issue_id")
    sp.add_argument("url")

    inbox_ = group("inbox")
    sp = inbox_("add", cmd_inbox_add)
    sp.add_argument("key")
    sp.add_argument("--body", required=True)
    sp.add_argument("--issue")
    sp.add_argument("--author", default="user", choices=inbox.AUTHORS)
    sp.add_argument("--reply-to")
    sp = inbox_("list", cmd_inbox_list)
    sp.add_argument("key")
    sp.add_argument("--open", action="store_true")
    for name, fn in (("ack", cmd_inbox_ack), ("mark-delivered", cmd_inbox_mark_delivered)):
        sp = inbox_(name, fn)
        sp.add_argument("key")
        sp.add_argument("msg_id")
    sp = inbox_("resolve", cmd_inbox_resolve)
    sp.add_argument("key")
    sp.add_argument("msg_id")
    sp.add_argument("--reply")
    inbox_("pending", cmd_inbox_pending)

    add("gate", cmd_gate).add_argument("key")
    sp = add("handback", cmd_handback)
    sp.add_argument("key")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--docs", required=True)
    sp = add("hook", cmd_hook)
    sp.add_argument("hook_name", choices=("stop", "session-start"))
    sp = add("render", cmd_render)
    sp.add_argument("key", nargs="?")
    sp.add_argument("--all", action="store_true", help="regenerate every rendered file across FORGE_HOME")
    sp = add("adopt-scan", cmd_adopt_scan)
    sp.add_argument("--clickup-parent", required=True)
    sp.add_argument("--repo")
    sp.add_argument("--github")
    add("ui", cmd_ui).add_argument("--port", type=int, default=7777)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except ForgeError as e:
        print(f"forge: {e}", file=sys.stderr)
        return 1
