---
name: ci-sonar-gate
description: Use when a forge task has PRs open and must get CI checks green, review threads resolved and the SonarCloud quality gate passing (80% coverage on new code) so that `forge gate` passes - including when the Stop hook blocks with gate reasons.
---

# forge: CI + Sonar gate loop

Goal: `forge gate <key>` exits 0. Loop until it does, or until something needs the user.

## Loop

1. `forge set-state <key> in-review --note "waiting on CI"` (if not already).
2. `forge prs refresh <key>` then `forge gate <key> --json` - read `reasons`.
3. For each PR with failing/pending checks:
   ```bash
   gh pr checks <n> -R <owner/repo>                 # overview
   gh pr checks <n> -R <owner/repo> --watch --fail-fast   # wait while pending (run in background if long)
   gh run list -R <owner/repo> --branch <branch> --limit 5
   gh run view <run-id> -R <owner/repo> --log-failed | tail -200
   ```
   - Fix real failures on the right branch (stack: fix on the lowest affected branch, restack).
   - Flaky/infra failure: rerun once `gh run rerun <run-id> --failed -R <owner/repo>`; if it fails
     again the same way, write an issue (`forge:write-issue`, severity `blocker`) and set `blocked`.
   - Record CI evidence on tests: `forge tests set <key> TST-n --status green --evidence "<run url>"`.
4. SonarCloud (when `sonar_project_key` is set). Token `SONAR_TOKEN` from env or `~/.config/forge/env`:
   ```bash
   curl -s -u "$SONAR_TOKEN:" "https://sonarcloud.io/api/qualitygates/project_status?projectKey=<key>&pullRequest=<n>"
   curl -s -u "$SONAR_TOKEN:" "https://sonarcloud.io/api/issues/search?componentKeys=<key>&pullRequest=<n>&resolved=false&ps=100"
   curl -s -u "$SONAR_TOKEN:" "https://sonarcloud.io/api/measures/component_tree?component=<key>&pullRequest=<n>&metricKeys=new_coverage,new_uncovered_lines&strategy=leaves&ps=100"
   ```
   - Coverage < 80% on new code: add tests for the uncovered new lines (the component_tree call lists
     the worst files), push, re-check.
   - Issues: fix those whose fix is < 20 lines; for larger ones explain in a PR comment, log it, and
     raise an issue if they block the gate.
   - Status `NONE` = not analysed yet -> wait for the Sonar check to finish.
5. Unresolved review threads: address them (`address-pr-comments` if present, else
   superpowers:receiving-code-review), push, reply and resolve each thread
   (`gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`).
   Thread ids are in `prs.json` -> `status.threads.items[].id`. Don't resolve a thread you disagree
   with without replying; if a reviewer's ask conflicts with the spec, write an issue.
6. Before each push: `./gradlew spotlessApply` on affected modules. Then go to step 2.

## Exit conditions

- Gate passes -> return to `forge:work` section 12 (handback).
- Needs the user (credentials, reviewer decision, persistent infra failure) -> `forge:write-issue`,
  `forge set-state <key> blocked --note "I-n ..."`. The Stop hook stops blocking you after 5 tries
  and sets `blocked` itself - don't rely on that; set it deliberately with a reason.
- If the repo forbids running its tests locally (its CLAUDE.md says so), never run them to "check";
  CI is the evidence.
