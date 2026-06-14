---
name: plan-to-codex
version: 1.0.0
description: "Write a structured Codex execution plan from the current Claude Code design discussion and print a copy-paste Codex bootstrap prompt. Does not invoke Codex."
type: skill
model: sonnet
requires_mcp: []
required_entities: []
safety_tier: green
tags: [planning, codex, handoff, workflow]
intent_phrases:
  - "plan to codex"
  - "handoff to codex"
  - "write a codex plan"
  - "make an execution plan for codex"
---

# plan-to-codex Skill

Convert the current Claude Code design discussion into a canonical Codex execution plan.
Do not run Codex, push branches, open PRs, or edit files outside the plan artifact unless the user explicitly asks.

## Inputs

Use the current conversation plus optional `$ARGUMENTS` as the source material.
If a required implementation decision is missing, ask at most three concise questions before writing the plan.

## Step 1: Choose plan identity

Derive:
- `title`: short human title
- `slug`: kebab-case, 3-7 words, no date suffix
- `date`: `YYYY-MM-DD` from the current session date
- `plan_path`: `reports/plans/<slug>-<date>.md`
- `branch`: prefer `codex/<slug>` unless the user names another branch

## Step 2: Read the canonical template

Read `reports/plans/_template.md` and follow its section order exactly.

## Step 3: Write the plan file

Create `reports/plans/<slug>-<date>.md` with:
- one-sentence goal
- concrete context links (files, docs, prior decisions)
- branch name
- files table (path + change type + reason)
- numbered atomic tasks, each with a success criterion
- one exact test command executable from repo root
- explicit out-of-scope items
- escalation triggers that tell Codex when to stop and post a PR comment

Keep the plan implementation-ready: Codex should not need to infer scope, test commands, or done criteria.

## Step 4: Validate before reporting

After writing the file, confirm:
- all required template headings are present
- branch name is populated
- every numbered task has an explicit success criterion
- test command is present and runnable
- out-of-scope and escalation triggers are non-empty

Fix any gaps before responding.

## Step 5: Print Codex bootstrap prompt

Respond with:
1. Plan path
2. Branch name
3. The copy-paste prompt below (rendered — so the user can copy it directly):

---
You are working in the `{YOUR_ORG}/{YOUR_REPO}` repo on branch `<branch>`.

First read `AGENTS.md`, `CLAUDE.md`, and `<plan_path>` fully.
Treat `<plan_path>` as the execution plan: work through the numbered tasks in order, run the plan's test command after each task where practical and at the end, do not deviate from the files/task list, and escalate if blocked by posting a PR comment: `@plan-author needs decision: <reason>`.

After implementation, commit changes, push the branch, open a PR using the plan's PR description template, and report the PR URL.
---

Do not invoke Codex yourself. Do not modify files outside `reports/plans/`.

## Token budget guardrail

Keep single paste-blocks under **50,000 tokens**. A typical 5-task plan with file anchors lands 4–8K tokens — well within budget. If a plan exceeds 50K tokens, split it: extract shared context into a separate file and open one Codex session per logical unit.
