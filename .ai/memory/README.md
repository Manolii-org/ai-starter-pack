# Persistent Memory System

This directory stores learned knowledge across Claude Code sessions. All files are JSONL (one JSON object per line) and git-tracked so they're shared across machines.

## Files

| File | Purpose | Format |
|------|---------|--------|
| `facts.jsonl` | Quick facts, decisions, preferences | `{"type": "...", "content": "...", "tags": [], "confidence": 0.0, "created": "...", "source": "..."}` |
| `patterns.jsonl` | Learned patterns with confidence scoring | `{"type": "...", "problem": "...", "solution": "...", "rule": "...", "confidence": 0.0, "tags": [], "reinforced": 0, "created": "...", "last_seen": "..."}` |
| `sessions.jsonl` | Session summaries (appended by Stop hook or manually) | `{"session_date": "...", "branch": "...", "files_modified": [], "decisions": [], "patterns_discovered": [], "issues_unresolved": [], "tags": []}` |
| `retrospectives/session-retrospectives.jsonl` | Auto session retrospectives (Stop hook) | `{"mode":"stop","captured_at":"...","branch":"...","dysfunction_score":0,"failure_class":"unclassified",...}` |
| `archive/` | Pruned patterns + migration conflict archives | never deleted |
| `retry/` | Failed KL write-through envelopes awaiting retry | |

## Commands

| Command | Purpose |
|---------|---------|
| `/remember` | Save a fact, decision, or preference |
| `/recall <query>` | Search memory for relevant knowledge |
| `/learn` | Extract a reusable pattern from current session |
| `/evolve` | Promote high-confidence patterns to skill candidates |
| `/prune` | Archive stale, unconfirmed patterns |
| `/diagnose <error>` | Search traces and memory for similar past issues |

## Confidence Scoring

- **0.0-0.4**: Low confidence, speculative
- **0.5-0.7**: Moderate confidence, observed once or twice
- **0.8-0.9**: High confidence, reinforced multiple times
- **1.0**: Certain, verified rule

Patterns with confidence >= 0.8 and reinforced >= 3 are candidates for promotion to skills via `/evolve`.

## Archive

Pruned patterns are moved to `.ai/memory/archive/patterns-expired-{DATE}.jsonl`. They are never deleted and can be recovered.


## Session retrospectives

`session-retrospective.py` (Stop hook) always writes local JSONL under `retrospectives/`.
When `MCP_API_KEY` + `KL_ENTITY` (or `RETROSPECTIVE_ENTITY` / `.ai/config/retrospective.json`)
are set, it also writes Amber-tier KL notes/facts. Without those credentials it degrades to
local-only — consumers without a Knowledge Layer must not break.

### failure_class taxonomy

Canonical enum (single source: `scripts/lib/failure_class.py`):

`instruction-gap` · `tooling` · `environment` · `planning` · `memory-context` · `external-dependency` · `unclassified`

### Memory path migration

Repos that still use `.claude/memory/` should run:

```bash
bash scripts/migrate-memory-path.sh
```

This moves contents into `.ai/memory/`, leaves a relative symlink for compatibility, and
archives collisions under `archive/migration-conflicts/` (no-clobber).
