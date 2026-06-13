# MCP Response-Shape Hygiene Audit

> **Purpose:** Prevent credential leaks via overly-broad MCP tool responses landing in transcript.
> **Applies to:** Teams deploying AI agents with MCP server integrations (secrets managers, databases, cloud APIs, web automation).

## Threat Model

An MCP tool may return broader data than the caller asked for. When that response lands on the main thread, the full payload enters the persistent transcript — accessible to context windows, logs, and integrations. Defect classes:

1. **Bulk dumps on narrow queries** — `get_secret <key>` returns entire secret set instead of one value
2. **Credential/token fields in normal responses** — API responses include embedded tokens or keys
3. **PII bodies on count/list requests** — metadata responses include the full document/record instead of just count
4. **Arbitrary SQL passthrough** — `execute_sql` has no query filtering, enables arbitrary disclosure
5. **Full-file contents on path queries** — file read endpoint echoes entire file on a `.env*` or credential path

## Risk Classification

- **HIGH** — credential/token fields, arbitrary SQL → arbitrary disclosure, full-table dumps from narrow query
- **MEDIUM** — broad PII or unscoped data a careful caller could over-request without realizing
- **LOW** — response strictly matches request scope

## Server-by-Server Assessment

### Secrets Manager (Doppler / 1Password / HashiCorp Vault)

**Tools reviewed:** ~30 | **Highest risk:** HIGH | **Mitigation:** Pre-routing block + subagent handling

| Operation | Risk | Rationale |
|---|---|---|
| `get_secret <key>` | HIGH | Returns the secret value directly |
| `list_secrets` | HIGH | Lists all secrets WITH values for the entire config |
| `create_secret` | HIGH | Returns full post-create secret set, not just the created key |
| `update_secret` | HIGH | Returns full post-update set, not just changed keys |
| `delete_secret` | HIGH | Returns remaining secret set after deletion |
| `get_names_only` | LOW | Names only, no values — safe enumeration |

**Mitigation:** Block value-returning operations on main thread (where responses persist in transcript). Routes to a restricted sub-agent (`secrets-handler`) with a return-contract that enforces one of three shapes:
- **Write action:** `{success, action, names_changed}` (never the values)
- **Read summary:** `{count, kind, names}` (never the values)
- **Error:** `{success:false, error_summary}` (redacted)

### Database MCP (Postgres/MySQL via `execute_sql`, schema introspection)

**Tools reviewed:** ~20 | **Highest risk:** HIGH | **Mitigation:** Query-shape filtering + subagent

| Operation | Risk | Rationale |
|---|---|---|
| `execute_sql` with arbitrary query | HIGH | Caller could `SELECT * FROM secrets` or multi-statement payload with exfiltration |
| `execute_sql` with credential-bearing column selection | HIGH | Query like `SELECT oauth_token, email FROM users` returns tokens in result set |
| `get_schema` | LOW | Table/column names and types only |
| `list_tables` | LOW | Metadata only |

**Mitigation:** Block `execute_sql` on main thread when:
- Query contains `DELETE`, `DROP`, `ALTER`, or `INSERT` (destructive)
- Query SELECTs from sensitive tables (users, oauth_credentials, audit_log, secrets, keys, tokens)
- Query includes credential-shaped column names (`api_key`, `secret`, `password`, `oauth_*`, `jwt`, etc.)
- Multi-statement detected (scan each statement separately)

Routes via `secrets-handler` with redacted summary.

### Cloud SDK (AWS / Google Cloud / Azure)

**Tools reviewed:** ~15 | **Highest risk:** MEDIUM | **Mitigation:** Output filtering

| Operation | Risk | Rationale |
|---|---|---|
| `get_credential` | HIGH | Returns credential material (access key, service account JSON, etc.) |
| `list_secrets` | MEDIUM | May include metadata with secret names; could be PII |
| `describe_resources` | LOW | Metadata/IDs only unless misconfigured |

**Mitigation:** Block `get_credential` and credential-bearing `get_*` calls; route via subagent with size-only return.

### Browser Automation (Playwright / Puppeteer / Browserbase)

**Tools reviewed:** ~20 | **Highest risk:** HIGH | **Mitigation:** Extraction-blocking

| Operation | Risk | Rationale |
|---|---|---|
| `extract_page_content` | HIGH | Can capture full HTML/text including auth tokens, form values, sensitive page data |
| `evaluate_javascript` | HIGH | Arbitrary JS execution can read localStorage, cookies, session storage |
| `take_screenshot` | MEDIUM | May reveal sensitive content on-screen |
| `navigate`, `click`, `type` | LOW | Control operations, bounded response |

**Mitigation:** Block `extract_page_content` and `evaluate_javascript` on main thread. Routes via subagent with structured summary only (no raw HTML).

### Web Scraping Service

**Tools reviewed:** ~3 | **Highest risk:** HIGH | **Mitigation:** Output filtering

| Operation | Risk | Rationale |
|---|---|---|
| `scrape_url` | HIGH | Returns full page payload; authenticated targets leak credential-bearing HTML |
| `scrape_bulk` | HIGH | Multiple payloads in one call, amplifies blast radius |

**Mitigation:** Block both on main thread; routes via subagent summary-only handling.

### Code Repository API (GitHub / GitLab)

