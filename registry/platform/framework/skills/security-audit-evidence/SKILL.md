---
name: security-audit-evidence
description: Plan bounded security audits and validate coverage, independent verification and savings evidence before reporting results. Use with an existing scanner or coding agent; loading this skill does not start scans.
version: 1.0.0
scope: platform
license: Apache-2.0
---

# Evidence for security audits

Use the owning repository's existing security workflow. Start with one service
or one related application/API pair inside the same authorized owner boundary.
Record each checkout's exact revision and dirty state before review. A joint
source review can follow caller/authorization/storage relationships; a bulk
campaign of independent repository scans does not establish that coverage.

Before scanning, record the scope, threat actors, entry points, trust boundaries,
attack classes and an explicit invocation/token budget. Use already authorized
billing and models. A free software license does not establish free inference.
Unknown entitlement or exhausted quota stays blocked; no API-key fallback or
automatic enabling of paid scans. Do not load production config values.

Maintain a coverage ledger by repository, source revision, boundary and attack
class. Keep covered, needs_validation, deferred and out_of_scope distinct.
Revalidate changed source; a prior clean result cannot cover a changed boundary.
Hunt high-impact boundaries first, reserve budget for verification, and give a
candidate to a different verifier who tries to disprove it. Separate agents
using one model are not separate model families. Keep rejected and unresolved
leads private and visible; route only confirmed candidates to the owning
remediation workflow, which still independently validates them.

Use source-only review unless an OS-enforced, resource-limited, network-disabled
sandbox with an allowlisted environment and no production access has been
verified. Only that sandbox may execute target builds, exploit reproducers or
adversarial patch retests. Source-only output is never runtime reproduction.
Mantis is experimental; its default static-only mode does not run exploits and
its PR action must not become a production merge gate without qualification.
Keep Claude's proprietary plugin inside its licensed Anthropic runtime; do not
copy its prompts into other agents or this pack.

Persist a metadata-only receipt using the contract in
[the contract guide](contract.md). Run the bundled helper from this skill's
directory (resolve it relative to the installed SKILL.md, not the caller cwd):

```bash
python3 scripts/audit_evidence.py validate /path/to/private/receipt.json
python3 scripts/audit_evidence.py compare /path/to/private/scan-metadata.json \
  --baseline 2026-10-09 --comparison 2026-10-20 --days 11 --timezone Australia/Sydney
```

The helper is offline, uses the Python standard library, reads only the supplied
JSON, writes no files and prints counts/measurements rather than finding text.
Its validation checks the receipt contract, not vulnerability correctness or
scanner effectiveness. Never feed raw secrets, production config, source bodies,
prompts or full tool responses into it. A scanner's native artifacts are not this
receipt: map only the allowed metadata and retain the originals privately.

Report scope and unresolved prerequisites alongside findings. Require a real
before/after reproducer and legitimate-behavior checks for a patch where
execution is available; otherwise retain the validation limitation. Preserve
existing finding IDs, dispositions, dedupe, merge labels and release gates.
Do not merge ownership namespaces or send another owner's findings to a shared
consumer merely because this method is portable.

Compare equal local-calendar windows using unique scan IDs. Missing usage,
denied access, skipped execution, zero findings and zero free allowance are
distinct facts. Count reductions are workload observations; billable tokens,
invoice charges and analyst time need their own evidence. The helper reports
observed spend differences only when both windows are complete and every scan
has recorded cost; it never attributes those differences to an optimization.
Use matched scope/workload and a recorded intervention for a separate attribution
assessment. Do not convert elapsed job timestamps into billable runtime.

Method references: [Cloudflare's audit skill](https://github.com/cloudflare/security-audit-skill),
[fleet-harness limits](https://blog.cloudflare.com/build-your-own-vulnerability-harness/),
[Codex system review](https://github.com/openai/codex-security),
[Mantis limitations](https://github.com/google/mantis).
