---
name: work
description: Use when you are a forge worker session for a task ("You are the forge worker for task <key>", "Resume task <key>") - the end-to-end loop that takes one task from spec to handed-back PRs with tests, CI/Sonar green, docs and ClickUp updated.
---

# forge: worker loop

You own **one task** end-to-end, possibly across many resumes. The task folder is the source of
truth; `log.md` is your memory. The user may `claude attach` to you at any time.

Set once at the start of each run:
```bash
K=<task key from the prompt>
D=$(forge path $K)
forge show $K --json      # repo, github, sonar key, base branch, clickup, session, state, parent_key
```

## 0. Resuming?

The SessionStart hook prints a briefing (state, spec head, log tail, open inbox, issues, PRs, gate).
If `log.md` has entries, **continue from the last entry** - do not restart or re-plan finished steps.
Handle open inbox messages first (section 8). Skip to the step the log says is next.

## 1. Orient

- Read `$D/spec.md`, `$D/tests.md`, `$D/issues.md`, `$D/PRs.md`, and the parent spec
  (`forge path <parent_key or project slug>`/spec.md) for context and interfaces.
- `forge set-state $K in-progress --note "started"`.
- Append to `$D/log.md`: `## <UTC time> start` + one-paragraph understanding of the task.
- If the spec is ambiguous or contradicts code you find, write an issue (section 9) before building.

## 2. ClickUp subtask

If `task.clickup` is null, invoke `forge:clickup-task` to create the subtask under the parent ticket
and record it. Use its `custom_id` in branch names and PR titles.

## 3. Branch and worktree

You are already in your own git worktree (created by dispatch). In it:
```bash
git fetch origin <base_branch>
git checkout -b <CUSTOM_ID>-<short-description> origin/<base_branch>   # e.g. ENG-105-billing-dao
git submodule update --init   # repos with submodules
```
Never commit to `main`. Never edit a submodule's files in place; change the submodule's own repo.

## 4. Plan

Use **superpowers:writing-plans** to write the implementation plan to `$D/plan.md` (not the repo).
Decide PR shape now:
- One PR if the change is small and cohesive (< ~400 changed lines, one concern).
- Otherwise a stack of <= 3 PRs (e.g. contract/IDL -> core -> wiring) - follow `forge:stacked-prs`.
- More than 3 PRs or > ~15 files across > 2 modules -> the task is too big: go to section 11.
Log the decision.

## 5. Implement - test-driven

Use **superpowers:test-driven-development** (and superpowers:subagent-driven-development for
independent sub-steps if it helps). For each test in `tests.json`:
1. Write the failing test. Record: `forge tests set $K TST-n --status red --evidence "<why/where it fails>"`.
2. Implement until it passes; record `--status green --evidence "<CI run URL or output excerpt>"`.
3. New tests you discover: `forge tests add $K --name ... --type ... --command ... --expected ...`.
4. A test that cannot apply: `--status skipped --skip-reason "<concrete reason>"`.

**Repos whose CLAUDE.md forbids local test runs:** don't run them. Record the exact command in the
test, push, and use the CI run as evidence. If a local run is truly needed, ask the user (section 9).
For other repos, run tests locally and paste the relevant output excerpt as evidence.

Before every push: `./gradlew spotlessApply` on affected modules (Gradle repos); follow the repo's
CLAUDE.md coding standards. Use superpowers:verification-before-completion before claiming green.

## 6. Document

- Repo docs: update/add `docs/...` as the repo's CLAUDE.md requires (new handler/workflow/flag/
  contract change) and link it from `docs/index.md`. Paired knowledge-base PR when the Firestore
  data model changes - add it as another PR on this task.
- Task log: append decisions, dead ends and anything a reviewer or your future self needs.

## 7. PRs

- Push and open **draft** PRs (single: `gh pr create --draft ...`; stack: `forge:stacked-prs`).
- PR title `[<CUSTOM_ID>] <summary>`; body: what/why, test plan, link to the ClickUp subtask,
  stack links if stacked, and the repo's attribution line.
- Record each: `forge prs add $K --url <PR URL> --stack-index <0 = bottom>`.
- Then run `forge:ci-sonar-gate` until the gate passes; set `in-review` while waiting on CI.
- Review comments: use the `address-pr-comments` skill/command if present, else
  superpowers:receiving-code-review. Resolve threads after fixing (gate requires 0 unresolved).
- Sonar issues: fix only if the fix is < 20 lines; otherwise explain in the PR and log it.

### 7a. Walkthrough artifact (required once a PR is open)

Publish a Claude artifact that walks a reviewer through the problem and the fix, so nobody has to
reconstruct it from the diff.
- Load `artifact-design` first (and `artifact-diagramming` if listed), then write one HTML page in
  your scratchpad and publish it with the Artifact tool (private by default).
