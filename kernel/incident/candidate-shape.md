# Candidate shape

One JSON object per line. `additionalProperties` is false.

| Field | Value |
|---|---|
| `entity` | routes manifest `entity` |
| `repo` | owning repository (`owner/name`) from the route |
| `runtime_id` | route `runtime_id` |
| `provider` | `sentry` |
| `provider_account_id` | immutable Sentry organization id |
| `provider_project_id` | immutable Sentry project id |
| `delivery_id` | `sentry:issue:<group id>:<environment>:<UTC date>` |
| `release_sha` | 7–64 hex chars, or `null` |
| `fingerprint` | `sha256("sentry-group:<group id>")` (64 hex) |
| `title` | `Sentry <SHORT-ID>` or `Sentry <SHORT-ID>: <ExceptionType>` |
| `classification` | `code_defect` \| `migration` \| `configuration` \| `provider` \| `quota` \| `security` \| `unknown` |
| `severity` | `error` \| `fatal` |
| `environment` | `production` (the route owns its environment) |
| `telemetry_links` | `["https://<org slug>.sentry.io/issues/<group id>/"]` |
