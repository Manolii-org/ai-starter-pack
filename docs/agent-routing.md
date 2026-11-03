# Agent routing catalogue

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
