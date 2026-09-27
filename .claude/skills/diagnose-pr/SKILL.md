---
name: diagnose-pr
version: 1.0.0
description: Diagnose guarded-path PR CI failures without pushing commits. Use when CI fails on a PR whose changed files match .ai/guards.json, or when an autofix loop detects a forbidden guarded-path touch.
type: skill
model: sonnet
data_sensitivity: restricted_us_oss_ok
max_tokens: 1200
safety_tier: amber
requires_mcp: []
required_entities: []
allowed-tools:
  - Read
  - Bash
  - mcp__github__pull_request_read
  - mcp__github__add_issue_comment
tags:
  - system
  - workflow
---

# `/diagnose-pr` — guarded-path PR diagnosis

## Trigger

Run this skill when **both** conditions are true:

1. A PR CI, static-review, PR-assessment, or autofix-loop signal failed.
2. At least one modified PR file matches a guard entry in `.ai/guards.json`
   or the hard-coded self-protection allowlist used by `/diagnose-pr`.

Guarded paths are high-stakes surfaces such as model routing, KL RLS migrations,
permissions settings, the KL safety layer, and the guards manifest itself. They
must not be fixed autonomously unless an operator explicitly chooses a proposed
option later.

## Model, consensus gate, and tools

- Use the declared guardrailed `sonnet` path for the final sanitized diagnosis comment; `restricted` inputs remain no-AI.
- Before the final comment, workflows run `scripts/guarded-consensus-review.py` unless `vars.CONSENSUS_REVIEW_ENABLED` is false. The gate asks three reviewers for JSON `{verdict: approve|reject|defer, confidence, reason}`:
  1. `sonnet` through the fail-closed advisor guardrail (`data_sensitivity=restricted_us_oss_ok`); never dispatch `restricted` data.
  2. `claude-haiku-4-5-20251001` via LiteLLM on a sanitized internal packet (`data_sensitivity=internal`, matching its `data_sensitivity_max`).
  3. `sonnet` via LiteLLM by default (`data_sensitivity=restricted_us_oss_ok`), which MUST keep the fail-closed advisor guardrail in `.claude/model-routing.json` and `deploy/litellm-proxy/sonnet_advisor_guardrail.py`.
- Consensus policy: 2-of-3 `approve` means the diagnosis may proceed. Failed/unparseable calls count as `defer`; two returned approvals still proceed, otherwise escalate with all opinions visible.
- Kill switch: `vars.CONSENSUS_REVIEW_ENABLED=false` falls back to single-model Sonnet diagnosis and logs `<!-- consensus-review:v1 -->` as disabled.
- Read-only investigation is allowed: failure logs, modified code, related design docs, ADRs, runbooks, consensus summary, and focused git history.
- GitHub comment posting is allowed.
- **Do not edit files, commit, push, force-push, dismiss reviews, rerun jobs, or change PR labels.**

## Investigation checklist

1. Identify guarded file(s) by reading `.ai/guards.json` and matching the PR file
   list against each guard path, then also check the hard-coded
   self-protection allowlist used by `/diagnose-pr`.
2. Read failed workflow logs and extract the smallest reproducible failure
   signature.
3. Read the guarded code/config diff, nearby implementation context, and tests.
4. Read relevant design context:
   - `.ai/decisions/` ADRs when names or text match the guarded surface.
   - `docs/` runbooks or policy docs referenced by the touched files.
   - Git history for the touched guarded paths (`git log --oneline -- <path>` and
     targeted `git show` as needed).
5. Read `.ai/loop-logs/consensus-review.md` when present. If consensus says ESCALATE, expose all three opinions and require operator action rather than implying autonomous continuation.
6. Check for interactions with judge/static-review, escalation comments, `test-adequacy`, and `security-boundary-test` expectations.

## PR comment format

Post exactly one PR comment using this structure:

```markdown
<!-- diagnose-pr:v1 -->
**[diagnose-pr] Guarded-path failure diagnosis**

Diagnosed head SHA: `<head-sha>`

Guarded paths detected:
- `<path>` — guard `<id>`: <reason>

Root cause:
- <one to three bullets with the most likely root cause and evidence>

Consensus review:
- Decision: <PROCEED|ESCALATE|DISABLED fallback>
- Opinions: <summarize or table the Sonnet direct, Haiku LiteLLM, and OSS LiteLLM verdicts with confidence and reason>

Options:
- **Option A — <short name>**
  - Description: <what would change and why>
  - Confidence: <0-100>%
  - Effort: <S|M|L>
  - Risk: <main risk or validation need>
- **Option B — <short name>**
  - Description: <what would change and why>
  - Confidence: <0-100>%
  - Effort: <S|M|L>
  - Risk: <main risk or validation need>
- **Option C — <short name>**
  - Description: <what would change and why>
  - Confidence: <0-100>%
  - Effort: <S|M|L>
  - Risk: <main risk or validation need>

Recommended option: <A|B|C> because <brief reason>.

Operator action:
- Reply `@diagnose-pr select A` (or `B`/`C`) to authorize that option.
- No commits were pushed by this diagnosis.
```

## Quality bar

- Include the exact PR head SHA supplied by the workflow; operator selections are rejected if the PR head changes after diagnosis.
- Keep options mutually exclusive and implementation-ready.
- Confidence scores must reflect evidence, not preference.
- Prefer linking ADRs/runbooks when they exist; state when none were found.
- If fewer than three credible fixes exist, include only A/B and explicitly say no
  third option was credible.
- If consensus rejected or deferred, keep recommendations conservative and require the operator to select a path before any commit.
- If the failure is likely unrelated to the guarded path, say so and include a low-risk option to rerun or address the unguarded failure separately.
