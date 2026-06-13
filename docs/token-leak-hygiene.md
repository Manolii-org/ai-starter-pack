# Token-Leak Hygiene

> **Purpose:** Defence-in-depth protection against credential leaks in Bash and tool output.
> **Applies to:** Teams using Claude Code with secrets in environment variables (API keys, tokens, database DSNs, etc.).

## Threat Model

Bash commands and agent output can accidentally echo credentials. Common incident patterns:

- **Bash expansion leak:** `echo "${API_KEY:-default}"` expands to the value when set (not the default)
- **Introspection leak:** `printenv API_KEY` or `declare -p API_KEY` prints the value
- **Credential file direct read:** `cat ~/.netrc` echoes raw PAT values
- **Sub-agent stream leak:** a sub-agent's private Bash STDOUT echoes a token before hooks can redact it

## Four Layers of Defence

### Layer 1 — Sanctioned Env-Probe Helpers

Instead of raw Bash expansions, use safe helpers from `scripts/safe_env.sh`:

| Helper | Purpose | Output |
|---|---|---|
| `safe_summary VAR_NAME` | Safe diagnostic | `"VAR: set (len=N)"` or `"VAR: unset"` |
| `is_set VAR_NAME` | Presence check | `set` / `unset` |
| `safe_prefix VAR_NAME [chars]` | First N chars, hard-capped at 8 chars regardless of N | `sk.…[REDACTED:len=45]` |
| `safe_length VAR_NAME` | Numeric length | `45` |

**Rules:**
- `safe_prefix` NEVER returns more than 8 characters, regardless of request
- `safe_summary` reports only presence and length (`set (len=N)` / `unset`) — it never returns any part of the value
- Always source first: `source scripts/safe_env.sh && safe_summary TOKEN`

**Examples:**
```bash
# Safe
source scripts/safe_env.sh
safe_summary API_KEY           # Output: API_KEY: set (len=45)
is_set DATABASE_URL            # Output: set

# Unsafe — BLOCKED
echo "${API_KEY:-default}"     # BLOCKED: :- expands to value
echo "$API_KEY"                # BLOCKED: direct expansion
printenv API_KEY               # BLOCKED: introspection
```

### Layer 2 — PreToolUse Bash Blocker

