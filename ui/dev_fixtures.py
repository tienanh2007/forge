"""Build a realistic fixture FORGE_HOME for UI development (files written per CONTRACT.md).

Usage: python3 ui/dev_fixtures.py [target-dir]   (default: a new temp dir; prints its path)
"""

import json
import sys
import tempfile
import uuid
from pathlib import Path

T0 = "2026-09-20T09:00:00Z"
T1 = "2026-09-24T15:30:00Z"
T2 = "2026-09-25T10:05:00Z"
GC_REPO = "/Users/dev/src/my-service"
GC_GITHUB = "example-org/my-service"
GC_SONAR = "example-org_my-service"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _items(items: list) -> dict:
    return {"schema": 1, "items": items}


def _session(key: str) -> dict:
    name = "forge-" + key.replace("/", "-")
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "forge:" + key)), "name": name, "worktree": name}


def _key_dir(home: Path, key: str) -> Path:
    segments = key.split("/")
    path = home / segments[0]
    for segment in segments[1:]:
        path = path / "tasks" / segment
    return path


def _pr_status(number, title, *, state="OPEN", draft=False, checks="SUCCESS", failing=(), pending=(),
               threads=(), comments=0, decision=None, sonar="OK", sonar_key=GC_SONAR, github=GC_GITHUB):
    items = []
    for idx, (resolved, path, line, convo) in enumerate(threads):
        items.append({
            "id": f"PRRT_{number}_{idx}", "path": path, "line": line, "is_resolved": resolved,
            "is_outdated": False,
            "comments": [{"author": a, "body": b, "at": T1,
                          "url": f"https://github.com/{github}/pull/{number}#discussion_r{number}{idx}{j}"}
                         for j, (a, b) in enumerate(convo)],
        })
    status = {
        "url": f"https://github.com/{github}/pull/{number}", "title": title, "state": state,
        "is_draft": draft, "base": "main", "head": f"branch-{number}", "review_decision": decision,
        "checks": {
            "state": checks,
            "total": 12,
            "failing": [{"name": n, "url": f"https://github.com/{github}/actions/runs/{number}0{i}"}
                        for i, n in enumerate(failing)],
            "pending": [{"name": n} for n in pending],
        },
        "threads": {"total": len(items), "resolved": sum(1 for t in items if t["is_resolved"]),
                    "unresolved": sum(1 for t in items if not t["is_resolved"]), "items": items},
        "comments_count": comments,
        "updated_at": T1,
        "fetched_at": T2,
    }
    if sonar_key:
        status["sonar"] = {
            "status": sonar,
            "conditions": [] if sonar in ("OK", "NONE") else [
                {"metric": "new_coverage", "status": "ERROR", "actual": "61.2", "threshold": "80"}],
            "url": f"https://sonarcloud.io/summary/new_code?id={sonar_key}&pullRequest={number}",
        }
    return status


def _pr(number, title, branch, stack_index, status, github=GC_GITHUB):
    return {"repo": github, "number": number, "url": f"https://github.com/{github}/pull/{number}",
            "title": title, "branch": branch, "base": "main", "stack_index": stack_index,
            "added": T0, "status": status}


def _task(home, key, title, state, *, parent=None, depends=(), clickup=None, dispatched=True,
          gate=None, handback=None, history=(), repo=GC_REPO, github=GC_GITHUB, sonar=GC_SONAR,
          spec="", log="", tests=(), prs=(), issues=(), inbox=()):
    path = _key_dir(home, key)
    hist = [{"state": "scoped", "at": T0, "note": ""}] + [
        {"state": s, "at": T1, "note": n} for s, n in history]
    _write(path / "task.json", {
        "schema": 1, "key": key, "title": title, "parent_key": parent, "depends_on": list(depends),
        "repo": repo, "github": github, "sonar_project_key": sonar, "base_branch": "main",
        "clickup": clickup, "session": _session(key) if dispatched else None, "state": state,
        "state_history": hist,
        "gate": gate or {"last_run": None, "passed": False, "reasons": [], "consecutive_blocks": 0},
        "handback": handback, "created": T0, "updated": T2,
    })
    _write(path / "spec.md", spec or f"# {title}\n\nSpec pending.\n")
    _write(path / "log.md", log or f"# Log — {key}\n")
    _write(path / "tests.json", _items(list(tests)))
    _write(path / "prs.json", _items(list(prs)))
    issue_items = []
    for issue in issues:
        body = issue.pop("body")
        issue_items.append({"file": f"issues/{issue['id']}.md", "artifact_url": None, "created": T1,
                            "updated": T2, "resolution": "", **issue})
        _write(path / "issues" / f"{issue['id']}.md", body)
    _write(path / "issues.json", _items(issue_items))
    _write(path / "inbox.json", _items([
        {"issue_id": None, "reply_to": None, "status": "open", "delivered": False, "at": T1, **m}
        for m in inbox]))


