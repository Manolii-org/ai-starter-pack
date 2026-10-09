# Codex security remediation — PACK-TEMPLATES receipts (2026-10-09)

Source: Codex cloud diff-scan board export (`repo=master`, severity critical|high,
`path` under `templates/ai-starter-pack/`) + 9 synced-workflow findings receipted
in KL PR #1112 + master-fix verification (#6110, #6100, #6101, #6102).
Pipeline: `prompts/security/codex-security-finding-resolve.md` (validation-first).
Remediation session: https://app.devin.ai/sessions/b3dbd832a5e945e684d0dd9c79532fef

## Fixed in this PR (must_fix → draft PR)

| finding | surface | fix |
|---|---|---|
| 0b93684e (critical) | `.github/workflows/pr-autofix-loop.yml` | PR head ref through env var + `git check-ref-format`; checkout pinned to preflight-validated head_sha; instruction surface restored from base SHA |
| 920ebb3b | `.github/workflows/pr-assessment.yml` | `persist-credentials: false` on all checkouts; `.claude`, `scripts`, `CLAUDE.md`, `AGENTS.md` restored from `pull_request.base.sha` in classify/specialists/broad-agents/judge |
| fa2a829d, 6ed192c9 | `.github/workflows/pr-assessment-reusable.yml` | pull_request ⇒ probe forces `needed=true`; hydration force-overwrites runtime files + `.claude` from `inputs.pack_ref` (defaulting to the moving `v1` alias) — caller-shipped runtime can no longer poison the secret-bearing lane (dormant `sp05` style lanes get the same protection when enabled) |
| d44ba37b | `pr-autofix-loop.yml` tier-2 prompt | 30KB cap + explicit UNTRUSTED-data framing for CI logs/diffs/comments |
| 5bc596ce, f79b8ffc | `.github/workflows/release-tag.yml` | Bearer token via `curl -H @-` stdin, never argv |
| 11900f53 | `deploy/litellm-proxy/fly.toml`, README | GHCR image pinned by digest `sha256:175cad…` (tags mutable) |
| 5aba4592, b32f51e5 | `.claude/hooks/session-start.sh` | no longer sources caller-controlled `scripts/load-ecosystem.sh` from the workspace |
| 5c763818 | `session-start.sh`, `scripts/cross-repo-preflight.sh` | cached secrets moved out of `.git/` to `$XDG_CACHE_HOME/ai-starter-pack-session/` |
| 0edfb370 | `.claude/settings.json.jinja` | destructive-command deny list restored (rm -rf root/home, force push, pipe-to-shell, netrc writes, approval-bypass MCP tools). `.ai/guards.json` flags `permissions` — human review required |
| 685b5693 | `deploy/litellm-proxy/config.yaml`, `.claude/model-routing.json`, `scripts/check-oss-routing.py` | `claude-haiku-4-5-20251001` → real `anthropic/` backend (was silently remapped to Fireworks gpt-oss-120b); OSS offload kept under honest `haiku` alias; tier fallbacks repointed; assessment: `.ai/assessments/claude-haiku-4-5-20251001-2026-10-09.md` |

## Already resolved / N-A in pack

| finding | verdict | evidence |
|---|---|---|
| 4cdcccb9 | already-resolved | `pr-autofix-loop.yml` lines ~52-65: `author_association` gated to OWNER/MEMBER/COLLABORATOR on all entry triggers |
| 1fe9b9da, 4c3fc581 | N-A in pack | the pack ships no auto-merge workflow — the dep-bot lane lives only in master's `templates/ai-starter-pack/core/` sync-source; fixed in the companion master PR |
| #6110 equivalent | N-A | no `disablePullRequestAutoMerge` call sites in the pack |
| #6100 equivalent | applied | base-SHA restore blocks added to pr-autofix + pr-assessment + reusable hydration |
| #6101 equivalent | already-gated | `pr-assessment.yml` runner selector already restricts self-hosted pool to same-repo PRs; portable template keeps `ubuntu-latest` default |
| #6102 equivalent | applied | `.claude`/instruction-surface restore from base SHA added to both PR workflows |

## UNCERTAIN — human decision required

| finding | question | position |
|---|---|---|
| 6de394ba | Demote `sonnet`/`claude-sonnet-4-6` from the `restricted_us_oss_ok` route | `sonnet_advisor_guardrail.py` is wired fail-closed in config.yaml (Groq Llama-3.3-70B veto), which is the designed control for this tier. Demoting sonnet re-routes `secrets-handler` and the assessment pipeline — a routing-policy decision under the Model Change Protocol, not a scan-fix. Recommend operator review of the guardrail rather than silent demotion. |
| 838c93db | Same for the `sonnet` short alias (US-only data → non-US OSS) | Same reasoning — the advisor guardrail is the intended mitigating control; whether PRC/OSS backends may ever serve this alias is a policy call. |
| 258a521d | `MN_GH_AUTOMATION_TOKEN` is broad cross-repo | Mitigation is secret provisioning, not code: provision a fine-grained PAT limited to the repos autofix serves. Code already falls back to `GITHUB_TOKEN` when absent — org-side narrowing recommended. |

## Residual risk noted (not a finding fix)

- `5c763818` residual: `session-start.sh` writes selected cached credential values to `CLAUDE_ENV_FILE` as `KEY=VALUE` entries (not `export` statements) — the hook itself does not establish a session-level export; any propagation is performed by the hook host reading that file. The *on-disk cache* moved out of the checkout; mitigation beyond this is a session-env policy decision.
