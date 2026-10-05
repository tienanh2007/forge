---
name: review-handback
description: Use in a forge coordinator (or a coordinating worker) when a task has been handed back and needs acceptance review before being marked done ("review the handback for ENG-105", "T2 is handed back").
---

# forge: review a handed-back task

## Checklist

1. `forge show <key> --json` - read `handback.summary`, `handback.docs`, gate result.
2. `D=$(forge path <key>)`; read `$D/spec.md`, `$D/tests.md`, `$D/PRs.md`, `$D/issues.md`, tail of `$D/log.md`.
3. `forge prs refresh <key>` then `forge gate <key>` - must exit 0.
4. Review against the spec:
   - [ ] Every acceptance criterion delivered; interfaces match what the split promised.
   - [ ] Every test `green` with real evidence (CI run URL / output), or `skipped` with a sound reason.
   - [ ] PRs: checks green, zero unresolved threads, Sonar OK (>= 80% coverage on new code), drafts noted.
   - [ ] Stack order sane (`stack_index`), each PR description links the stack and ClickUp.
   - [ ] Docs: repo `docs/` updated where required (new handler/workflow/flag/contract), `docs/index.md`
         linked, paired knowledge-base PR if Firestore data model changed.
   - [ ] Decisions log: `forge issues list <key> --json` - no issue left `decided` or `in-progress`
         (a decision not yet acted on; the gate also fails on these), none `awaiting-user` or `open`
         without a note why, and each resolved decision's `resolution` says what was done. The project
         view is `$(forge path <project>)/decisions.md`.
   - [ ] Fable review ran (tasks with PRs): `log.md` has a `fable review` entry on the final diff,
         every finding is fixed or rejected with a reason, and the handback summary states the outcome.
         Missing -> send back.
   - Optionally skim diffs (`gh pr diff <n> -R <repo>`) or run superpowers:requesting-code-review.
5. **Accept:**
   - `forge set-state <key> done --note "accepted: <one line>"`.
   - ClickUp: status follows `forge set-state` automatically; check `clickup_sync` has no error (see `forge:clickup-task`),
     comment the summary + PR links.
   - `forge ready <project>` and tell the user which tasks are now unblocked (hand to `forge:dispatch`).
6. **Send back:**
   - `forge inbox add <key> --author coordinator --body "<specific, actionable list>"`.
   - `forge set-state <key> in-progress --note "sent back: <why>"`.
   - Deliver: `forge message <key> "Handback returned; see inbox."`; on exit 3 use SendMessage to the
     session name, then `forge inbox mark-delivered <key> <C-id>`.
7. Report to the user in 3-5 lines: accepted or returned, why, what's next.
