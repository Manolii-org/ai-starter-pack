# Advisor Escalation Format

Machine-readable specification for the executor → advisor escalation protocol used by the US-OSS pilot (Prompt 36-Advisor-06) and all future executor/advisor pairs.

**Schema duplication policy:** The event log schema in §4 is duplicated verbatim in prompts 02, 06, and 08 to keep each prompt self-contained for autonomous execution. When the schema changes, update all three copies in the same commit. A future refactor may consolidate these to `docs/advisor-schemas/advisor-usage-v1.md`.

---

## 1. Escalation Packet Schema

When an executor determines it cannot proceed with high confidence, it emits an escalation packet inline in its output. The dispatch-layer shim extracts this packet, sends it to the Anthropic advisor, and resumes the executor with the advisor's guidance.

### JSON format (preferred)

```
ESCALATE: {
  "question": "<string — the specific question the executor cannot answer>",
  "context_pack": {
    "<key>": "<value — structured data the advisor needs to reason over>"
  },
  "executor_uncertainty": 0.8
}
```

### YAML format (accepted)

```yaml
ESCALATE:
  question: "What extraction rule applies when a contract field is present in Exhibit A but absent from the main body?"
  context_pack:
    document_section: "Exhibit A, clause 4.2"
    conflicting_fields:
      - "payment_terms"
      - "governing_law"
    extraction_rule_applied: "main_body_precedence"
  executor_uncertainty: 0.85
```

### Field definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | yes | The specific question the executor cannot resolve. Must be concrete — not "I need help" but the precise decision point. |
| `context_pack` | object | yes | Structured data for the advisor to reason over. Include document excerpts, conflicting evidence, the rule being applied, and the options being weighed. Keep under 4,000 tokens. |
| `executor_uncertainty` | float 0.0–1.0 | yes | Self-rated uncertainty. See §2 for band definitions. |

---

## 2. Self-Uncertainty Rating Scale

Executors rate their confidence on a 0.0–1.0 scale. At 0.7 or above, escalation is expected. Below 0.7, the executor should proceed and note the uncertainty in its output.

| Band | Range | Meaning | Action |
|------|-------|---------|--------|
| Confident | 0.0–0.3 | Executor is certain of the correct answer | Proceed, no escalation |
| Acceptable | 0.3–0.6 | Minor uncertainty; executor has a defensible answer | Proceed, flag uncertainty in output |
| Uncertain | 0.6–0.7 | Meaningful ambiguity; output may be incorrect | Escalate if restricted workload; proceed otherwise |
| High uncertainty | 0.7–0.9 | Executor lacks the information or reasoning to proceed reliably | Escalate |
| Cannot proceed | 0.9–1.0 | Executor cannot produce a defensible answer | Escalate immediately |

> **Scale note:** This 0.0–1.0 scale applies to the executor/advisor escalation packet only. The main-thread self-audit mechanism (documented in `docs/escalation-taxonomy.md`) uses a separate 0–10 confidence scale — these are two independent systems serving different contexts.

---

## 3. Escalation Taxonomy

Executors should escalate on these concrete trigger classes. For document extraction workloads (the Advisor-06 pilot), the most common escalation reasons are the first three.

### 1. Contract negotiation / ambiguous terms

Triggered when: a document contains provisions that are non-standard, contradictory, or that require legal/business judgment to interpret.

```json
{
  "ESCALATE": {
    "question": "Clause 7.3 limits liability to 'direct damages' but Exhibit B defines 'damages' to include lost profits. Which definition governs for extraction purposes?",
    "context_pack": {
      "clause_7_3": "In no event shall either party be liable for indirect or consequential damages...",
      "exhibit_b_definition": "Damages means all losses including lost profits, lost revenue, and loss of business opportunity...",
      "document_type": "SaaS Master Services Agreement",
      "extraction_field": "liability_cap"
    },
    "executor_uncertainty": 0.82
  }
}
```

### 2. Ambiguity in extraction rules

Triggered when: the extraction schema's rules do not clearly cover the document structure encountered.

```json
{
  "ESCALATE": {
    "question": "The extraction schema expects 'payment_terms' as a single field, but this document has three separate payment schedules in different sections. How should I populate the field?",
    "context_pack": {
      "schema_field": "payment_terms",
      "schema_description": "Primary payment terms and schedule",
      "found_sections": [
        {"location": "Section 4.1", "content": "Net 30 for standard invoices"},
        {"location": "Section 4.2", "content": "Net 60 for milestone payments"},
        {"location": "Exhibit C", "content": "Net 15 for support fees"}
      ]
    },
    "executor_uncertainty": 0.75
  }
}
```

### 3. Missing field validation

Triggered when: a required field cannot be found and the executor cannot determine whether the field is absent, redacted, or in a non-standard location.

