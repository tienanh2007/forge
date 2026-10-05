---
name: status
description: Use when the user asks for the state of a forge project or task ("forge status", "how are the workers doing", "what's blocked", "which PRs are green").
---

# forge: status report

## Checklist

1. `forge prs refresh <project-or-key>` (fetch fresh GitHub + Sonar status; skip if refreshed < 2 min ago).
2. `forge tree <project-or-key> --json` - states, counts of open issues/inbox, PRs green, tests green.
3. `forge sessions --json` - which task sessions are active/idle/finished.
4. For any `blocked` or `handed-back` task, `forge show <key> --json` for gate reasons / handback summary,
   and `forge inbox list <key> --open`.

## Report format (concise)

```
<project> - <n> tasks: <x> done, <y> in progress, <z> blocked, <w> waiting review
| Task | State | Session | PRs (green/total) | Tests (green/total) | Open issues | Next |
```
Then bullets only for what needs the user: blocked tasks with the issue title, handed-back tasks
awaiting `forge:review-handback`, failing CI/Sonar, unanswered issues. Link issues/PRs.
Do not paste logs. If nothing needs the user, say so in one line.
