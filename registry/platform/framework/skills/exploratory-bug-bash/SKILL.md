---
name: exploratory-bug-bash
version: 1.0.0
description: "Advisory AI exploratory testing (tester-army/e2e `explore`) against an isolated stack: LiteLLM actor/judge routing, bounded budgets, planted-bug calibration, and deterministic Playwright reproduction before any finding is confirmed."
type: skill
disable-model-invocation: true  # explicit /bug-bash or scheduled-session use only; never auto-invoked
data_sensitivity: internal
safety_tier: amber
requires_mcp: []
required_entities: []
tools:
  - Read
  - Bash
  - Write
tags:
  - testing
  - exploratory
  - browser
intent_phrases:
  - "run a bug bash"
  - "exploratory test the app"
  - "find bugs nobody wrote a test for"
---

# Skill: Exploratory Bug Bash

Deterministic Playwright suites only catch failures someone already imagined.
This lane lets a vision agent explore the app against written charters and
report **candidates**. A candidate becomes a finding only after it is
reproduced by a deterministic Playwright test that fails on the base commit.

**Advisory only.** Never a required check, never in a merge gate, never
blocks a deploy. `e2e explore` exit code `1` means "candidate reported", not
"product defect".

## Preconditions (stop if any is false)

1. An **isolated** target: local stack or a disposable preview with its own
   database. Never shared staging, UAT or production — exploration mutates data
   (posts, groups, settings) and the agent has no domain allowlist.
2. **Synthetic accounts** only, signed in without typing passwords (magic-link
   token exchange, API-issued cookie or saved storage state). Password fills
   taint screenshots and the agent's evidence.
3. LiteLLM proxy URL + key available in the environment (never printed).
4. Node `>=22.12`.

## Harness

Copy `harness/` from this skill into a scratch directory **outside** the app
repo (its pins conflict with app Playwright versions), then `npm ci`.

| File | Purpose |
|---|---|
| `package.json` | Exact pins: `e2e@0.15.1`, `@e2e-dev/web@0.11.1`, `playwright@1.63.0`. e2e is pre-1.0 — bump deliberately, re-calibrate after. |
| `e2e.config.ts` | LiteLLM OpenAI-compatible provider; separate actor (vision) and judge routes; personas `default`/`skeptic`/`fuzzer`/`stateful`; budgets. |
| `tests/auth.setup.e2e.ts` | Magic-link session setup per synthetic account (Supabase example; adapt). |
| `run.py` | Binds env in-process and runs `npx e2e …` with telemetry off and `CI` unset. |
| `fanout.sh` | Runs a charter file in parallel, one output dir + log + exit line per charter. |
| `charters.example.txt` | `slug|target|agent|charter` format. |

### Environment contract

| Var | Meaning |
|---|---|
| `BB_LITELLM_URL` / `BB_LITELLM_KEY` | LiteLLM proxy base + key (map from `LITELLM_PROXY_URL` / `LLM_API_KEY`). |
| `BB_ACTOR_MODEL` | Default `candidate-luna-vision` (must accept images). |
| `BB_JUDGE_MODEL` | Default `candidate-luna-critic`. Keep actor ≠ judge. |
| `BB_APP_URL` | Target base URL (default `http://localhost:3000`). |
| `BB_APP_CONTEXT` | One paragraph: what the app is, what is stubbed locally, and what is **not** a bug. |
| `BB_ACCOUNTS` | JSON map of session name → synthetic account email, e.g. `{"bb-alice":"alice@example.test"}`. Must cover every `Sign in as session <name>` in the charters file; `auth.setup.e2e.ts` creates sessions only for these entries (default `{}` creates none). |
| `BB_SUPABASE_URL` / `BB_SUPABASE_SECRET_KEY` | Isolated stack's auth URL + key for the magic-link exchange in `auth.setup.e2e.ts` (Supabase example). |
| `E2E_TELEMETRY_DISABLED=1` | Always (set by `run.py`). |

Model changes go through the `assess-model` protocol; reuse existing routes.

### Budgets (defaults that produced signal in Phase 0)

`--max-steps 6`, `maxModelCalls 40`, `--timeout 600000`, `workers 1` per
charter, fan-out parallelism 4, `retries 0`, `--video` +
`--reporter list,markdown`. One charter ≈ 8–75 model calls and 0.07–2M
tokens (mostly cached). Do not raise `--max-steps` before checking that the
charter is specific enough.

