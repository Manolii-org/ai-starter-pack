# Native Muse subscription execution

`scripts/muse-subscription.py` runs one coding task with the official Muse CLI
and a tenant-owned High/Everyday CLI onboarding credential. Install Python 3.12
and the official CLI (1.4.9 or newer) from `https://dev.meta.ai` first; inspect installation and
version before injecting a credential. The executor checks both the version
and the CLI help statement that the sandbox defaults on before credential injection. The durable v1 event contract was checked
with Muse 1.4.9. No SDK, shared runner, proxy, knowledge service or API-key fallback
is required.

Supply `MUSE_CODE_SUBSCRIPTION_KEY` from the owning tenant's approved secret store.
Never borrow another tenant's credential or substitute a dashboard PAYG key.
Ambient `META_API_KEY`, `MODEL_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY` and
`OPENAI_BASE_URL` are rejected. The runner uses the standard `muse-spark-1.3`
model with contributor mode explicitly off; contributor models and usage-priced
fallback are not supported.

```bash
python3 scripts/muse-subscription.py \
  --operation-id task-123-attempt-1 \
  --prompt-file /tmp/approved-task.md \
  --output-dir /tmp/muse-task-123-attempt-1 \
  --workdir "$PWD"
```

Use a unique operation/attempt directory outside the workspace. Add `--dry-run`
to verify local setup without model inference or artifact writes, including when
a completed result already exists (dry-run never substitutes historical reuse). Add `--read-only`
for a smoke prompt such as “Read AGENTS.md and report the repository name; modify
nothing.” That mode disables shell and write tools, so it tests provider execution
and final extraction, not project tests or PR delivery.

The executor strips delivery/database credentials and uses an isolated HOME.
Interactive input and delegated subagents are disabled; the OS sandbox remains on.
An exit-zero process without a completed provider terminal and nonempty final
answer fails. Partial deltas are retained for recovery and cannot substitute for
a missing terminal answer. Parsing streams the full trace with memory proportional
to a maximum 16 MiB JSON record. Larger records produce an explicit
`output_parse_error=event_record_exceeds_16_mib` failure, retaining the raw trace
for reconciliation; they are never silently skipped or retried as paid work. Timeouts and SIGINT/SIGTERM cancellation stop the process group
on POSIX hosts and retain partial output. The runner tracking ID is preserved so
GitHub Actions can clean up descendants if the parent is forcibly terminated. The output leaf directory and files use owner-only permissions; this is not a privilege boundary against
another process running as the same OS user. Keep secrets out of the checkout
and use a disposable runner/worktree. Saved-result reuse is a recovery aid, not
cryptographic attestation; the owning agent must verify source state and output.
Workflow integrations must upload only receipt.json and final.md; raw events and
stderr remain private diagnostics and must not be uploaded wholesale.

A matching completed operation can be read again without repeating paid inference;
the console receipt says `reused_completed_result`. Incomplete or mismatched
existing directories require reconciliation. This is historical result reuse,
not a fresh provider or source-state check.

The owning agent retains responsibility for task authority, path limits, project
validation, branch policy, independent review and PR creation using its existing
trusted tooling. Completion of the model turn does not attest those later steps.
This helper does not change product model routing, merge policy or production
deployment. Subscription mode is declared by the credential contract; account
plan, remaining quota and manual sessions still require provider-side evidence.
Receipts label billing evidence as a declared credential contract and explicitly
record that the account plan was not verified. Exit 2 requires reading the
structured status: blocked setup has no inference; failed, timed_out or interrupted
operations need reconciliation of their retained evidence before a new attempt.
