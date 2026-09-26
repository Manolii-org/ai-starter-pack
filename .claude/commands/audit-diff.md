---
name: audit-diff
version: 1.0.0
description: "Pre-push adversarial audit of the staged (or last-committed) diff via codex-adversarial. Cheap safety net before opening a PR."
type: command
requires_mcp: []
required_entities:
  - "*"
safety_tier: green
tags:
  - diagnostic
  - read-only
  - tooling
eval_cases: null  # TODO: Prompt 16
---

# /audit-diff

**Purpose:** shift pre-push adversarial review left. In an incident that
motivated this command, adversarial review found 7 P1 bugs across 6 rounds —
each one would have been a same-session fix if audit had happened before push. This command runs the
same adversarial reasoning pass on the current diff, in-session, before the
first push.

## When to use

- Before pushing a branch that opens a PR.
- Before a mid-PR revision that touches multiple process boundaries.
- Any diff that spans ≥2 files across ≥2 subsystems.

## When to skip

- Docs-only changes.
- Test-only changes.
- A pure revert.
- A change where you've already run `/verify-work --deep` or the
  `docs/multi-boundary-feature-checklist.md` in this session.

## Behavior

1. Resolve the diff scope:
   - If `$ARGUMENTS` is empty: diff `git diff --cached` (staged) if non-empty,
     else diff the last commit (`git diff HEAD~1 HEAD`).
   - If `$ARGUMENTS` is a commit / ref / range: diff that.
2. If the diff is empty, report "nothing to audit" and exit.
3. Dispatch a `general-purpose` subagent (NOT `codex-adversarial` —
   that agent is gated on `PR_ASSESSMENT_CODEX_ENABLED=1` for CI cost
   control and would exit immediately here). Attach the diff and give
   the subagent the following brief verbatim:

    ```
    You are running as a pre-push adversarial reviewer of the attached
    diff. Not a general code review — look ONLY for the specific class
    of bugs that survive normal review:
      - Silent-drop paths (`try: ... except: pass`, missing branches)
      - Cross-process env / path / file assumptions (env -i, relative paths,
        temp workspace fs boundaries)
      - Fallback lookup chains where the fallback has different
        security/correctness properties than the primary
      - Set operations that OVERWRITE when semantics say "monotonic"
      - Filters (`--skill X`, `--only`) that narrow the input when the
        persistence path assumes full-set
      - Regex traps: JS global-regex lastIndex state, `\b` on non-word chars,
        replacer-array recursion in JSON.stringify
      - Hardcoded numbers / entity lists that will drift when the code changes
    Return P1s only. For each: file:line, one-sentence failure scenario, one
    concrete fix. Skip stylistic nits. If you find nothing, return an
    empty findings list.
    Return length cap: <300 words.
    ```

4. Report the findings to the operator. Do NOT auto-apply — the operator
   evaluates each finding and either fixes it inline or dismisses it with
   reasoning.

### Why not `codex-adversarial`?

`codex-adversarial` is the CI adversarial-pass agent — it exits immediately
unless `PR_ASSESSMENT_CODEX_ENABLED=1` is set (see
`.claude/agents/codex-adversarial.md` § Gate), and that env var is only
set inside the PR assessment pipeline for cost control. A pre-push audit
triggered by the operator wants the same reasoning **without** the
CI gate, so we route to `general-purpose` with the brief inlined. This
also means `/audit-diff` cannot regress the CI-side cost gate.

## Auto-invocation guidance

Not auto-invoked. The main-thread executor should suggest `/audit-diff` when
the user is about to push a branch that:
- Adds a new file that composes with existing modules.
- Spawns a subprocess (`subprocess.run`, `child_process.spawn`, `sh -c`).
- Modifies an `.mcp.json`, `settings.json`, or workflow YAML.
- Adds a new env var that must reach a grandchild process.

## Related

- `docs/multi-boundary-feature-checklist.md` — six-item pre-push pass.
- `/verify-work --deep` — full work-critic (heavier, uses sonnet).
- `general-purpose` subagent — receives the inlined adversarial-review
  brief (see § "Why not codex-adversarial?" above for why we don't route
  through the CI-gated `.claude/agents/codex-adversarial.md`).