def _cu(task_id, custom_id):
    return {"id": task_id, "custom_id": custom_id, "url": f"https://app.clickup.com/t/{task_id}"}


def _test(tid, name, ttype, status, command="", evidence="", skip_reason=""):
    return {"id": tid, "name": name, "type": ttype, "command": command, "expected": "passes",
            "status": status, "evidence": evidence, "skip_reason": skip_reason, "updated": T2}


MERMAID_DECISION = """# Idempotency key storage: Postgres vs Redis

The DAO needs to dedupe retried `CreateInvoice` calls. Two options:

```mermaid
flowchart LR
  A[CreateInvoice RPC] --> B{key seen?}
  B -- Postgres unique idx --> C[(invoices_idem)]
  B -- Redis SETNX --> D[(redis)]
  C --> E[insert invoice]
  D --> E
```

| Option | Durability | Latency | Ops cost |
|---|---|---|---|
| Postgres unique index | survives failover | +2ms | none |
| Redis `SETNX` + TTL | lost on eviction | +0.3ms | new dependency |

**Recommendation:** Postgres — durability matters more than 1.7ms here.

How should this be resolved?
"""

MERMAID_BLOCKER = """# Docs site build is broken on main

`docs/` build fails before my change, so the gate can't pass.

```mermaid
sequenceDiagram
  participant W as worker
  participant CI
  W->>CI: push docs change
  CI-->>W: mkdocs build FAILED (missing plugin)
```

Needs someone with repo-admin to bump the docs image. See the artifact for the full CI log.
"""


