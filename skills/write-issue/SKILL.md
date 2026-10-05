---
name: write-issue
description: Use when a forge worker needs a decision, answer or action from the user (blocker, design decision, question, FYI) - writes a structured issue into the task folder, registers it, optionally publishes it as an artifact, and blocks the task if needed.
---

# forge: write an issue for the user

The user reads issues in the forge UI (rendered markdown + mermaid) and replies there; replies reach
you through the inbox. Write so the user can decide in under two minutes.

## Checklist

1. Pick severity: `blocker` (cannot continue), `decision` (choose between options), `question`
   (need information), `fyi` (no action, awareness only).
2. Write the body to a temp file (it is copied into `issues/I-n.md`), top-down:
   ```markdown
   # <Title - the decision or problem in one line>
   **Task:** <key> · **Severity:** <severity> · **PR/branch:** <links if relevant>

   ## Context
   2-4 sentences: what you were doing and what the spec says.

   ## Problem
   What is wrong or undecided, with concrete evidence (error excerpt, file/class names, numbers).

   ## Options
   1. **<Option A> (recommended)** - what it means, cost, risk.
   2. **<Option B>** - ...
   Why A: one or two sentences.

   ## What I need from you
   Exactly one ask: "Reply A or B", "Run `<command>` and paste output", "Grant X".
   What I'll do meanwhile: <parallel work or 'paused'>.
   ```
   Add a ```mermaid``` diagram (sequence/flow/graph) when it clarifies a flow, dependency or
   before/after design. Keep the body <= 1 page. No secrets, no log dumps (excerpt <= 20 lines).
3. Register it: `forge issues add <key> --title "<title>" --severity <sev> --body-file <tmp>` -> prints `I-n`.
   State defaults to `awaiting-user` for blocker/decision/question (it needs the user) and `open` for
   fyi; pass `--state awaiting-user` for an fyi that still needs an answer. Issue states:
   `open → awaiting-user → decided → in-progress → resolved|wontfix` (reopen: closed → `open`);
   `forge issues set-state <key> I-n <state> --note ..` validates transitions.
4. If `blocker`/`decision` and you cannot proceed: `forge set-state <key> blocked --note "I-n: <title>"`.
5. Tell the owner: SendMessage to the coordinator/parent session (see `forge:work` section 12.4)
   `Issue <key> I-n (<severity>): <title>` so it reaches the user quickly.
6. Optional - publish as an Artifact (for sharing or richer review), **from your own session** so
   artifact comments route back to you:
   - Build a single HTML page rendering the issue (headings, options, mermaid via
     cdn.jsdelivr.net/npm/mermaid); follow the Artifact tool's design rules.
   - Publish with the Artifact tool, then `forge issues set-artifact <key> I-n <url>`.
   - When an artifact comment arrives, mirror it into the inbox before acting:
     `forge inbox add <key> --issue I-n --author user --body "<comment text> (via artifact)"`,
     then handle it like any inbox message. The forge UI + inbox stay the source of truth.
7. When answered: make sure the decision is recorded - state `decided` (the user did it in the UI), or
   record it yourself: `forge issues decide <key> I-n --decision "<decision>" --by user --comment-id C-n`.
   Then `forge issues set-state <key> I-n in-progress`, act, and
   `forge issues resolve <key> I-n --resolution "<decision + what I did>"` (`--wontfix` if dropped).
   Resolve the inbox message with a reply, and `forge set-state <key> in-progress` if it was blocking.
   Log the decision in `log.md`. The gate refuses handback while an issue is `decided`/`in-progress`.
