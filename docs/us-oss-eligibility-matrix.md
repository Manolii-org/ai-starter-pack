# US-OSS Eligibility Matrix (OPTIONAL — If `oss_routing` Feature Enabled)

> **Framework, not mandate.** This matrix applies **only if your setup includes OSS model routing** (check `.ai/setup-complete` for `oss_routing=true`). It provides guidance for task-class eligibility when cost and capability trade-offs favour non-Anthropic models. Reframe table headings and examples for your org's naming and policy.

**Tier:** `restricted_us_oss_ok` (engagement-policy capability — if enabled)
**Policy gate:** Your contract or internal policy permits US-origin OSS models; stricter policies (e.g. `anthropic_only`) override.
**Weight provenance rule:** US-origin weights only — exclude non-domestic-origin models even when hosted on US infrastructure.

> **Two orthogonal gates apply:**
> 1. **Policy gate** — does your contract or engagement permit US-origin OSS inference?
> 2. **Capability gate** — does the task class tolerate lower model reliability?
>
> Both gates must clear. A task class listed as "eligible" below still requires the policy gate.

---

## Task-Class Eligibility

| Task Class | Eligible for OSS? | Rationale |
|---|---|---|
| **Document extraction** | ✅ Strong fit | Stateless transform with well-defined correct output. OSS models deliver equivalent accuracy on structured extraction. Low failure cost — output is reviewed before use. |
| **Structured data transform** | ✅ Strong fit | Schema-in → schema-out. Deterministic mapping; model intelligence gap is irrelevant when the transform is unambiguous. |
| **Single-shot generation** | ✅ Strong fit | One-turn drafts (email subject, short description, tag list). No tool-calling or multi-hop required. Output reviewed by human. |
| **Classification** | ✅ Strong fit | Binary / multi-class over well-labelled categories. High throughput, low cost. |
| **Summarisation** | ✅ Strong fit | Internal docs, meeting notes, code diffs. No PII, no client code. OSS 65–70% benchmark sufficient for summarisation quality. |
| **Bulk extraction / grep / lint** | ✅ Strong fit | Format-check, lint, count, grep tasks. Speed and cost dominate. |
| **DOM selection / browser automation** | ✅ Strong fit | Browser-based multi-action flows; model interprets DOM labels. OSS ~1000 tok/s on optimized hardware — 5–10× faster than baseline for latency-critical paths. |
| **Code boilerplate / scaffolding** | ✅ Acceptable | Low-stakes generation: TypeScript interfaces, SQL stubs, test fixtures. Not for security-sensitive or client-facing code paths. |
| **Multi-step agentic loops** | ⚠️ Weak fit — stay first-party | Tool-calling reliability drops significantly outside first-party models. In a 10-step loop with 10 tool calls, 85% per-call success = ~20% loop completion rate vs ~95% on Anthropic/OpenAI. Stateful loops do not tolerate per-step failures gracefully. |
| **Security-sensitive code** | ❌ Keep first-party | Vulnerability assessment, auth logic, cryptography. Model capability gap means material risk of missing subtle security issues. |
| **Long-chain reasoning** | ❌ Keep first-party | Architecture decisions, trade-off analysis, design reviews. Quality degrades non-linearly with chain length outside first-party models. |
| **Client-facing output** | ❌ Keep first-party | Anything the client reads directly. Tone, accuracy, and hallucination risk matter. |
| **Standing-order Red-tier dispatch** | ❌ Keep first-party | Red actions (send email, merge contact, bulk delete) require human-level reasoning about intent and consequences. Never delegate to OSS. |
| **Email enrichment / recommendation** | ❌ Keep first-party | Standing orders evaluate nuanced context. Mis-classification has real-world impact. |
| **Cross-repo security review** | ❌ Keep first-party | Needs full first-party capability. Gap too large for security review work. |

---

## Enforcement

- **Pre-commit validation:** Model routing linter checks agents with `data_sensitivity: restricted_us_oss_ok` against permitted model origins.
- **Runtime validation:** Routing gates respect `client_ai_policy` if your project implements per-engagement policy enforcement.
- **Source of truth:** `.claude/model-routing.json` — `data_sensitivity_max` field on each model entry.

## Decision Flowchart (if OSS routing is enabled)

```
Does your policy permit OSS models?
  NO  → Use first-party models only. Stop.
  YES → Is the task class in the "Keep first-party" list above?
          YES → Use first-party (Anthropic/OpenAI). Stop.
          NO  → Is the model US-origin?
                  YES → Permitted. Route to OSS model.
                  NO  → Use first-party. Stop.
```

## Notes

- This framework is **your team's to customize**. Adjust task-class eligibility, add your own categories, and document your org's risk tolerance.
- The framework assumes you have telemetry or evals to validate the cost-vs-quality trade-off. If you don't yet, start with the "strong fit" tasks (extraction, classification, summarisation) and measure quality + cost before expanding.
- **Weight origin is orthogonal to data residency.** A model with non-domestic weights hosted on US infrastructure still requires policy clearance if your contract excludes non-domestic weights.