def build(home: Path) -> Path:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    p1 = "billing-v2"
    _write(home / p1 / "project.json", {
        "schema": 1, "slug": p1, "title": "Billing v2", "state": "active",
        "clickup_parent": _cu("86abc", "ENG-100"),
        "repos": [{"path": GC_REPO, "github": GC_GITHUB, "sonar_project_key": GC_SONAR, "base_branch": "main"}],
        "max_parallel": 5, "coordinator_session": {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "forge:" + p1)),
                                                   "name": "forge-billing-v2-coordinator"},
        "created": T0, "updated": T2,
    })
    _write(home / p1 / "project.md", "# Billing v2\n\nRebuild invoice generation on the new ledger.\n")
    for name in ("spec.md", "tests.md", "plan.md"):
        _write(home / p1 / name, f"# {name}\n")

    api = f"{p1}/ENG-101-api"
    _task(home, api, "Invoice API", "coordinating", clickup=_cu("86a101", "ENG-101"),
          history=[("dispatched", ""), ("in-progress", ""), ("coordinating", "split into DAO + handler")],
          spec="# Invoice API\n\nExpose `CreateInvoice` / `GetInvoice` gRPC endpoints.\n\n"
               "## Acceptance\n- idempotent create\n- p99 < 50ms\n",
          log="# Log\n\n- 09-24 split into ENG-105 (DAO) and ENG-106 (handler)\n")
    dao = f"{api}/ENG-105-dao"
    _task(home, dao, "Invoice DAO", "in-progress", parent=api, clickup=_cu("86a105", "ENG-105"),
          history=[("dispatched", ""), ("in-progress", "")],
          gate={"last_run": T2, "passed": False, "consecutive_blocks": 1,
                "reasons": ["TST-3 is red", "PR #9912: checks FAILURE (unit-tests)",
                            "PR #9912: 2 unresolved review threads", "1 open inbox message"]},
          spec="# Invoice DAO\n\nPostgres-backed DAO with idempotency keys.\n\n```java\n"
               "Invoice create(CreateInvoiceRequest req, String idemKey);\n```\n",
          log="# Log\n\n" + "\n".join(f"- step {i}: did a thing" for i in range(1, 41)) + "\n",
          tests=[_test("TST-1", "rejects negative amount", "unit", "green",
                       "./gradlew :gw-shared:test --tests InvoiceDaoTest", "local run ok"),
                 _test("TST-2", "idempotent create returns same id", "integration", "green",
                       evidence="https://github.com/example-org/my-service/actions/runs/1"),
                 _test("TST-3", "concurrent create race", "unit", "red", evidence="expected 1 row, got 2"),
                 _test("TST-4", "manual staging smoke", "manual", "pending")],
          prs=[_pr(9911, "[ENG-105] Invoice table + migration", "ENG-105-schema", 0,
                   _pr_status(9911, "[ENG-105] Invoice table + migration", decision="APPROVED",
                              threads=[(True, "db/V12__invoice.sql", 3, [("alice", "index name?"), ("worker", "fixed")])],
                              comments=4)),
               _pr(9912, "[ENG-105] InvoiceDao + idempotency", "ENG-105-dao", 1,
                   _pr_status(9912, "[ENG-105] InvoiceDao + idempotency", draft=True, checks="FAILURE",
                              failing=["unit-tests", "spotless"], decision="CHANGES_REQUESTED", sonar="ERROR",
                              threads=[(False, "InvoiceDao.java", 42, [("bob", "This races under **concurrent** creates."),
                                                                      ("worker", "Adding a unique index.")]),
                                       (False, "InvoiceDao.java", 88, [("bob", "Use `RequestContextLogger` here.")]),
                                       (True, "InvoiceDaoTest.java", 10, [("bob", "nit: name")])],
                              comments=7))],
          issues=[{"id": "I-1", "title": "Idempotency key storage: Postgres vs Redis", "severity": "decision",
                   "status": "open", "body": MERMAID_DECISION},
                  {"id": "I-2", "title": "Flaky migration test on CI", "severity": "fyi", "status": "resolved",
                   "resolution": "Pinned testcontainers image", "body": "# Flaky migration test\n\nResolved.\n"}],
          inbox=[{"id": "C-1", "issue_id": "I-1", "author": "worker", "body": "Opened I-1; leaning Postgres.",
                  "status": "open"},
                 {"id": "C-2", "issue_id": "I-1", "author": "user", "body": "Go with Postgres.",
                  "reply_to": "C-1", "status": "acked", "delivered": True},
                 {"id": "C-3", "issue_id": None, "author": "user", "body": "Also please rebase on main.",
                  "status": "open"}])
    _task(home, f"{api}/ENG-106-handler", "Invoice gRPC handler", "scoped", parent=api, dispatched=False,
          depends=[dao], clickup=_cu("86a106", "ENG-106"))
    _task(home, f"{p1}/ENG-102-schema", "Ledger schema", "handed-back", clickup=_cu("86a102", "ENG-102"),
          history=[("dispatched", ""), ("in-progress", ""), ("in-review", ""), ("handed-back", "gate passed")],
          gate={"last_run": T1, "passed": True, "reasons": [], "consecutive_blocks": 0},
          handback={"summary": "Ledger tables + migrations merged; backfill job left to ENG-110.",
                    "docs": "docs/billing/ledger.md (new), docs/index.md", "at": T1},
          tests=[_test("TST-1", "migration applies cleanly", "ci", "green", evidence="CI #881"),
                 _test("TST-2", "load test", "e2e", "skipped", skip_reason="covered by staging soak")],
          prs=[_pr(9870, "[ENG-102] Ledger schema", "ENG-102-schema", 0,
                   _pr_status(9870, "[ENG-102] Ledger schema", state="MERGED", decision="APPROVED", comments=2))])
    _task(home, f"{p1}/ENG-103-docs", "Billing docs", "blocked", clickup=_cu("86a103", "ENG-103"),
          history=[("dispatched", ""), ("in-progress", ""), ("blocked", "docs build broken on main")],
          sonar=None,
          gate={"last_run": T2, "passed": False, "consecutive_blocks": 6, "reasons": ["no PRs recorded", "no tests"]},
          issues=[{"id": "I-1", "title": "Docs site build is broken on main", "severity": "blocker",
                   "status": "open", "artifact_url": "https://claude.ai/code/artifact/example-docs-ci-log",
                   "body": MERMAID_BLOCKER}])
    _task(home, f"{p1}/T4-cleanup", "Remove v1 billing flags", "cancelled", dispatched=False,
          history=[("cancelled", "descoped")])

    p2 = "lakeview-alerts"
    _write(home / p2 / "project.json", {
        "schema": 1, "slug": p2, "title": "LakeView alerts", "state": "scoping", "clickup_parent": None,
        "repos": [{"path": "/Users/dev/src/agents", "github": "example-org/agents",
                   "sonar_project_key": None, "base_branch": "main"}],
        "max_parallel": 2, "coordinator_session": None, "created": T0, "updated": T1,
    })
    _write(home / p2 / "project.md", "# LakeView alerts\n")
    ai = dict(repo="/Users/dev/src/agents", github="example-org/agents")
    _task(home, f"{p2}/T1-runbook", "Write alert runbook", "dispatched", sonar=None, **ai,
          prs=[_pr(310, "Runbook for stale metrics alert", "t1-runbook", 0,
                   _pr_status(310, "Runbook for stale metrics alert", draft=True, checks="PENDING",
                              pending=["lint", "build"], sonar_key=None, github="example-org/agents"),
                   github="example-org/agents")])
    _task(home, f"{p2}/T2-metric", "Emit staleness metric", "in-review", sonar="example-org_agents", **ai,
          gate={"last_run": T2, "passed": False, "consecutive_blocks": 2, "reasons": ["PR #311: sonar ERROR"]},
          tests=[_test("TST-1", "metric emitted", "unit", "green")],
          prs=[_pr(311, "Emit lakeview_staleness_seconds", "t2-metric", 0,
                   _pr_status(311, "Emit lakeview_staleness_seconds", sonar="ERROR", decision="REVIEW_REQUIRED",
                              sonar_key="example-org_agents", github="example-org/agents"),
                   github="example-org/agents"),
               _pr(312, "Dashboard panel", "t2-dash", 1, None, github="example-org/agents")],
          issues=[{"id": "I-1", "title": "Which threshold should page?", "severity": "question", "status": "open",
                   "body": "# Threshold\n\n```mermaid\npie title Staleness p95 (min)\n  \"<5\" : 80\n"
                           "  \"5-30\" : 15\n  \">30\" : 5\n```\n\n30 min or 60 min?\n"}],
          inbox=[{"id": "C-1", "issue_id": "I-1", "author": "worker", "body": "See I-1 — need a threshold."}])

    clickup = {
        "schema": 1,
        "parents": {
            p1: {"fetched_at": T2, "summary": {
                "id": "86abc", "custom_id": "ENG-100", "name": "Billing v2", "status": "in progress",
                "url": "https://app.clickup.com/t/86abc", "assignees": ["Tien"],
                "subtasks": [
                    {"id": "86a101", "custom_id": "ENG-101", "name": "Invoice API", "status": "in progress",
                     "url": "https://app.clickup.com/t/86a101", "assignees": ["Tien"], "subtasks": [
                         {"id": "86a105", "custom_id": "ENG-105", "name": "Invoice DAO", "status": "in review",
                          "url": "https://app.clickup.com/t/86a105", "assignees": [], "subtasks": []},
                         {"id": "86a106", "custom_id": "ENG-106", "name": "Invoice handler", "status": "to do",
                          "url": "https://app.clickup.com/t/86a106", "assignees": [], "subtasks": []}]},
                    {"id": "86a102", "custom_id": "ENG-102", "name": "Ledger schema", "status": "done",
                     "url": "https://app.clickup.com/t/86a102", "assignees": ["Tien"], "subtasks": []},
                    {"id": "86a103", "custom_id": "ENG-103", "name": "Billing docs", "status": "blocked",
                     "url": "https://app.clickup.com/t/86a103", "assignees": [], "subtasks": []},
                    {"id": "86a109", "custom_id": "ENG-109", "name": "Tax rules (not in forge)", "status": "to do",
                     "url": "https://app.clickup.com/t/86a109", "assignees": [], "subtasks": []}]}}},
    }
    _write(home / ".cache" / "clickup.json", clickup)
    return home


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="forge-fixtures-"))
    print(build(target))
