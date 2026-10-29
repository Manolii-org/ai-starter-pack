<!-- agent-validation:begin -->

## Validate candidates in the coding session

Before the first edit, inspect the integration branch, owning instructions,
guarded paths, generators and affected callers. Keep required CI names,
fork isolation, secret scanning and production approvals intact.

Before pushing a candidate, run:

```bash
python3 scripts/agent-check.py --provision --provider codex
```

Use your actual provider (`claude`, `cursor`, `codex` or `devin`). The
repository owns commands in `config/agent-checks.json`; keep them aligned
with CI and its toolchain. Missing tools or failed installs are incomplete
validation. This explicit command is required until this host's hook
execution has a receipt. File presence alone proves no hook ran.

Iterate with focused tests, batch corrections and independently review the
settled diff once at the required scope/tier. Review contracts, generated
outputs and process seams; add regression tests for recurring defects.
Keep PRs draft while iterating; expensive draft-skipped lanes must also run
on `ready_for_review`. Follow the owning repository's feature/release lanes.

Local receipts are reusable only for matching inputs, commands and tools;
they grant no merge or protected-path authority. Run any additional owning
preflight/review gate. Classify recurring infrastructure failures separately
from source defects; use one owning incident and bounded recovery rather
than repeated code pushes. Measure total agent, workspace and CI cost.

Reusable results also bind the effective execution environment without recording
its values. Keep the ignored private cache key and dependency state local; do
not upload them. Candidates containing symlinks still run their checks, but
cannot reuse or save a cached PASS based only on Git's link text.

Install only on an isolated task branch. The installer preserves the owner's
integration `base` and optional `protected_branches`, including the current
recipe when a replacement is supplied. Policy sync keeps every previously
managed Claude instruction surface current and preserves other owner prose.

This block is generated from `docs/agent-validation.md` by
`python3 scripts/sync-agent-validation.py`; `--check` detects surface drift.

<!-- agent-validation:end -->

<!-- ai-starter-pack:development-policy:begin -->
<!-- Generated from platform/framework/skills/adaptable-development/SKILL.md; policy 1.0.1; sha256 e065684e4ad947ac3b6b8fdac475b4ba2ad80bdb37e40d8ea376b0c6d66b7371. -->

# Adaptable development and design fidelity

Build the current approved feature so likely changes stay local. Scale preparation and verification to the impact of the work; keep trivial unrelated edits proportionate.

Treat a supplied approved design as the visual acceptance contract. Match it pixel by pixel at its reference viewports and states. Reuse must support that appearance. A material visual departure requires a compelling, evidenced reason and explicit resolution of any conflict with the approved experience.

Inspect and build on existing components, modules and contracts. Share implementations when their meaning and behaviour match; keep genuine product and industry differences explicit.

Give expected variation an appropriate home: scoped themes and tokens for presentation; content/templates for wording, assets, prompts and communications; validated configuration for supported options; domain modules for differing business rules; adapters for providers.

Preserve stable identifiers, existing defaults, configuration precedence, stored data and consumer contracts. Existing fallback or error behaviour must not change incidentally during structural work. Respect product, tenant, ownership and data boundaries.

Prefer small, understandable boundaries. Introduce packages, generic engines or infrastructure when concrete reuse and lifecycle needs justify them.

For design handoffs, new features and substantial redesigns, first read `.claude/skills/design-feature-adoption/SKILL.md`: reconcile current approved intent, map requirements to the real application, identify shared versus variable behaviour, and verify visual fidelity and complete functionality.

For existing-app refactoring, first read `.claude/skills/compatibility-improvement/SKILL.md`: establish the current baseline, map affected consumers, make small reversible changes and prove preservation of relevant journeys, outputs and in-flight work.

For material changes to shared boundaries, exercise one representative variation with proportionate existing tooling. Briefly record customisation locations, evidence, limitations and deferred work. Proceed through the authorised stage using the repository's current release rules.

<!-- ai-starter-pack:development-policy:end -->

# AGENTS.md — Multi-Agent Routing

This document defines how AI agents interact with this repository.

## Agent Types

| Agent | Access Level | Purpose |
|-------|-------------|---------|
| **Claude Code (primary)** | Full read/write | Development, debugging, implementation |
| **CodeRabbit** | PR read | Automated code review on pull requests |
| **Gemini Code Review** | PR read | Secondary automated code review |
| **Sub-agents** | Scoped per definition | Parallel task execution (see `.claude/agents/`) |
| **memory-protocol** | `.ai/memory/` JSONL files | Read/write memory with correct formatting (haiku) |
| **session-retrospective** | `.ai/memory/retrospectives/` | Stop-hook capture; `failure_class` taxonomy; KL-optional |
| **insight-miner** | `.ai/memory/` JSONL files | Surface patterns and promotion candidates (sonnet) |
| **pr-classifier** | PR diff (read) | Triages PR diff → routing manifest for skills/agents (haiku) |
| **diff-reflex** | Uncommitted diff (read) | Lightning pre-commit self-review — CRITICAL issues only (sonnet) |
| **review-internal** | Repo files (read) | Code review for own-repo PRs — correctness + OWASP Top 10 (haiku) |
| **architecture-impact** | Codebase (read) | Downstream caller count, god-node detection, breaking change risk (sonnet) |
| **ci-fixer** | CI logs + diff (read-only) | CI failure diagnosis and scoped fix proposal — propose-only (sonnet) |
| **security-deep-dive** | Codebase (read) | SAST finding triage with true-positive likelihood scoring (sonnet) |
| **systems-consistency** | Deploy surface (read) | Cross-file deployment invariant checks (sonnet) |
| **judge** | Candidates + GitHub | Final PR review filter — 4-gate, only agent that posts to GitHub (sonnet) |
| **orchestrator** | Scoped per task | Multi-step DAG coordinator for parallel sub-agent tasks (sonnet) |

