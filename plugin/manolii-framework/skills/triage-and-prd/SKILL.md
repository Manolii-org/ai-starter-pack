---
name: triage-and-prd
version: 1.2.0
description: "Quiet safety net for a non-engineer product owner (typically Clayton) working across product code. Use judgement to decide whether the user is about to author a genuinely load-bearing change that a solutions architect should see first — the sort of change that, if it goes wrong, is expensive or dangerous to unwind (data model, sign-in / session mechanics, payment / subscription state, a brand-new deployable service). If yes, pause and offer to draft a short Product Requirements Document (PRD) instead of coding. If no — and this is the default — stay silent and let the user work. Bias hard toward silent: it's better to let the user code and course-correct than to interrupt their flow. Do not invoke on ordinary product work (new pages, new endpoints, refactors, config, workflow YAML, styling, copy, bug fixes near sensitive areas). When genuinely uncertain, do not invoke."
type: skill
model: inherit
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
tags:
  - triage
  - prd
  - risk-management
  - product-requirements
  - non-technical-user
---

# Triage & PRD Skill (v1.2 — judgement, not keywords)

**Audience:** a non-engineer product owner (typically Clayton) working in Claude Code across multiple product repos.

**v1.0 was too twitchy** — it gated ordinary work behind a PRD interview and created PR mess. **v1.1 tuned that down** with a keyword-driven trigger list. **v1.2 drops the keywords entirely** and asks you to use judgement.

## The one rule

**Default is stay-silent.** You only speak up if you'd honestly bet money that a solutions architect would want to see this before code lands. If you're weighing it — silent. Interrupting the user's flow has a real cost; being wrong about silence has almost none, because they can always come back to you.

## What "load-bearing" looks like

Not a checklist. A shape:

- The change alters how the **data model** is defined or how records relate (real schema DDL, not app-level reads/writes against an existing shape).
- The change alters **who can access what** or how identity is established (real sign-in / session / RLS logic, not the copy or error messages that live near it).
- The change alters **money movement or entitlement state** (real charge / refund / subscription-state logic, not display of prices or receipts).
- The change introduces a **brand-new deployable** that will run on its own — a new service, a new scheduled worker, a new queue consumer. Not a new route inside the existing app.
- The user is asking a **design question** rather than a build question — "how should we…", "should this be…", "let's rearchitect / replatform / migrate the DB / rewrite auth". These are asks for a plan, not for code.

Everything else is not this. New pages, new endpoints, refactors, config, workflow YAML, styling, copy, tests, bug fixes near sensitive code (as long as the sensitive logic itself isn't the thing being changed) — all pass through in silence.

## The disambiguator you can actually use

When you're unsure, ask yourself: *"If this change turned out to be wrong, would it be recoverable with a follow-up commit — or would it need a data backfill, a migration rollback, a security incident, or a user notification?"*

- **Follow-up commit fixes it → stay silent.**
- **Needs a backfill / rollback / incident / notification → speak up.**

## When you do speak up

One short paragraph naming what specifically flagged, then one question:

> *"Want me to draft a short PRD for review first, or code it and flag the risky bit in the commit message?"*

If they say proceed / just do it / code it → do the work, log one line at the top ("Proceeding without PRD as requested"), don't repeat the offer.

If they say PRD / spec it / draft it → PRD mode below.

## PRD mode

Only enter when explicitly requested (either the user asked directly, or you offered in the previous section and they chose it).

1. **Interview** — questions one at a time, short. Cover: problem, user, success measure, in-scope, out-of-scope, UX, data changes, edge cases, dependencies, rough size. Skip anything already answered. Stop when a competent developer could act on what you have.
2. **Draft** — use `references/prd-template.md`. Write to `docs/prds/<YYYY-MM-DD>-<slug>.md`. Mark unknowns as `_TBD_`.
3. **Commit + PR** on `<user>/prd-<slug>`. Message: `docs(prd): <title>`. PR title: `PRD: <title>`. Do not open a code PR. Do not start implementing after the PR is up.

## Anti-patterns

- ❌ Offering a PRD "just in case" on ordinary work. If you're not sure it's load-bearing, it isn't.
- ❌ Asking the user to justify their work before letting them code.
- ❌ Interpreting the presence of sensitive filenames in the diff (`auth/`, `.github/workflows/`, `stripe.ts`) as automatic RED. What matters is whether the *logic* changed, not whether the *file* was touched.
- ❌ Running the triage aloud on GREEN work. Silence is the correct output.
- ❌ Doing PRD-and-code in parallel. The user wants one path.

## Step 2 — What to do when you DO intervene

When one of the five triggers above clearly fires:

1. **Say one short paragraph** naming what specifically flagged (e.g., "You're editing `supabase/migrations/`, which is a real schema change") and why a PRD would help.
2. **Ask ONE question**: *"Want me to draft a short PRD first for developer review, or proceed with code and flag it in the commit message?"*
3. If the user says "proceed" / "just do it" / "code it" / anything indicating they want to keep going → do the work. Log a single line at the top of your reply: *"Proceeding without PRD as requested."* Do not repeat the offer.
4. If the user says "PRD" / "yes draft it" / "spec it" → go to Step 3.

**Do not gate.** If the user overrides, you code. No PRD side-draft, no "I'll do both", no follow-up nagging.

## Step 3 — PRD mode (only when explicitly requested)

Only enter this mode if:
- The user said "draft a PRD" / "spec this out" / "let's write requirements", OR
- You raised the flag in Step 2 and the user chose PRD.

Then:

1. **Interview.** Ask questions one at a time, short. Cover: problem, user, success measure, in-scope, out-of-scope, UX, data changes, edge cases, dependencies, rough size guess. Skip anything already answered. Stop asking as soon as you have enough for a competent developer to make decisions — don't collect for the sake of collecting.

2. **Draft.** Use `references/prd-template.md`. Write to `docs/prds/<YYYY-MM-DD>-<slug>.md`. Fill sections; put `_TBD_` where the user hasn't said.

3. **Commit + PR** on a branch `<user>/prd-<slug>`. Commit message: `docs(prd): <title>`. Open a PR with title `PRD: <title>`. Do NOT open a code PR. Do NOT start implementing after the PR is up.

## Step 4 — Anti-patterns (things v1.0 did wrong)

- ❌ Firing on generic verbs ("add", "build", "implement", "change", "refactor", "wire up"). Removed.
- ❌ Treating any file under `auth/**` or `.github/workflows/**` as architectural. Now only real logic/deployable changes count.
- ❌ Offering a PRD alongside code work. The user wants ONE path, not a fork.
- ❌ Asking "code it or PRD?" on medium-scope work. If it's not clearly RED, just code.
- ❌ Being visible on safe work. Silence is a feature.

## Step 5 — Explicit overrides (still respected)

- **"triage this: <ask>"** → run the classification aloud so the user can see your reasoning (this is the ONLY time you narrate the triage on non-RED work).
- **"draft a PRD"** → go straight to Step 3.
- **"proceed anyway"** on a RED gate → drop the gate, code.

## Notes for maintainers

- Calibrated to bcp-core (Next.js + Supabase + Vercel). The five RED triggers should port to any stack — swap "supabase/migrations" for whatever the equivalent is.
- If Clayton is still getting gated on ordinary work after v1.1, the trigger list is too broad and needs another prune. Better to under-catch than over-catch.
- If you catch yourself thinking "this is *sort of* architectural" — that's a GREEN signal, not a YELLOW one. Real RED is unambiguous.
