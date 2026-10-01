# Jev judge shadow (CI-only, observe-only)

An opt-in arm of `pr-assessment-reusable.yml` that measures whether Jev
(TypeSafe System One, pinned `jev-1.13.0`) agrees with the Judge's four
per-finding gates. Pack equivalent of Manolii master's
`JEV_ENABLED_JUDGE_FINDING_SHADOW` judge shadow.

**It never changes the review.** Judge's verdict, posted review, and job
result are identical with the arm on or off. Deterministic CI and the Judge
stay authoritative.

## What runs

After `Run judge`, the `judge` job:

1. Sparse-checks out the pack's own `scripts/jev_judge_shadow.py` at
   `refs/tags/<pack_ref>`. Callers can't substitute their own copy, because
   the next step runs it with the TypeSafe key. `pack_ref` must be a release
   tag (`vN`, `vN.N` or `vN.N.N`); a branch, SHA or PR ref skips the shadow.
   The runner refuses HTTP redirects so the key is never forwarded.
2. Reads the candidates and Judge's decision log (`.ai/judge-log/`).
3. Sends one System One request per judged finding (cap: 25 findings,
   60 s overall). Each request carries the finding, its fix, its location,
   and a ±20-line HEAD excerpt with token shapes redacted.
4. Writes `judge-jev-shadow.jsonl` and uploads it as the `judge-jev-shadow`
   artifact (14 days).

Each receipt records: entity, repo, PR, SHA, finding id, pinned model,
policy version, thresholds, `judge_kept`, Noul probabilities, `shadow_class`
(`agreement_keep` / `agreement_drop` / `false_drop_candidate` /
`false_keep_candidate` / `unavailable`), `error_class`, and latency.
Receipts never contain finding text, file paths, excerpts, provider
response bodies, or credentials.

## Activation (all must hold)

| Gate | Where | Off value |
|---|---|---|
| `jev_judge_shadow: 'on'` (literal) | caller `with:` | default `'off'` |
| `vars.JEV_ENABLED_JUDGE_FINDING_SHADOW == 'true'` | repo/org variable | anything else, e.g. `0`, is the kill switch |
| `jev_entity` matches `[a-z0-9-]{1,40}` | caller `with:` | empty |
| `JEV_TYPESAFE_API_KEY` set | caller secret mapping | unset |
| `CLIENT_AI_POLICY` empty | repo variable | any value (client engagements stay off) |
| repo owner not in `DENIED_OWNERS` | script constant | `cpdcheck` is always denied |

Each refusal is reported in the step summary, e.g. `not run (flag_off)`. In
that case no provider call is made.

**Rollback:** set `JEV_ENABLED_JUDGE_FINDING_SHADOW=0`. The caller doesn't
need editing.

## Credentials: entity-scoped only

Pass the caller entity's own TypeSafe key, for example
`TYPESAFE_API_KEY_IMPAKTFUL` for Impaktful. Never pass a Manolii or Buro key
to another entity's repo, and never pass a shared LiteLLM master key. The arm
assumes no LiteLLM URL, no Knowledge Layer entity, and no Manolii
infrastructure; it calls `https://api.typesafe.ai` directly. A
`TYPESAFE_BASE_URL` override must be `https://`.

## Fail-open

The script always exits 0, and all three steps run with
`continue-on-error: true`. Missing credentials, HTTP or transport errors,
model mismatches (an answer from `jev-latest` is rejected), malformed answers,
and budget expiry each produce an `unavailable` receipt or a refusal line.
None of them fails the job.

## CPDcheck

CPDcheck stays off:

- Its product and CI AI policy keeps all model calls on Azure OpenAI
  (australiaeast), with no shared LiteLLM and no Manolii KL entity.
- `cpdcheck` is in `DENIED_OWNERS`, so even a fully opted-in caller is refused.

Re-enabling CPDcheck needs a recorded decision on sending PR diffs to TypeSafe
and a code change that removes the owner from the denylist.

## Caller example

In the caller's `uses: .../pr-assessment-reusable.yml@<tag>` job:

- Add `pack_ref: <tag>`, `jev_judge_shadow: 'on'` and `jev_entity: impaktful`
  to its `with:` block.
- Map the entity's own key to `JEV_TYPESAFE_API_KEY` in its secret block.

Then set the repo variable `JEV_ENABLED_JUDGE_FINDING_SHADOW=true`.

## Promotion

Leave the arm in shadow until the receipts show a measured
`false_drop_candidate` and `false_keep_candidate` rate per entity, and the
Manolii master sweep (`jev-shadow-sweep.yml`) has reported on the same
policy version. Any change that lets Jev affect the verdict is a separate,
reviewed change: it gets a new `POLICY_VERSION` and its own flag.