- Content, in order: the problem in 2-3 sentences; a **mermaid** diagram of the broken flow (where it
  goes wrong, highlighted); a **mermaid** diagram of the fixed flow; what changed (files/functions,
  per PR if stacked); how it is tested (test names, CI run links); risks and follow-ups. Load mermaid
  from `https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js` and render with a theme that works
  in light and dark.
- Keep it factual: every claim must match the code and evidence in the PR.
- Link it: add `Walkthrough: <artifact URL>` to each PR body (`gh pr edit`) and to `log.md`
  (not `PRs.md` - forge regenerates it from `prs.json` and drops extra lines). Republish to the same file path when the fix changes after review.
- Tasks that hand back with no PR (parked/investigation only) skip this step.

## 8. Inbox - check every major step and whenever a message arrives

Messages arrive via SendMessage from the coordinator or via a resume prompt. Regularly:
```bash
forge inbox list $K --open
forge inbox ack $K C-n                        # immediately: "seen"
# ... act on it ...
forge inbox resolve $K C-n --reply "<what you did / answer>"
```
The Stop hook blocks you from stopping while unacked messages exist. A message that changes scope
or an interface -> write an issue or confirm with the sender before acting.

## 9. Needing the user

Anything you cannot decide (scope, interface change, credentials, local test run, flaky infra):
invoke `forge:write-issue` (the issue starts in state `awaiting-user`), then
`forge set-state $K blocked --note "I-n: <title>"`. Continue on any unblocked work.

Issues carry a state (`open → awaiting-user → decided → in-progress → resolved|wontfix`) so the user
can track every decision in `decisions.md` and the UI. When an inbox message answers an issue:
```bash
forge issues list $K --state decided           # did the user already record it via the UI?
# if not: record what the user said, pointing at the message (no new message is posted)
forge issues decide $K I-n --decision "<the user's decision, one line>" --by user --comment-id C-n
forge issues set-state $K I-n in-progress --note "acting on the decision"
# ... act ...
forge issues resolve $K I-n --resolution "<decision + what I did>"
```
A UI decision arrives as an inbox message `Decision on I-n: ...` (its C-id is already in the issue's
`decision.comment_id`) - just `set-state in-progress`, act, `resolve`, and ack/resolve the message.
Then `forge set-state $K in-progress` if the task was blocked. The gate fails while any issue is
`decided` or `in-progress`. Never stall silently; never guess on a `blocker`/`decision`.

## 10. ClickUp status

Update the subtask status on transitions (in progress, in review, blocked) per `forge:clickup-task`.

## 11. Too big -> split inside your task

Invoke `forge:split` with your own key as parent. Your state becomes `coordinating`; you remain the
owner: dispatch children (`forge:dispatch`), relay their inbox (`forge:relay`), review their
handbacks (`forge:review-handback`). Keep any PRs you already opened on your task or move them to a
child (log which). Hand back only when all children are handed-back/done.

## 12. Hand back

0. **Fable review pass (required for any task with PRs).** Before handing back, dispatch an
   independent reviewer with the Agent tool: `subagent_type: "general-purpose"`, `model: "fable"`.
   Give it the PR URLs, `spec.md`, `tests.md` and the repo path; ask it to read the diffs
   (`gh pr diff`) and report only problems it can point at (file/function, line): correctness bugs,
   spec criteria not met, missing or weak tests, risky changes (compatibility, defaults, error
   handling), and docs the repo requires. No style nits. Then:
   - Fix every finding you agree with (TDD as in section 5), push, and let CI re-run.
   - For a finding you reject, note why in `log.md`; if it needs the user's call, `forge:write-issue`.
   - Append `## <UTC time> fable review` to `log.md`: findings, what was fixed, what was rejected
     and why. If you changed code, re-run the review once on the new diff.
   Tasks that hand back with no PR skip this step.
1. `forge prs refresh $K && forge gate $K` - must exit 0. Fix every reason it lists.
2. ```bash
   forge handback $K --summary "<what was delivered, PR links, walkthrough artifact URL, fable review outcome, tests evidence, follow-ups>" \
     --docs "<docs updated with paths, or 'none needed: <why>'>"
   ```
3. Update ClickUp subtask to its review status and comment the summary.
4. Notify the owner of the parent: if `task.parent_key` is set, SendMessage to that task's session
   name (`forge show <parent_key> --json` -> `session.name`); else to the project's
   `coordinator_session.name` (`cat "$(forge path <slug>)/project.json"`). Message: `Handback <K>: <2-line summary>`.
   If SendMessage fails (owner not running), that is fine - the coordinator's relay loop notices the
   `handed-back` state.
5. Append `## <UTC time> handed back` to `log.md`. Stay available: review follow-ups may come back
   through the inbox.

## Rules

- `log.md` is append-only; write to it at every step so a resume can continue.
- Do not merge PRs unless the user explicitly asks; draft is fine for the gate.
- Keep the worktree; forge does not clean it automatically.
- If the Stop hook blocks you with gate reasons, keep fixing/monitoring CI (`gh pr checks <n> --watch`)
  or set `blocked` with an issue if it needs the user.
