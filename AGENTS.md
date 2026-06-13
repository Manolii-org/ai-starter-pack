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
| **insight-miner** | `.ai/memory/` JSONL files | Surface patterns and promotion candidates (sonnet) |
| **pr-classifier** | PR diff (read) | Triages PR diff → routing manifest for skills/agents (haiku) |
| **diff-reflex** | Uncommitted diff (read) | Lightning pre-commit self-review — CRITICAL issues only (sonnet) |
| **review-internal** | Repo files (read) | Code review for own-repo PRs — correctness + OWASP Top 10 (haiku) |
| **architecture-impact** | Codebase (read) | Downstream caller count, god-node detection, breaking change risk (sonnet) |
| **ci-fixer** | CI logs + diff (read-only) | CI failure diagnosis and scoped fix proposal — propose-only (sonnet) |
| **security-deep-dive** | Codebase (read) | SAST finding triage with true-positive likelihood scoring (claude-sonnet-4-6) |
| **systems-consistency** | Deploy surface (read) | Cross-file deployment invariant checks (sonnet) |
| **judge** | Candidates + GitHub | Final PR review filter — 3-gate, only agent that posts to GitHub (sonnet) |
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
- Modifying CI/CD workflows (`.github/workflows/`, `.gitlab-ci.yml`) without explicit human review

### Database Rules
- No direct production database access without explicit approval
- All queries must be parameterized (no string interpolation)
- Migrations require rollback plans
- Use explicit timeouts on all external calls (`AbortSignal.timeout()` in JS/TS, `timeout` in Bash/curl, `asyncio.timeout()` in Python)

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
