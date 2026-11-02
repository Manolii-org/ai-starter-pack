---
name: exploratory-bug-bash
version: 1.1.0
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
  - "launch QA"
  - "test like first-time users"
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
| `e2e.config.ts` | LiteLLM OpenAI-compatible provider; separate actor (vision) and judge routes; targets `web` (1280×720) and `web-mobile` (375×812); personas `default`/`skeptic`/`fuzzer`/`stateful` plus the launch set `impatient`/`trust-checker`/`literal`/`breaker`; budgets. |
| `tests/auth.setup.e2e.ts` | Magic-link session setup per synthetic account (Supabase example; adapt). |
| `run.py` | Binds env in-process and runs `npx e2e …` with telemetry off and `CI` unset. |
| `charters.launch.example.txt` | The five first-time-user launch charters (§ Launch QA). |
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
   (reload/back/forward), `default` (first-time user), or a launch persona
   (§ Launch QA).
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

## Launch QA (five first-time users)

Run once per launch, landing (`Shippable: yes`) or substantial onboarding
change — not per PR. It is the expensive lane: five charters at the budgets
above are roughly 40–375 model calls. Cheaper lanes must already be green:
the finalise gate, the PR `ui-smoke` against the preview, and
`ui-smoke --profile launch` (slow-3G + 4× CPU on 375px) for the deterministic
mobile/slow-network half of the brief. That last one replaces an LLM "slow
3G user": the engine has no network throttling, and a timing budget is a
measurement, not a judgement.

| Persona | Target | Constraint (one per persona, so findings stay attributable) |
|---|---|---|
| `impatient` | `web` | Takes the most prominent CTA, never reads help, quits at the first error or retry |
| `trust-checker` | `web` | Reads pricing, terms and privacy first; reports gaps or contradictions with the flow |
| `default` | `web-mobile` | Whole journey on a 375px phone; every control reachable without zoom/scroll |
| `literal` | `web` | Acts only on literal UI text; reports every label or error it must guess at |
| `breaker` | `web` | Junk input, double-submits, back mid-flow |

1. **Calibrate first** (§ Procedure 2) with the launch charters — a change to
   `e2e.config.ts` or the charters changes the fingerprint, so an older
   calibration no longer authorises the run.
2. **Explore** `charters.launch.example.txt` (edited to your real journey and
   its invariant) against an isolated preview: `./fanout.sh charters.launch.txt .e2e/out/launch 5`.
   Each persona screenshots/videos every step; candidates carry the evidence.
3. **Merge and rank** every candidate in one list by
   **severity × journey stage** — a blocker before first success outranks
   cosmetic issues anywhere — with the number of personas that hit it as the
   tiebreaker. Five synthetic users are not frequency data: one persona
   blocked on the critical path is P0.
4. **Triage** each item into the § Procedure 4 buckets. Only `confirmed`
   (a deterministic test that fails on base) is fixed in this pass;
   `design-intent` goes to the owner, `fixture` to seed data.
5. **Fix in one batched PR per area**, each fix with its regression test
   (Playwright spec, `ui-smoke` route, or unit test). The tests — not a
   persona re-run — are the proof, because model runs are non-deterministic
   and "not seen again" proves little.
6. **Re-run the five charters once** as a sanity pass on the fixed build and
   record it. A recurring candidate after the fix means the regression test
   does not cover it — extend the test, do not re-roll the explorer.

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
   candidates per bucket, confirmed PRs, and cost. `--run-at` is required and
   records the run's execution time — never the append time of a delayed
   aggregation. Append-only; a GitHub issue comment may mirror the row but
   the ledger file is the record.
4. **A scheduled runner** — e.g. a weekly Devin automation or CI
   `workflow_dispatch`-capable schedule that brings the isolated stack up,
   then gates unconditionally on
   `harness/ledger.py calibration-check <runs.jsonl> --target <t>
   --expect-fingerprint <fp>` (compute `<fp>` from the recipe in
   `harness/calibration.md`). The check reads only the JSONL, so a first
   run (or any fingerprint change) exits 11 until calibration has actually
   been persisted: exit 11 means run calibration first, triage its result,
   append the calibration row (carrying the fingerprint), and re-run the
   check. Only exit 0 proceeds to real charters — resolve the cause the
   gate reports (missing/stale/incomplete calibration, recall < 2/3,
   fingerprint mismatch), not always the charters. Then triage, append the
   weekly ledger row, and post
   the summary. Advisory end-to-end: `continue-on-error` semantics, never a
   required check.
5. **Stop rule** — `harness/ledger.py stop-rule <runs.jsonl> --target <t>`
   exits `10` when the last 3
   **completed** weekly runs (every charter accounted for) produced zero
   confirmed bugs; the lane pauses (trigger
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
