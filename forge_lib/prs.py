"""PR tracking: add PRs to a task, refresh their GitHub + Sonar status, judge green-ness."""
from forge_lib import keys, state, util
from forge_lib.errors import ForgeError
from forge_lib.integrations import github, sonar

CLOSED_STATES = ("MERGED", "CLOSED")


def add(key: str, url: str, stack_index: int | None = None) -> dict:
    repo, number = github.parse_pr_url(url)
    state.load_task(key)
    items = state.load_json_items(key, "prs")
    existing = next((p for p in items if p.get("repo") == repo and p.get("number") == number), None)
    if existing:
        if stack_index is not None:
            existing["stack_index"] = stack_index
        item = existing
    else:
        item = {"repo": repo, "number": number, "url": url.strip(), "title": "", "branch": "", "base": "",
                "stack_index": len(items) if stack_index is None else stack_index,
                "added": util.now_iso(), "status": None}
        items.append(item)
    state.save_json_items(key, "prs", items)
    return item


def fetch_status(item: dict, sonar_key: str | None) -> dict:
    """Fresh status for one PR item; on failure keep the previous status and record the error."""
    try:
        status = github.pr_status(item["repo"], item["number"])
        status["sonar"] = sonar.quality_gate(sonar_key, item["number"]) if sonar_key else None
    except ForgeError as e:
        status = {k: v for k, v in (item.get("status") or {}).items() if k != "error"}
        status["error"] = str(e)
    status["fetched_at"] = util.now_iso()
    return status


def refresh_task(key: str, include_closed: bool = True) -> list[dict]:
    task = state.load_task(key)
    items = state.load_json_items(key, "prs")
    for item in items:
        prev = item.get("status") or {}
        if not include_closed and prev.get("state") in CLOSED_STATES:
            continue
        item["status"] = fetch_status(item, task.get("sonar_project_key"))
        st = item["status"]
        if "error" not in st:
            item["title"], item["branch"], item["base"] = st.get("title"), st.get("head"), st.get("base")
    state.save_json_items(key, "prs", items)
    return items


def refresh(key_or_project: str | None = None, include_closed: bool = False) -> None:
    """Refresh every PR under a task/project (or everything). Merged/closed PRs are skipped by default."""
    if key_or_project is None:
        targets = state.all_task_keys()
    else:
        targets = ([] if keys.is_project_key(key_or_project) else [key_or_project]) \
            + state.descendant_keys(key_or_project)
    for key in targets:
        if state.load_json_items(key, "prs"):
            refresh_task(key, include_closed=include_closed)


def problems(item: dict, sonar_required: bool, required_checks: list[str] | None = None,
             below: dict | None = None) -> list[str]:
    """Why a PR is not green (empty list = green).

    required_checks: substrings of check names that must have run - catches CI that silently never
    started (draft-skipped jobs, merge conflicts) while the few checks that did run are green.
    below: the PR under this one in its stack, to detect a stale base.
    """
    label = f"PR {item.get('repo')}#{item.get('number')}"
    st = item.get("status")
    if not st:
        return [f"{label}: status not fetched"]
    if st.get("error"):
        return [f"{label}: status fetch failed: {st['error']}"]
    out = []
    checks = st.get("checks") or {}
    cstate = checks.get("state", "NONE")
    if cstate == "FAILURE":
        names = ", ".join(c.get("name") or "?" for c in checks.get("failing") or [])
        out.append(f"{label}: CI failing ({names})")
    elif cstate == "PENDING":
        names = ", ".join(c.get("name") or "?" for c in checks.get("pending") or [])
        out.append(f"{label}: CI pending ({names})")
    elif cstate != "SUCCESS":
        out.append(f"{label}: no CI checks reported")
    if st.get("mergeable") == "CONFLICTING":
        out.append(f"{label}: merge conflict with {st.get('base')} (CI may not run until resolved)")
    ran = [n.lower() for n in checks.get("names") or [] if n]
    for req in required_checks or []:
        if not any(req.lower() in n for n in ran):
            out.append(f"{label}: required check '{req}' never ran"
                       + (" (draft PRs may skip it)" if st.get("is_draft") else ""))
    below_st = (below or {}).get("status") or {}
    if (below_st.get("head") and st.get("base") == below_st.get("head") and st.get("base_oid")
            and below_st.get("head_oid") and st["base_oid"] != below_st["head_oid"]):
        out.append(f"{label}: stale stack - based on an old {st['base']}; restack onto "
                   f"{below.get('repo')}#{below.get('number')}")
    unresolved = (st.get("threads") or {}).get("unresolved", 0)
    if unresolved:
        out.append(f"{label}: {unresolved} unresolved review thread(s)")
    if sonar_required:
        sq = st.get("sonar") or {}
        sstatus = sq.get("status", "NONE")
        if sstatus == "NONE":
            out.append(f"{label}: sonar pending")
        elif sstatus != "OK":
            failed = ", ".join(c["metric"] for c in sq.get("conditions") or [] if c.get("status") == "ERROR")
            out.append(f"{label}: sonar quality gate {sstatus}" + (f" ({failed})" if failed else ""))
    return out


def is_green(item: dict, sonar_required: bool, required_checks: list[str] | None = None,
             below: dict | None = None) -> bool:
    return not problems(item, sonar_required, required_checks, below)


def with_below(items: list[dict]) -> list[tuple[dict, dict | None]]:
    """Pair each PR with the PR beneath it in the same repo's stack (by stack_index)."""
    ordered = sorted(items, key=lambda i: (i.get("repo") or "", i.get("stack_index") or 0))
    return [(it, prev if prev and prev.get("repo") == it.get("repo") else None)
            for prev, it in zip([None] + ordered[:-1], ordered)]


def summary_line(item: dict) -> str:
    st = item.get("status") or {}
    sonar_status = (st.get("sonar") or {}).get("status", "-")
    threads = st.get("threads") or {}
    return (f"[{item.get('stack_index')}] {item.get('repo')}#{item.get('number')} {item.get('title') or ''} — "
            f"{st.get('state', '?')}{' draft' if st.get('is_draft') else ''}, "
            f"CI {(st.get('checks') or {}).get('state', '?')}, "
            f"threads {threads.get('unresolved', '?')} unresolved, sonar {sonar_status}"
            + (f", error: {st['error']}" if st.get("error") else ""))