**Tools reviewed:** ~40 | **Highest risk:** HIGH (path-conditional) | **Mitigation:** Path-blocking

| Operation | Risk | Rationale |
|---|---|---|
| `get_file_contents` on `.env*` or `*credentials*` paths | HIGH | Echoes committed secrets if repository contains them |
| `get_file_contents` on `*.pem`, `.key`, `id_rsa*` | HIGH | SSH/TLS private keys |
| `get_file_contents` on `service-account*.json` | HIGH | Cloud service account JSON (contains keys) |
| `search_code` | MEDIUM | If repository contains pushed secrets, search surface exposes them |
| `get_file_contents` on normal paths | LOW | Public code only |

**Mitigation:** Optional pattern — teams should route sensitive file reads via `secrets-handler` subagent. Deploy safeguard: use credential scanning in CI (e.g., `git secrets` or similar) to prevent credential commits in the first place.

## Cross-Cutting Mitigations

### Layer 1: Pre-Routing Blocker

`scripts/pre-tool-use.py` — main-thread dispatch containment gate. Enforces SCOPE_BUDGET / allowed_paths for write-capable agent dispatches, PR-target guards (blocks cross-repo PR misfires), and Bash token-leak guards (blocks echo/printenv on token-named variables). Add project-specific credential-shaped tool routing rules here if you want automatic redirect to the `secrets-handler` sub-agent.

### Layer 2: Sub-Agent Handler

`secrets-handler` sub-agent (restricted-tier, `restricted_us_oss_ok` route) handles all routed calls. Three return shapes:

**Write action (create/update/delete):**
```json
{
  "success": true,
  "action": "update",
  "names_changed": ["API_KEY", "DATABASE_URL"]
}
```

**Read summary:**
```json
{
  "kind": "count",
  "count": 12,
  "names": ["API_KEY", "DATABASE_URL", "...]
}
```

**Error (always redacted):**
```json
{
  "success": false,
  "error_summary": "[REDACTED]"
}
```

### Layer 3: PostToolUse Stream Redaction (Advisory)

`.claude/hooks/post-tool.py` — advisory scan for credential-shaped values and external content that may leak into transcript despite Layers 1–2. Emits `[SECRET-IN-RESPONSE]` or `[INJECTION-WATCH]` markers.

**Pattern:**
```
[REDACTED:PROVIDER_TOKEN:len=52:fp=a3f1b2cd]
```

- `len` — original token length (preserved for operator reasoning)
- `fp` — first 8 hex chars of SHA-256(token) — operators can correlate leaks without seeing values

Events logged to `.ai/security-events.jsonl` (append-only, gitignored) by `scripts/canary_tokens.py`. Note: `post-tool.py` emits `[SECRET-IN-RESPONSE]` and `[INJECTION-WATCH]` markers only; it does not write to the events file directly.

## Residual Risks

1. **MCP server-side defects** — client-side blocks only prevent transcript persistence. If the MCP server returns overly-broad data, that data is produced at the server before the client block fires.
2. **Query filtering gaps** — credential column detection is pattern-based (regex on names like `api_key`, `secret`, `password`). Novel naming (e.g., `auth_material`, `sensitive_value`) may slip through.
3. **Sensitive-table list drift** — if you add new tables storing secrets/credentials, update the regex in `pre-tool-use.py` or the block won't catch them.
4. **Web scraping at authenticated endpoints** — browser automation tools can capture full HTML from pages requiring login. Operator discipline + structured extraction (not raw HTML) is the main defense.

## Usage Patterns

**Safe (these do NOT trigger blocks):**
```bash
# Secrets manager: list names only
get_secret_names()  # Returns ["API_KEY", "DB_URL", …]

# Database: query with explicit column selection (no credential columns)
SELECT user_id, email FROM users  # Safe
SELECT api_key FROM users         # BLOCKED

# Browser: structured extraction, not raw HTML
extract_page_content(
  selector=".product-title",
  instruction="Get the product name only"
)  # Returns: "Blue Widget" — structured

# Code repo: read public code
get_file_contents("src/main.ts")   # Safe
get_file_contents(".env.local")    # BLOCKED
```

**Unsafe (these will be blocked or rerouted):**
```bash
# Secrets manager: returns full set
list_all_secrets()  # Returns {API_KEY: "sk-…", DB_URL: "postgres://…", …}  # BLOCKED

# Database: arbitrary query
execute_sql("SELECT * FROM oauth_credentials")  # BLOCKED

# Browser: raw HTML extraction
extract_page_content()  # Returns raw <body>…</body>  # BLOCKED

# Code repo: credential paths
get_file_contents(".env.production")  # BLOCKED
get_file_contents("aws-credentials.json")  # BLOCKED
```

## Testing

`tests/test_security_guards.py` covers:
- All blocked tool patterns
- Subagent routing for each tool type
- Return-contract enforcement (no credential fields in return)
- False-positive resistance (legitimate queries still work)

Run: `python3 -m unittest tests.test_security_guards -v`

## Related Security Docs

- `docs/token-leak-hygiene.md` — stream-level token redaction, Bash helper functions, credential file guards
- `.claude/persistent-instructions.md` — prompt-level constraints
- `docs/pre-pr-quality-gate.md` — code review checklist for MCP tool calls
