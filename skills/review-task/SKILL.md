---
name: review-task
description: Use to review the current state of one forge task end to end (spec, PRs, CI, SonarCloud, review threads, code) and turn every problem or pending decision into a real forge issue; also drafts missing spec.md/tests. Run by forge:adopt-project for each adopted task (one subagent per task, in parallel) and by coordinators before review-handback.
---

# forge: review a task and raise real issues

You are a reviewer. **Read-only on external systems**: never push, comment on, approve, resolve or
edit PRs, ClickUp tasks or CI. You write only into the task folder, through the `forge` CLI.

Input: a task key. `TASK=$(forge path <key>)`.

## 1. Load context
```bash
forge show <key> --json
cat "$TASK/spec.md" "$TASK/log.md" 2>/dev/null
forge prs refresh <key> && cat "$TASK/prs.json"
cat "$TASK/issues.json"   # existing issues - do not duplicate them
```
ClickUp: the task's (or project parent's) description and comments, via ClickUp MCP tools or the API.

## 2. Collect evidence per PR (bottom of the stack first)
- `gh pr view <n> --repo <o/r> --json title,body,files,baseRefName,headRefName,isDraft,reviewDecision`
- `gh pr diff <n> --repo <o/r>` (skim; read the parts that matter to the spec)
- CI failing: `gh pr checks <n> --repo <o/r>`, then `gh run view <run-id> --repo <o/r> --log-failed | tail -80`
  and find the real root cause (first failing test / compiler error), not just the job name.
- Unresolved review threads: from `prs.json` `status.threads.items` (`is_resolved=false`); read the
  full thread and the code it points at; decide whether the author already addressed it.
- SonarCloud (if `sonar_project_key`): gate conditions from `prs.json`, and
  `https://sonarcloud.io/api/issues/search?componentKeys=<key>&pullRequest=<n>&resolved=false`
  (basic auth `SONAR_TOKEN:` from `~/.config/forge/env`).
- Stack health: each PR's base is the PR below it; flag a PR whose base is stale or merged.

## 3. Review the code against the spec/ticket
Look for: behaviour the ticket asks for that no PR delivers, missing or weak tests, risky changes
(backward compatibility, config defaults, multi-cloud, error handling), docs the repo requires
(e.g. `docs/` rules in its CLAUDE.md), and scope that belongs in another task.
Only report problems you can point at (file/class/method, PR, check name). No style nits.

## 4. Draft spec and tests if missing
- If `spec.md` is still the template, write it from the ticket + PR bodies: Goal, Scope,
  Out of scope, Interfaces, Acceptance criteria. Mark it `Status: drafted by review - confirm`.
- For each acceptance criterion / test the PRs add: `forge tests add <key> --name .. --type ..
  --command ..`; set `green` with the passing CI run URL as evidence, `red` with the failing run.

## 5. Raise issues
One issue per **distinct** problem or decision (group review threads by theme; one CI root cause =
one issue even if it fails several PRs). Follow `forge:write-issue` for the body template and
register with `forge issues add <key> --title .. --severity .. --body-file .. --by reviewer`.
The state defaults correctly: `awaiting-user` for blocker/decision/question, `open` for fyi - do not
override it, and never `decide` or `resolve` issues yourself.

| Finding | Severity |
|---|---|
| CI failing, Sonar gate ERROR, stack broken, spec requirement undelivered | `blocker` |
| Reviewer asks for a change the author hasn't made / disagrees with; design choice open | `decision` |
| Missing information only the user has (ownership, rollout, env access) | `question` |
| Risk or follow-up worth knowing, no action needed now | `fyi` |

Every issue must contain: evidence with links (PR, check run, thread URL, file:line), the root
cause as far as you can establish it, options with a recommendation, and the one thing needed from
the user. Add a mermaid diagram when a flow, stack or dependency explains the problem faster.
Do **not** change task state and do not publish artifacts (the owning session does that).

## 6. Report back (≤15 lines)
Task key, spec/tests drafted (counts), issues created (`I-n severity title`), and anything you
could not check and why.
