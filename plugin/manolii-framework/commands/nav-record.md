---
name: nav-record
version: 1.0.0
description: Record a browser navigation flow and save it as a reusable script
type: command
requires_mcp: [browserbase, playwright]
required_entities: ["*"]
safety_tier: green
tags: [workflow, browser, automation]
eval_cases: null
supersedes: []
deprecation: null
---

# /nav-record — Record a Browser Navigation Script

Based on proven patterns from the Manolii ecosystem.

Record a browser navigation flow and save it as a reusable script.

## Usage

```
/nav-record <name>
```

`<name>` must be kebab-case (e.g. `login-oauth`, `download-report`). It becomes the filename. Validate: `<name>` must match `^[a-z0-9][a-z0-9-]{0,49}$` — reject if it contains spaces, slashes, uppercase, or special characters.

## Process

### Step 1 — Gather Context

Ask the user for:
1. **URL** — where does the navigation start?
2. **Goal** — what does the flow accomplish?
3. **Environment** — `any` / `desktop-only` / `web-only`
4. **Mode** — `deterministic` / `smart` (default) / `vision`

Then ask: "Walk me through each step. For secrets, just name them—I'll reference them from environment variables."

### Step 2 — Generate Script

Convert the outline to JSON with:
- `name` = kebab-case argument
- `status` = `"proposed"`
- For secrets: use `"value": "${ENV:VAR_NAME}"` format

### Step 3 — Credential Sanitization (REQUIRED)

Scan all fields for credential patterns (JWT, API keys, tokens, etc.) and auto-replace with `${ENV:SCRUBBED_VALUE}`.

Reject if `extract` targets forbidden fields: cookies, localStorage, authorization headers, tokens.

Set `"_sanitized": true` after passing.

### Step 4 — Check Dependencies

Ask if this flow requires another script to run first. Set `"dependencies": ["<script-name>"]` if yes.

### Step 5 — Save as Proposed

Write JSON to `.ai/navigation/scripts/<name>.json`

### Step 6 — Present for Approval

Show the JSON and ask: approve / edit / cancel?

On approve: set `"status": "approved"` and save again.
On edit: apply changes → Step 3 (sanitization) → re-present.
On cancel: leave as proposed.

## Security

- Never save session tokens, passwords, or API keys in step `value`
- `${ENV:VAR_NAME}` is resolved at runtime — not stored
- `.ai/navigation/scripts/` is version-controlled — must contain zero live credentials
- Scripts with `_sanitized: false` are rejected by the replay runner

ARGUMENTS: $ARGUMENTS