## Procedure

1. **Write charters** — one user goal per line, naming the account session,
   the start URL and what to cross-check. Pick a persona per charter:
   `skeptic` (counts/dates/names), `fuzzer` (input matrices), `stateful`
   (reload/back/forward), `default` (first-time user).
2. **Calibrate (mandatory before trusting a run, and after every model or e2e
   bump).** In a separate worktree plant 3–4 small, realistic bugs on the
   charter paths (off-by-one count, relaxed validation, dropped field on save),
   start that build on its own port, and run the same charters against it.
   Recall = planted bugs reported / planted. Below 2/3 → fix charters or
   budgets before running real charters. Record recall in the report — or in
   the ledger for recurring runs: `harness/ledger.py calibration-check` is
   the deterministic gate a scheduler runs before any real charters (exit 0
   = calibrated, 11 = uncalibrated or recall < 2/3).
3. **Explore** the unmodified build: `./fanout.sh charters.txt .e2e/out/real 4`.
   `exits.txt` records each charter's exit code: `0` clean, `1` candidate
   reported, anything else a harness error (missing env exits `2`) — rerun
   those charters (same outdir; each rerun replaces that charter's code)
   before triage. `fanout.sh` exits nonzero if any charter in the invocation
   errored.
4. **Triage** each candidate in `summary.md` into exactly one bucket:
   - `confirmed` — reproduced by a deterministic Playwright/unit test that fails
     on base and passes with the fix. Only this bucket is a bug.
   - `fixture` — caused by thin/missing seed data (verify in the DB).
   - `design-intent` — behaviour may be deliberate; ask the owner.
   - `judge-error` — screen evidence contradicts the claim (e.g. Save was
     disabled).
   - `unconfirmed` — could not reproduce within the time box.
   Same title in planted and real runs → it is pre-existing, not planted.
5. **Land confirmed bugs** as a normal fix PR with the regression test. The
   deterministic test, not the explorer, is what guards it from then on.
6. **Report**: charters run, recall, candidates per bucket, confirmed bug PRs,
   model calls/tokens, wall time.

## Operationalising as a weekly advisory lane

For a recurring cadence (one bounded pass per week against an isolated
stack), the lane needs five committed artifacts beyond the harness:

1. **Calibration set** — ≥3 planted-bug `.patch` files committed to the APP
   repo (see `harness/calibration.md`), applied only in a scratch worktree on
   a dedicated port. Must include a count/total bug, a validation bug, and a
   dropped-field-on-save bug. Re-run whenever the actor/judge model or `e2e`
   pin changes, or after ~90 days; recall < 2/3 pauses real runs until
   charters are fixed.
2. **Charters with explicit invariants** — one goal per line naming the check,
   not just the area: "the card count equals the detail count after each
   toggle" not "check counts". Invariants are what the judge verifies; a
   broken invariant is the candidate.
3. **Durable run ledger** — one JSONL row per run committed to the app repo
   (see `harness/ledger.py append`), carrying charters, exits, recall,
   candidates per bucket, confirmed PRs, and cost. Append-only; a GitHub issue
   comment may mirror the row but the ledger file is the record.
4. **A scheduled runner** — e.g. a weekly Devin automation or CI
   `workflow_dispatch`-capable schedule that brings the isolated stack up,
   runs calibration, then runs `harness/ledger.py calibration-check` — only
   when it exits 0 does the run proceed to real charters; exit 11 pauses
   them (fix charters first). Then triage, append the ledger row, and post
   the summary. Advisory end-to-end: `continue-on-error` semantics, never a
   required check.
5. **Stop rule** — `harness/ledger.py stop-rule` exits `10` when the last 3
   weekly runs produced zero confirmed bugs; the lane pauses (trigger
   disabled, ledger note) until charters or models change. Three empty weeks
   is the agreed cost ceiling. After a charter/model change, append a
   `kind=reset` row — the stop-rule window only counts weekly runs after the
   latest reset, so the resumed lane gets a fresh 3-run window.

## Do not

- Put `e2e explore` (or any LLM step) in a required check or merge gate.
- Point it at shared staging/UAT/prod or real accounts.
- Treat exit `1`, a screenshot, or the judge's severity as proof.
- Commit `.e2e/` artifacts (screenshots/videos may contain data).
- Print `BB_LITELLM_KEY` or service-role keys; `run.py` binds them in-process.
