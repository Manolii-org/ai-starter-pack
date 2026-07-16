# Operational Memory evaluation pack

Reusable behaviour pins for the reviewed Operational Memory module (contracts in
`<your-om-contracts-repo>/contracts/operational-memory/`). Cases follow `.ai/evals/schema.md` v1
and run through the standard harness (`scripts/run-evals.py`).

**Synthetic by construction.** Subjects are fictional units (`EX-01`…`EX-05`), packet IDs are
`sp-synthetic-*`, and no case contains real guest/owner data, real property names, financial
values, credentials or access codes. The example overlay manifest is passed as an *input path*
only — swap the manifest argument to reuse the entire pack for any other deployment or
vertical; no case body is example-specific.

Expected behaviours covered: answer-with-citation, answer-with-qualification (staleness),
surface-conflict-and-escalate, refuse (access codes, pricing floors, payments/trust,
system-of-record writes), unknown-is-an-answer, propose-change-set-never-publish,
draft-only-never-send, prompt-injection-as-data.

Severity: all `hard` except `stale-source-qualified` (`soft` — qualifier wording varies).

All cases set `requires_mcp: true`: they exercise live KL retrieval, so the PR `eval-gate.yml`
skips them (it never carries an `MCP_API_KEY`); run them from a trusted session with
`EVAL_ALLOW_MCP=1 python3 scripts/run-evals.py om-staff-answer` (and `om-fact-capture`).