## Tool Routing

Agents use tools in this priority order:

1. **HTTP MCP servers** (preferred — work in web sessions)
   - GitHub MCP, Context7, Vercel MCP
   - Configured in `.mcp.json`

2. **Stdio MCP servers** (fallback — require local process)
   - Neon, Sentry, Stripe, etc.
   - Add to `.mcp.json` when needed

3. **CLI tools** (last resort)
   - `git`, `gh`, `npm`/`pnpm`, `curl`
   - Via Bash tool

Claude Code may record `InstructionsLoaded` receipts (`docs/instruction-load-observability.md`).
Codex and Cursor do not emit that host event; do not invent one.

## Security Boundaries

### Allowed
- Read any file in the repository
- Edit/write files in the repository
- Run build, test, lint, typecheck commands
- Access configured MCP servers
- Create branches and push to feature branches
- Create pull requests

### Denied
- Direct push to `main`/`master` branches
- `rm -rf /` or recursive deletion of root paths
- `git push --force` (use `--force-with-lease` if needed)
- `git reset --hard` without explicit user approval
- `git clean -f` without explicit user approval
- `git branch -D` without explicit user approval
- Hardcoding secrets in any file
- Skipping git hooks (`--no-verify`)
- Disabling required checks, secret scanning or tenant boundaries as an incidental workflow change

Authorized workflow changes may be implemented, validated, independently reviewed
and merged autonomously. Existing task and merge authorization persists; the file
being a workflow does not require another human approval. Respect remote branch
protection and fix actionable findings before merging.

**This repository is PUBLIC** — enforced by `registry-lint.py` (CI):
- Content under `registry/<universe>/`, `registry/repo/`, or
  `registry/personal/` beyond `scope.yaml` (universe content lives in
  per-org private mirrors; see docs/registry.md § Public-repo boundary)
- Private-repo slugs or infrastructure identifiers anywhere in the repo
  (PACK-SURFACE check; ratchet allowlist `registry/pack-surface-allowlist.txt`)
- Customer data, internal URLs/hostnames, or credentials anywhere — ever.
  Committing to this repo publishes to the world permanently, history
  included.

### Database Rules
- No direct production database access without explicit approval
- All queries must be parameterized (no string interpolation)
- Migrations require rollback plans
- Use explicit timeouts on all external calls (`AbortSignal.timeout()` in JS/TS, `timeout` in Bash/curl, `asyncio.timeout()` in Python)

## CI cost (GitHub Actions minutes)

See `docs/ci-cost-conventions.md`. Skip drafts on PR Assessment; Autofix is
not draft-gated. Require `needs.classify.result == 'success'` so SAST cannot
start when classify is skipped. Do not add `paths` / `paths-ignore` on PR
Assessment (`converted_to_draft` is filtered too and cannot cancel in-flight
LLM/SAST). Do not add unfiltered browser/`supabase start` PR workflows;
extract `gh run list` wait loops to `scripts/ci/` with a fake-`gh` test.

## Sub-Agent Invocation

Sub-agents are defined in `.claude/agents/` and invoked via the Agent tool:

```markdown
# Example: Spawn diff-reflex for pre-commit check
Agent(subagent_type="diff-reflex", model="sonnet",
  description="Pre-commit self-review",
  prompt="Review this diff for CRITICAL issues only:\n\n<git diff HEAD>")
```

### PR Assessment Pipeline

The full PR review pipeline runs automatically via `.github/workflows/pr-assessment.yml`:

```
pr-classifier → [specialist skills in parallel] → [broad agents if depth=broad] → judge
```

Trigger it manually for any staged diff:
```bash
python3 scripts/run-pr-classifier.py --diff <(git diff HEAD) --title "PR title"
```

### Parallel Execution
Multiple sub-agents can run in parallel for comprehensive verification:

```markdown
Spawn IN PARALLEL:
1. diff-reflex — flag CRITICAL issues in the diff
2. review-internal — full review for correctness + security
3. architecture-impact — check breaking change risk
Wait for all results. Block commit if any CRITICAL or ERROR findings.
```

## Review Integration

During an ongoing delivery task, a request to report the new head SHA is a
progress update. Continue review, repair, merge and requested post-merge checks.
A push or migration rename does not transfer ownership. Preserve unfinished
commitments and verify a real wake or accepted durable handoff before claiming
background monitoring. Honor explicit stop, handoff and implementation-only
instructions. See `docs/agent-release-experience.md` for the target release
contract; owning deployment controls remain in force until qualified adoption.

For authorized agent-managed work, review publication format alone must not add a
human gate. Apply `.claude/skills/autonomy-follow-through/review-evidence.md`:
verified current-head independent automated review can satisfy review without a
formal Approve click; empty acknowledgements and incomplete reviews cannot.
Preserve required checks, findings, explicit human holds and native merge queues.


### CodeRabbit
- Automatically reviews all PRs
- Comments are actionable — address or reply explaining why not
- Use `/pr-resolve` to address review comments

### Gemini
- Secondary review perspective
- Focus on logic errors and edge cases

### Human Reviewers
- Always highest priority
- Address all human review comments before bot comments
