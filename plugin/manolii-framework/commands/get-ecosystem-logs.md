---
name: get-ecosystem-logs
version: 1.0.0
description: Unified log fetching across deployment platforms
type: command
model: haiku
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
tags: [debugging, infrastructure, logs]
supersedes: []
deprecation: null
eval_cases: null
---

# /get-ecosystem-logs

Based on proven patterns from the Manolii ecosystem.

Fetch and normalize logs from your deployment services.

## Usage

```
/get-ecosystem-logs service=<fly|vercel|neon|supabase> [time_range=<1h|6h|24h>] [filter=<keyword>]
```

## Supported Services

- **fly** — Fly.io
- **vercel** — Vercel (serverless, edge functions)
- **neon** — Neon PostgreSQL
- **supabase** — Supabase PostgreSQL

Consult your project documentation to see which services are deployed.

## Behavior

1. Validate `service` ∈ {fly, vercel, neon, supabase}; validate `time_range` ∈ {1h, 6h, 24h}
2. Route to the appropriate log source (Fly API, Vercel API, Neon API, or Supabase API)
3. Fetch up to 100 log lines for the given time range; apply keyword filter if provided
4. Normalize all output to `[TIMESTAMP] [LEVEL] [SERVICE]: message`
5. Apply credential redaction (Bearer tokens, `sk-*` keys, base64 blobs)
6. Display logs; emit `[WARN] service unavailable` per service on failure (partial success OK)
7. Never persist log content to memory — logs may contain PII or secrets

**Defaults:** `time_range=1h`

After display, offer: "Would you like me to diagnose similar past issues from project memory?"

ARGUMENTS: $ARGUMENTS
