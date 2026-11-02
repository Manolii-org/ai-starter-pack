# Private evidence contracts

`security-audit-evidence/v1` is a metadata wrapper, not a replacement for a
scanner's native report. Required fields:

- `schema`, `owner` (GitHub owner), `source_revisions`: nonempty list of
  `{repository: "Example-org/api", revision: "<40-hex commit>", dirty: false}`.
  All repositories must belong to that owner; dirty inputs need a separately
  attested snapshot and are refused by this committed-source contract.
- `budget`: `{max_invocations: 12, used_invocations: 6}`. Nonnegative integers;
  the maximum is positive and usage cannot exceed it. This records a ceiling,
  not approval to spend. Provider/OS limits remain separately enforced.
- `coverage`: nonempty list of `{id, repository, revision, boundary,
  attack_class, status, evidence}`. Status is covered, needs_validation,
  deferred or out_of_scope. `evidence` is a list of private references;
  covered units need at least one. Every revision must match the source list.
- `findings`: list of `{unit_id, verdict, evidence}`. Verdict is confirmed,
  needs_validation or rejected. A confirmed row additionally requires distinct
  `hunter_id`/`verifier_id` and `validation`: source_trace or sandbox_reproducer.
  All findings require nonempty evidence and must refer to a known coverage unit.
- `execution`: `{mode: "source_only", production_access: false,
  secret_environment: false}`. sandbox_reproducer requires mode isolated plus
  literal `isolation_verified: true` and `network_disabled: true`.

The summary's `coverage_complete` is true only when every declared unit is
covered and no finding still needs validation. It never proves the declared
scope is exhaustive or that findings are correct.

`security-scan-metadata/v1` contains `schema`, `as_of`,
`history_complete_after` (RFC3339 timestamps with timezone) and `scans`.
Every scan has `id`, `repository`, `started_at`, `status` and optionally
`success`, `finished_at`, `recorded_cost_usd`, `input_tokens`, `output_tokens`,
`analyst_minutes`. Repository names are owner/repo, metrics are finite and
nonnegative, token counts are integers. Explicit zero is measured; null or
omitted is unknown. `recorded_cost_usd` must come from a recorded charge/export,
not an estimate from elapsed time or a free-credit balance.

Only identical duplicate (repository, id) rows are collapsed. Conflicting
duplicates are refused. A window is complete only when the supplied coverage
start precedes its start and `as_of` reaches its end. Empty/incomplete windows
have unknown cost and time, even when their visible count is zero. Attribution
is always unmeasured by this helper. Record any differing scopes, workload,
interventions or provider-price changes separately before interpreting changes.

Keep receipts and reports in an owner-controlled ignored/private location.
Do not commit vulnerability details, source or account identifiers to this public
pack. The helper never reads credentials or fetches live settings. Missing live
settings remain unknown and must not be inferred from a workflow listing.
