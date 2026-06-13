---
description: "Test all configured services and report pass/fail"
---

# Health Check — Service Connectivity

Test all configured services and report pass/fail.

## Checks

### 1. MCP Servers

For each server in `.claude/mcp.json`:
- Attempt connection
- Report: name, status (pass/fail), latency

### 2. API Keys

For each known API key in environment:
- Make a lightweight validation call (e.g., list models, whoami)
- Report: key name, status (valid/invalid/missing)

### 3. Database

If database URL is configured:
- Test connection with a simple query (`SELECT 1`)
- Report: connection status, latency

### 4. Git Remote

- `git remote -v` — verify remote is accessible
- `git fetch --dry-run` — test connectivity

### 5. Package Manager

- Check `node_modules/` exists
- Verify lockfile matches (`npm ls --all` or `pnpm ls`)

## Output Format

```text
Service Health Check — {DATE}
================================
MCP Servers:
  ✓ github        (120ms)
  ✗ context7      (timeout)

API Keys:
  ✓ GH_TOKEN      (valid)
  ✗ VOYAGE_API_KEY (missing)

Database:        ✓ connected (45ms)
Git Remote:      ✓ accessible
Dependencies:    ✓ installed
================================
Overall: 5/7 passing
```