`scripts/pre-tool-use.py` scans Bash commands for dangerous patterns on token-named variables (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`, `DOPPLER_*`, `GH_*`).

**Blocked patterns:**

| Pattern | Reason |
|---|---|
| `echo "${TOKEN:-default}"` | `:-` expands to value when set — incident pattern |
| `echo "${TOKEN:+x}"` | `:+` with non-literal word (contains `$`, backtick) |
| `echo "$TOKEN"` | Direct expansion |
| `printf "%s" "$TOKEN"` | printf |
| `printenv TOKEN` | Introspection |
| `declare -p TOKEN` | Introspection |
| `env \| grep TOKEN` | Dumps the value |

**Allowed patterns:**

| Pattern | Reason |
|---|---|
| `echo "${TOKEN:+present}"` | `:+` with literal word only (safe) |
| `[ -n "${TOKEN:+x}" ]` | `:+` in non-output context (always safe) |
| `curl -H "Authorization: Bearer $TOKEN" …` | Using the value, not printing it |
| `source scripts/safe_env.sh && safe_summary TOKEN` | Sanctioned helper |

**Block message format:**
```
[BLOCKED:token-leak-pattern] do not use '${VAR:-default}' on token variables.
Safe alternatives: (1) [ -n "${VAR:+x}" ] for existence checks; 
(2) source scripts/safe_env.sh && safe_summary VAR_NAME
```

### Layer 3 — PostToolUse Stream Redactor (Advisory)

`.claude/hooks/post-tool.py` matches credential-shaped values against patterns in `.ai/security/token-shapes.json` and emits advisory `[SECRET-IN-RESPONSE]` markers for operator awareness.

**Redaction format:**
```
[REDACTED:PROVIDER_CLASS:len=52:fp=a3f1b2cd]
```

- `PROVIDER_CLASS` — token type (e.g., `GITHUB_PAT_CLASSIC`, `OPENAI_API_KEY`, `DATABASE_PASSWORD`)
- `len` — original length (so operators can reason about token shape)
- `fp` — first 8 hex chars of SHA-256(token) — unique fingerprint, safe for correlation

**Canonical patterns (`.ai/security/token-shapes.json`):**

Provider families covered:
- **Secrets managers** — Doppler (`dp.st.*`, `dp.pt.*`), HashiCorp Vault
- **AI providers** — Anthropic (`sk-ant-*`), OpenAI (`sk-*`), Groq, Together, Fireworks
- **Cloud** — AWS (`AKIA*`), Google, Azure
- **Database** — Postgres DSN, MySQL connection strings, MongoDB URI
- **SaaS** — GitHub (`ghp_*`, `ghs_*`), Stripe, Slack (`xoxb-*`), Sentry
- **Generic** — JWT (`eyJ…`), SSH private keys (PEM blocks)

**Adding new providers:**
```json
{
  "name": "MyService API Key",
  "pattern": "myserv_[a-zA-Z0-9_]{40,}",
  "class": "MYSERVICE_API_KEY"
}
```

Test against real token shapes before merging — patterns that are too loose false-positive on legitimate strings.

**False-positive resistance:**
- Allowlists: commit hashes (40/64 hex), URLs, path-shaped strings (≥2 `/`)
- Path detection catches most common false positives (package paths, branches)

### Layer 4 — Sub-Agent Stream Redaction (Optional Pattern)

Host runtimes can implement a sub-agent stream redaction wrapper using the patterns in `.ai/security/token-shapes.json`.

**Pattern (pseudocode):**
```python
# In host runtime
output = run_subagent(task)
redacted = redact_stream_fail_open(
    output,
    tool_name="execute_sql",
    subagent="secrets-handler"
)
# redacted has [REDACTED:…] markers in place of tokens
# event logged to .ai/security-events.jsonl
return redacted
```

**Fail-open design:** if pattern loading fails, returns original text unchanged + logs error event. This ensures a redaction failure never swallows output.

## Event Schema (`.ai/security-events.jsonl`)

All redaction events logged for operator audit:

| Event type | Purpose | Fields |
|---|---|---|
| `secret-shape-detected` | Legacy aggregate PostToolUse alert | `timestamp`, `severity`, `tool`, `provider_matches[]`, `count` |
| `token-leak-blocked` | PreToolUse blocker fired | `timestamp`, `command`, `pattern_class`, `reason` |
| `subagent-stream-redaction` | Sub-agent stream redacted | `timestamp`, `tool`, `subagent`, `pattern_class`, `redacted_length`, `fp` |
| `credential-file-read-blocked` | Netrc/credentials file direct read blocked | `timestamp`, `path`, `reason` |

Consumers (monitoring tools, ops dashboards) can count events by week/tool/pattern to measure leak incidents and near-misses.

## Multi-IDE Parity

| Surface | File | Enforcement |
|---|---|---|
| Claude Code (CLI/Desktop/Web) | `scripts/pre-tool-use.py` + hooks | **Automatic** |
| Cursor / VSCode | `.cursor/rules/output-token-discipline.mdc` | Self-enforce in IDE |
| Codex / Gemini CLI | `AGENTS.md` § Token-leak | Self-enforce in agent prompt |
| All IDEs | `.claude/persistent-instructions.md` | Loaded into every session |

Pattern file `token-shapes.json` is synced to downstream repos via version control.

## Testing

`scripts/tests/test_token_hygiene_redaction.py` covers:
- Every canonical provider pattern
- Idempotency (running redaction twice doesn't double-redact)
- Mixed-shape redaction (multiple tokens in one string)
- Large-stream handling (performance on 100KB+ transcripts)
- Fail-open stream behaviour (redactor failure doesn't swallow output)
- False-positive resistance (commit hashes, URLs, paths don't match)
- Fingerprint determinism (same token always produces same fingerprint)
- End-to-end incident regression tests

Run: `python3 -m pytest scripts/tests/test_token_hygiene_redaction.py -v`

## Residual Risks

1. **Sub-agent private Bash STDOUT** — if a sub-agent's Bash echo leaks a token, the current hook contract cannot rewrite it before the model sees it. Prevention: use Layer 1 helpers + Layer 2 blocker in sub-agent prompts.

2. **Novel token shapes** — patterns in `token-shapes.json` are regex-based. A new provider whose token shape isn't listed yet will not be redacted until the pattern is added.

3. **Entropy-only fallback** — high-entropy strings that don't match a known provider pattern may trigger a generic entropy heuristic (threshold=3 matches to avoid false positives). Set threshold too low and legitimate UUIDs false-positive.

4. **Operator discipline** — even with all layers, an operator can manually type `echo $TOKEN` or paste a credential into a prompt. These layers are automation safeguards, not replacements for credential management best practices (rotate keys, use short-lived credentials, least-privilege scopes).

## Best Practices

1. **Rotate keys regularly** — especially if a credential ever appeared in code/logs/transcripts
2. **Use short-lived credentials** — service account tokens with 1-hour TTL vs. long-lived API keys
3. **Scope credentials tightly** — a `read-only` database user instead of `admin`
4. **Commit hygiene** — pre-commit hook that scans staged files for credential shapes (use `run_secret_scanning.py`)
5. **Logging discipline** — never log credentials even if redaction will catch them post-hoc (prevent the incident, don't just detect it)
6. **Environment isolation** — keep dev/test credentials separate from production

## Related Docs

- `docs/mcp-response-hygiene.md` — MCP tool response filtering (secrets-handler sub-agent)
- `docs/pre-pr-quality-gate.md` — code review checklist for credential handling
- `.claude/persistent-instructions.md` — prompt-level constraints
