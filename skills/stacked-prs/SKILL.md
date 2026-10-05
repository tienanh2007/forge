---
name: stacked-prs
description: Use when a forge task's change should ship as a stack of dependent PRs (2-3 PRs built on each other) - creating, submitting, restacking after review, syncing after merges, with git-spice.
---

# forge: stacked PRs with git-spice

`git-spice` (alias `gs`) is installed. Commands verified against its `--help`. Stack <= 3 PRs;
bottom = most foundational (contract/IDL, then core, then wiring/UI).

## One-time per repo/worktree

```bash
git-spice repo init --trunk <base_branch> --remote origin
git-spice auth status || echo "ask the user to run: git-spice auth login"
```
Set drafts by default: `git config spice.submit.draft true`.

## Build the stack

Start from an up-to-date base (your worktree, see `forge:work` section 3):
```bash
# PR 1 (bottom): stage the changes, then
git-spice branch create <CUSTOM_ID>-p1-<desc> -m "[<CUSTOM_ID>] <part 1 summary>"
# PR 2, on top of PR 1:
git-spice branch create <CUSTOM_ID>-p2-<desc> -m "[<CUSTOM_ID>] <part 2 summary>"
git-spice log short            # verify the order
```
More commits on the current branch: `git-spice commit create -m "..."` (restacks upstack branches);
amend: `git-spice commit amend`. Navigate with `git-spice down` / `git-spice up` / `git-spice bottom`.
Run `./gradlew spotlessApply` (Gradle repos) on each branch before submitting.

## Submit

```bash
git-spice stack submit --draft --fill          # creates/updates a draft PR per branch
# or a single branch: git-spice branch submit --draft --title "..." --body "..."
```
git-spice adds a navigation comment listing the stack. Then edit each PR body
(`gh pr edit <n> --body-file ...`) to include: position (`PR 2/3`), links to the other PRs in the
stack, the ClickUp subtask link, what/why, test plan.

Record every PR with its position (0 = bottom):
```bash
forge prs add <key> --url <PR1 url> --stack-index 0
forge prs add <key> --url <PR2 url> --stack-index 1
```

## After review changes

```bash
git-spice branch checkout <branch-with-feedback>
# edit, stage
git-spice commit create -m "address review: ..."   # restacks branches above
git-spice stack submit                               # force-pushes updated branches, keeps draft state
```
If a restack conflicts: resolve, `git add`, `git-spice rebase continue` (or `git-spice rebase abort`).
Manual restack of everything: `git-spice stack restack`.

## Merging (bottom-up, only when the user asks)

1. Merge the bottom PR (its checks green, approved).
2. `git-spice repo sync --restack upstack` - deletes merged branches, retargets the next PR to trunk,
   restacks the rest.
3. `git-spice stack submit` to push the retargeted branches; `forge prs refresh <key>`.
4. Repeat for the next PR. Never merge a PR above an unmerged one.

## Rules

- Each PR must build and pass CI on its own (the gate checks every PR).
- Don't mix `git rebase` by hand with git-spice tracking; if you must, `git-spice branch track` afterwards.