```json
{
  "ESCALATE": {
    "question": "The 'governing_law' field is not present in the main body or standard schedule locations. Is it acceptable to leave this null, or should I search the full exhibits?",
    "context_pack": {
      "extraction_field": "governing_law",
      "searched_locations": ["Section 12", "Section 15", "Exhibit D", "Signature Block"],
      "document_page_count": 47,
      "exhibits_searched": false
    },
    "executor_uncertainty": 0.71
  }
}
```

### 4. Cross-document consistency

Triggered when: multiple documents are being extracted and their extracted values conflict.

```json
{
  "ESCALATE": {
    "question": "The Master Services Agreement sets termination notice at 30 days, but the Order Form signed 6 months later sets it at 90 days. Which value should I extract as the governing term?",
    "context_pack": {
      "extraction_field": "termination_notice_days",
      "msa_value": 30,
      "msa_effective_date": "2025-01-15",
      "order_form_value": 90,
      "order_form_effective_date": "2025-07-22",
      "precedence_rule_in_msa": "Order Forms supersede conflicting MSA terms"
    },
    "executor_uncertainty": 0.78
  }
}
```

---

## 4. Parsing Rules for the Dispatch-Layer Shim

The dispatch-layer shim in `lib/advisor/client.ts` extracts escalation packets from executor output using these rules:

### Detection

1. Scan executor output for the string `"ESCALATE:"` (case-sensitive) followed by either `{` (JSON) or a newline and two spaces (YAML).
2. If multiple `ESCALATE:` markers appear in one response, use only the first. Log a warning if more than one is found.
3. The marker may appear anywhere in the output — before, within, or after prose. Extract the structured block; discard surrounding prose for escalation handling.

### JSON extraction

Use a two-step parser:

1. Locate the first `ESCALATE:` marker.
2. From the first `{` after that marker, extract a **balanced-brace JSON object**:
   - increment depth on `{`
   - decrement depth on `}`
   - stop when depth returns to 0
   - ignore braces inside JSON strings (respect `\"` escaping)

> **Do not rely on non-greedy regex-only capture for JSON blocks** — nested objects in `context_pack` are valid and must be parsed correctly.

Reference pseudocode:

```ts
function extractEscalateJson(text: string): string | null {
  const marker = text.indexOf("ESCALATE:");
  if (marker < 0) return null;
  const start = text.indexOf("{", marker);
  if (start < 0) return null;

  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === "\"") inString = false;
      continue;
    }
    if (ch === "\"") inString = true;
    else if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}
```

Parse the extracted block as JSON. If parse fails, apply the parse-failure protocol (§5).

### YAML extraction

```
regex: ESCALATE:\n((?:  .*\n?)*)
```

Parse the captured group as YAML. If parse fails, apply the parse-failure protocol (§5).

### Validation

After extraction, validate:
- `question` is a non-empty string
- `context_pack` is an object (may be empty)
- `executor_uncertainty` is a float between 0.0 and 1.0

If validation fails, treat as a parse failure (§5).

---

## 5. Parse-Failure Protocol

When the shim cannot extract or parse the escalation packet AND the request has fallen through to the quaternary provider (Groq Llama 8B), force-escalate to the advisor rather than accept ambiguous output. The quaternary provider has acknowledged weaker tool-calling; unparseable output on restricted data must not be silently committed.

### Implementation

1. Wrap escalation parser with try/catch.
2. On parse failure + `actual_provider == 'groq'` (or any quaternary): synthesize a manual escalation packet:
   ```json
   {
     "question": "Executor output on restricted data was unparseable after falling through to quaternary provider",
     "context_pack": {
       "raw_output": "<first 500 chars of executor output>",
       "actual_provider": "groq",
       "document_id": "<document_id if known>"
     },
     "executor_uncertainty": 1.0
   }
   ```
3. Send to Anthropic advisor. Advisor response becomes the authoritative output; executor's raw unparseable output is discarded.
4. Log the event with `escalation_trigger: "parse_failure_escalation"` using the schema in §6.

On parse failure from a primary/secondary/tertiary provider (not quaternary):
- Treat as a non-escalation (accept executor answer as-is)
- Log a warning to `advisor-usage.jsonl` with `outcome: "failed"`
- Do not propagate malformed escalation packets to the advisor

---

## 6. Multi-Turn Escalation Protocol

The executor may escalate up to **3 times per task**. On each round:

1. Executor emits `ESCALATE: {...}` marker.
2. Shim extracts packet, invokes Anthropic advisor with:
   - Original task context
   - All prior escalation rounds (question + advisor guidance, in order)
   - Current escalation packet
3. Advisor responds with a guidance plan (prose + structured recommendations).
4. Shim injects advisor guidance into executor context as `advisor_guidance` and resumes executor.
5. Executor continues with advisor guidance available.

### Abort condition

If the advisor response contains the phrase "proceed with best guess" (case-insensitive), the shim aborts the escalation loop and returns the executor's most recent answer as the final output. This prevents infinite escalation loops when the advisor cannot resolve the ambiguity either.

