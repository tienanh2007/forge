"""GitHub PR status via the `gh` CLI."""
import json
import re

from forge_lib import util
from forge_lib.errors import IntegrationError, UsageError

PR_URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      url title state isDraft mergeable baseRefName baseRefOid headRefName headRefOid reviewDecision updatedAt
      comments { totalCount }
      commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { nodes {
        __typename
        ... on CheckRun { name status conclusion detailsUrl }
        ... on StatusContext { context state targetUrl }
      } } } } } }
      reviewThreads(first: 100) { nodes { id isResolved isOutdated path line
        comments(first: 50) { nodes { author { login } body createdAt url } } } }
    }
  }
}
"""

CHECK_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
STATUS_PENDING = {"PENDING", "EXPECTED"}


def parse_pr_url(url: str) -> tuple[str, int]:
    m = PR_URL_RE.match(url.strip())
    if not m:
        raise UsageError(f"not a GitHub PR url: {url}")
    return f"{m.group(1)}/{m.group(2)}", int(m.group(3))


def _gh_json(cmd: list[str], what: str):
    proc = util.run(cmd)
    if proc.returncode != 0:
        raise IntegrationError(f"{what} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise IntegrationError(f"{what}: invalid JSON from gh") from e


def _checks(rollup: dict | None) -> dict:
    failing, pending, names, total = [], [], [], 0
    for node in ((rollup or {}).get("contexts") or {}).get("nodes") or []:
        total += 1
        if node.get("conclusion") != "SKIPPED":  # a skipped job (e.g. draft-gated build) did not run
            names.append(node.get("context") or node.get("name"))
        if node.get("__typename") == "StatusContext":
            name, url, st = node.get("context"), node.get("targetUrl"), node.get("state")
            if st in STATUS_PENDING:
                pending.append({"name": name})
            elif st != "SUCCESS":
                failing.append({"name": name, "url": url})
        else:
            name, url = node.get("name"), node.get("detailsUrl")
            if node.get("status") != "COMPLETED":
                pending.append({"name": name})
            elif node.get("conclusion") not in CHECK_OK:
                failing.append({"name": name, "url": url})
    state = "NONE" if total == 0 else "FAILURE" if failing else "PENDING" if pending else "SUCCESS"
    return {"state": state, "total": total, "failing": failing, "pending": pending, "names": names}


def _threads(nodes: list[dict]) -> dict:
    items = [{
        "id": t.get("id"), "path": t.get("path"), "line": t.get("line"),
        "is_resolved": bool(t.get("isResolved")), "is_outdated": bool(t.get("isOutdated")),
        "comments": [{"author": (c.get("author") or {}).get("login"), "body": c.get("body"),
                      "at": c.get("createdAt"), "url": c.get("url")}
                     for c in (t.get("comments") or {}).get("nodes") or []],
    } for t in nodes]
    resolved = sum(1 for t in items if t["is_resolved"])
    return {"total": len(items), "resolved": resolved, "unresolved": len(items) - resolved, "items": items}


def map_pr(pr: dict) -> dict:
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = (commits[-1].get("commit") or {}).get("statusCheckRollup") if commits else None
    return {
        "url": pr.get("url"), "title": pr.get("title"), "state": pr.get("state"),
        "is_draft": bool(pr.get("isDraft")), "base": pr.get("baseRefName"), "head": pr.get("headRefName"),
        "mergeable": pr.get("mergeable"), "base_oid": pr.get("baseRefOid"), "head_oid": pr.get("headRefOid"),
        "review_decision": pr.get("reviewDecision"),
        "checks": _checks(rollup),
        "threads": _threads((pr.get("reviewThreads") or {}).get("nodes") or []),
        "comments_count": (pr.get("comments") or {}).get("totalCount", 0),
        "updated_at": pr.get("updatedAt"),
    }


def pr_status(repo: str, number: int) -> dict:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise UsageError(f"repo must be owner/name: {repo}")
    data = _gh_json(["gh", "api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"owner={owner}",
                     "-F", f"name={name}", "-F", f"number={int(number)}"], f"gh pr status {repo}#{number}")
    if data.get("errors"):
        raise IntegrationError(f"gh pr status {repo}#{number}: {data['errors'][0].get('message')}")
    pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not pr:
        raise IntegrationError(f"PR not found: {repo}#{number}")
    return map_pr(pr)


def search_prs(repo: str, search: str) -> list[dict]:
    return _gh_json(["gh", "pr", "list", "--repo", repo, "--state", "all", "--search", search,
                     "--json", "number,url,title,headRefName,baseRefName,state,isDraft"],
                    f"gh pr list {repo}")
