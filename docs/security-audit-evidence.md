# Adopting security audit evidence

The opt-in `security-audit-evidence` skill adds a bounded audit method and an
offline evidence checker. It does not install a scanner, enable paid usage,
read credentials, change model routing or start an automated security review.
Each owner retains its finding, remediation and human-merge workflow.

Edit `.claude/skills/security-audit-evidence/` only. Generate the branded plugin
with `scripts/build-plugin.py` and the unbranded registry with
`scripts/build-registry.py`. Both include `contract.md` and
`scripts/audit_evidence.py` inside the skill directory; no separately installed
root-level helper is required. Run the helper relative to the installed skill,
using Python 3.10+ and its standard library. Timezone comparisons require the
host's IANA timezone database (or its standard `tzdata` support).

Claude Code consumers can use the existing framework plugin or registry
resolver. The resolver's general capability materialisation currently supports
Claude Code; its separate native-guidance path supports a limited set of policy
skills on other surfaces. Adding this skill does not extend that policy set or
prove Codex, Cursor or Devin loaded it. On those surfaces, use the owner's
existing skill installation or explicit instruction-loading mechanism and verify
the installed path and invocation before claiming adoption. Do not add a global
always-loaded instruction or copy another owner's namespace to force discovery.

Run one same-owner service or related application/API pilot first. Declare
committed source revisions, trust boundaries, invocation and spend ceilings,
coverage units, and an independent verifier. Retain the native private scanner
artifacts; map only the allowed metadata into the receipt contract. A joint
review follows relationships across the selected repositories; independent
batch scans do not prove those relationships were reviewed.

Validation is `python3 -m pytest -q tests/test_security_audit_evidence.py`.
Tests exercise incomplete data, duplicate and conflicting records, explicit
zero versus absent metrics, DST boundaries and isolation constraints. Generated
copies must remain identical and the installed helper must work independently
of this repository's root-level scripts.

No monetary, token or analyst-time savings are assumed. Keep denied settings,
absent history, workload changes and missing billing metrics explicit. A
before/after difference is an observation until a separate attribution analysis
controls for scope, workload and provider changes. Never infer billable runtime
from scan start/finish timestamps.

Rollback removes the opt-in skill through the owner's existing capability
process. Keep private historical receipts available under that owner's retention
policy; do not archive, rename or delete findings as part of installation.
