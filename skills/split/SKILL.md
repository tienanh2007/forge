---
name: split
description: Use when a forge project or task must be decomposed into worker-sized tasks - after start-project/adopt-project scoping, or when a forge worker finds its own task too big for one context ("split this", "this task is too big", "break the project into tasks").
---

# forge: split into tasks

Works at two levels:
- **Project level** (coordinator): parent key = project slug.
- **Inside a task** (a worker whose task is too big): parent key = your own task key. Your task
  becomes `coordinating`; **you stay its owner** and coordinate the children exactly like a
  coordinator (dispatch, relay, review handbacks), then hand back your own task when children are done.

## Sizing heuristics - a task must fit one worker's context

- At most one stacked PR series of **<= 3 PRs**.
- Touches **<= ~15 files** in **1-2 modules**.
- Its spec.md is **<= 2 pages**.
- **Independently testable**: has its own tests that can go green without sibling tasks being merged
  (mock/stub across the interface if needed).
- One owner, one reason to change. If you cannot write a crisp title, it is two tasks.
- Prefer vertical slices over layers only when layers would force cross-task churn; IDL/proto
  changes (often a separate repo) are usually their own first task.

## Checklist

1. Read the parent's spec.md / tests.md (`forge path <parent-key>`).
2. Draft the decomposition. For each task: dir name, title, one-paragraph scope, repo, depends_on,
   **explicit interfaces** (what it provides/consumes: method signatures, proto messages, endpoints,
   config keys), and which parent acceptance tests it satisfies.
   - Task dir = `<ID>-<slug>`; ID = ClickUp custom id if a subtask already exists, else `T<n>`.
3. **Present a distribution table to the user and wait for approval:**

   | # | Task dir | Title | Repo/module | Depends on | Interfaces | Est. PRs | Tests |
   |---|---|---|---|---|---|---|---|

   Also show the dependency graph (mermaid `graph LR`) and which tasks can start in parallel.
4. On approval, create each task:
   ```bash
   forge new-task <parent-key> <task-dir> --title "<Title>" [--depends-on K1,K2] \
     [--repo PATH --github O/R --sonar-key K --base-branch B]
   D=$(forge path <parent-key>/<task-dir>)
   ```
5. Write `$D/spec.md` for each (<= 2 pages): context (link parent spec), scope, out of scope,
   interfaces (exact), acceptance criteria, docs to update.
6. Add tests for each task (these drive TDD in the worker):
   ```bash
   forge tests add <task-key> --name "<name>" --type unit --command "<cmd>" --expected "<expected>"
   ```
   Every acceptance criterion gets >= 1 test. Include a `ci` test ("PR checks green") per task.
7. If splitting inside your own task: `forge set-state <your-key> coordinating --note "split into N"`
   and append the split rationale to your `log.md`.
8. Hand off to `forge:dispatch` (only tasks the user approved).

## Rules

- Do not create tasks the user has not approved.
- Interfaces are contracts: if a worker later needs to change one, it must write an issue
  (`forge:write-issue`) rather than silently change it.
