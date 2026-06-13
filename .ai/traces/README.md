# Execution Traces

This directory stores execution traces from CI failures, deployment errors, and diagnostic sessions.

## Format

Trace files are named: `YYYY-MM-DD-{source}.log`

Sources:
- `ci` — GitHub Actions failures
- `deploy` — Deployment errors
- `inngest` — Background job failures
- `manual` — Saved via `/diagnose --save`

## Usage

The `/diagnose` command searches this directory for patterns matching an error description.

## Retention

Traces older than 90 days can be archived or deleted. They are git-tracked for team visibility.
