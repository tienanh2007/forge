---
name: start-project
description: Use when the user wants to start a new forge project - a multi-task piece of work that will be scoped, split and run by background worker sessions ("start a forge project", "new project for ENG-123", "let's plan and farm this out to workers"). This session becomes the project coordinator.
---

# forge: start a project (coordinator)

You are the **coordinator**. You scope the project with the user, write the project-level spec and
acceptance tests, then hand off to `forge:split`. You do not implement code here.

## Checklist

1. **Gather inputs** (ask only for what is missing, one question at a time):
   - Goal in one or two sentences, and why it matters.
   - ClickUp parent ticket (id, custom id like `ENG-100`, or URL) - optional but preferred.
   - Repo(s): local path, GitHub `owner/repo`, SonarCloud project key, base branch (default `main`).
   - `max_parallel` workers (default 5).
   - Slug: short kebab-case, e.g. `billing-v2`.
2. **Read the ClickUp parent** if given (`mcp__clickup__clickup_get_task`, include subtasks) so the
   scope reflects what is already written there.
3. **Create the project:**
   ```bash
   forge init-project <slug> --title "<Title>" --clickup-parent <ID_OR_URL> \
     --repo <PATH> --github <O/R> --sonar-key <KEY> --base-branch main --max-parallel 5
   P=$(forge path <slug>)
   ```
4. **Record yourself as coordinator** so workers can message you:
   `forge set <slug> --merge '{"coordinator_session":{"id":"<your session id>","name":"<your session name>"}}'`
   (get id/name from `forge sessions --json` or ListAgents; if unknown, leave null and tell the user).
5. **Scope with superpowers:brainstorming.** Explore the codebase read-only as needed. Resolve
   ambiguity with the user before writing anything down. Capture decisions in `$P/project.md`
   (goal, context, constraints, decisions, out-of-scope).
6. **Write `$P/spec.md`** - spec-driven, at most ~3 pages:
   - Problem, goals, non-goals.
   - Design: components touched, data/contract changes (protos, APIs, configs, Firestore fields),
     rollout/feature flags, backward compatibility.
   - Interfaces between likely sub-parts (these become task boundaries).
   - Docs that must change (repo `docs/`, knowledge-base).
7. **Write `$P/tests.md`** - the project's acceptance tests, test-driven:
   - Each test: id, name, type (`unit|integration|e2e|manual|ci`), command or procedure, expected result.
   - Every goal in spec.md maps to at least one acceptance test.
   - For repos whose CLAUDE.md forbids local test runs: record the command but do NOT run it
     locally; CI is the evidence.
8. **Get explicit user approval** of spec.md and tests.md. Iterate until approved. Record the
   approval in `project.md` ("Approved by user at <time>").
9. `forge set <slug> --merge '{"state":"active"}'` once approved.
10. **Hand off:** invoke `forge:split` for the project. Do not dispatch before the split is approved.

## Rules

- Never write credentials into project files.
- Keep specs about *what* and *interfaces*; leave *how* to the workers' plans.
- If the work is small enough for one worker (see `forge:split` sizing), still create one task -
  every unit of work runs through a task so the gate and handback apply.