### Context packing for advisor (multi-turn)

When building the advisor prompt for round N > 1, include all prior rounds as a structured history block:

```json
{
  "escalation_history": [
    {
      "round": 1,
      "question": "...",
      "context_pack": {...},
      "executor_uncertainty": 0.8,
      "advisor_guidance": "..."
    }
  ],
  "current_escalation": {
    "round": 2,
    "question": "...",
    "context_pack": {...},
    "executor_uncertainty": 0.75
  }
}
```

This ensures the advisor has full context and does not repeat prior guidance.

---

## 7. Event Log Schema

All advisor invocations are logged to `.ai/metrics/advisor-usage.jsonl`. The schema below is the canonical definition; it is duplicated verbatim in prompts 02, 06, and 08. **When the schema changes, update all three copies in the same commit.**

```json
{
  "timestamp": "ISO 8601",
  "session_id": "string",
  "engagement_id": "string | null",
  "workload_class": "enum: restricted_doc_extraction | pr_review | code_patch | inbox_briefing_deep_dive | standing_orders_dispatch | general_main_thread | other",
  "executor_model": "string (resolved model ID, e.g. claude-haiku-4-5-20251001, accounts/fireworks/models/gpt-oss-120b)",
  "advisor_model": "string | null (null if no advisor invoked)",
  "escalation_trigger": "enum: deterministic | taxonomy | self_uncertainty | auto_injection | manual | parse_failure_escalation",
  "trigger_matched": "array<string> (populated when escalation_trigger=deterministic; file patterns or operation names that matched)",
  "task_class": "string | null (populated for auto_injection; e.g. migration, auth, security, red_tier_mutation)",
  "executor_confidence_rating": "integer 0-10 | null (populated when escalation_trigger=self_uncertainty)",
  "executor_tokens_in": "integer",
  "executor_tokens_out": "integer",
  "advisor_tokens_in": "integer",
  "advisor_tokens_out": "integer",
  "latency_ms": "integer",
  "actual_provider": "string (Fireworks / Together / Groq / Anthropic; from response headers)",
  "fallback_event": "boolean (true if actual_provider != primary)",
  "outcome": "enum: plan_followed | plan_rejected | re_escalated | failed",
  "action_type": "enum: advisor_invocation_main_thread | advisor_invocation_restricted"
}
```

### Example: parse-failure escalation event

```json
{
  "timestamp": "2026-04-18T10:23:15Z",
  "session_id": "sess-xyz789",
  "engagement_id": "engage_abc",
  "workload_class": "restricted_doc_extraction",
  "executor_model": "accounts/fireworks/models/gpt-oss-120b",
  "advisor_model": "claude-opus-4-20250514",
  "escalation_trigger": "parse_failure_escalation",
  "trigger_matched": ["executor_output_unparseable_after_quaternary_fallback"],
  "task_class": "document_extraction",
  "executor_confidence_rating": null,
  "executor_tokens_in": 8500,
  "executor_tokens_out": 3200,
  "advisor_tokens_in": 11200,
  "advisor_tokens_out": 2100,
  "latency_ms": 8900,
  "actual_provider": "groq",
  "fallback_event": true,
  "outcome": "plan_followed",
  "action_type": "advisor_invocation_restricted"
}
```

---

## 8. Proxy Fallback Provider Chain (GPT-OSS-120B executor — `restricted_us_oss_ok`)

> **Sync requirement:** This table documents the fallback chain for the GPT-OSS-120B executor used in the US-OSS pilot (direct calls to `accounts/fireworks/models/gpt-oss-120b`). It must be kept in sync with `.claude/model-routing.json`. If routing changes, update both files in the same commit.

| Position | Provider | Model | Notes |
|----------|----------|-------|-------|
| Primary | Fireworks | gpt-oss-120b | Target executor for `restricted_us_oss_ok` workloads |
| Secondary | Together AI | gpt-oss-120b | Same weights, different infra |
| Tertiary | Fireworks | gpt-oss-120b (alt key) | Alt credentials / load balancer slot |
| Quaternary | Groq | Llama 3.1 8B Instant | Degraded quality; acknowledged weaker tool-calling |

When `actual_provider` = Groq and executor output cannot be parsed, the parse-failure escalation path (§5) fires unconditionally.

---

## 9. Proxy Down / Degraded Behaviour

If the LiteLLM proxy is temporarily down (all four providers unreachable):

1. The shim catches the connection error and logs `outcome: "failed"`.
2. The extractor falls back to the Anthropic-direct path: invoke Haiku+Opus pair as defined in Prompt 04.
3. A `fallback_event: true` record is written to `advisor-usage.jsonl` with `actual_provider: "anthropic_fallback"`.
4. No data is lost; the task completes via the Anthropic fallback.

This behaviour must be documented in the platform dispatcher so operators know a proxy outage triggers automatic degradation, not task failure.
