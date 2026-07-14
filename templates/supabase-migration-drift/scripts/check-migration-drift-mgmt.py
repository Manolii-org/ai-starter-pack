#!/usr/bin/env python3
"""Sandbox/CI-safe migration drift detector using Supabase Management API.

This script verifies that every migration in supabase/migrations/ carrying an
`@assert-applied:` predicate is actually applied on all target Supabase
projects. It works without direct psql connectivity (IPv6-only constraint) and
without Doppler CLI by using the Management API at:

    POST https://api.supabase.com/v1/projects/<ref>/database/query

Intended consumers: bcp-core, manolii-platform, Ensombl, and any project
needing guaranteed migration-drift detection (ADR-0029).

Auth: a Bearer PAT in SUPABASE_ACCESS_TOKEN (set via Doppler or GitHub Actions secret).

Project mapping: Supplied via CLI --projects or env var MIGRATION_DRIFT_PROJECTS
in the format: entity:ref,entity:ref,... (e.g., 'prod:abc123def456,staging:xyz789')

Example usage:
    SUPABASE_ACCESS_TOKEN=... \\
    python3 scripts/check-migration-drift-mgmt.py \\
        --projects prod:wccgdisnrbvstnnzppld,staging:xyz789 \\
        --migrations-dir supabase/migrations \\
        --json --out reports/drift-latest/

Exit codes:
  0 = no drift among annotated migrations
  1 = drift detected on at least one entity
  2 = operational error (auth / HTTP / predicate SQL failure)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ASSERT_RE = re.compile(r"^\s*--\s*@assert-applied:\s*(.+?)\s*$", re.IGNORECASE)
VERSION_RE = re.compile(r"^(\d{5})_")

MGMT_API_TIMEOUT_S = 30
MGMT_API_RETRIES = 2  # + initial attempt = 3 total on transient network errors


@dataclass
class MigrationCheck:
    version: str
    name: str
    path: Path
    asserts: list[str] = field(default_factory=list)

    @property
    def annotated(self) -> bool:
        return bool(self.asserts)


def parse_projects_config(config_str: str) -> dict[str, str]:
    """Parse entity:ref,entity:ref,... into {entity -> ref} dict.

    Raises ValueError if format is invalid or empty.
    """
    if not config_str or not config_str.strip():
        raise ValueError("projects config cannot be empty")

    projects: dict[str, str] = {}
    for item in config_str.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid project spec '{item}' — expected entity:ref")
        # Defensively strip whitespace around both sides — users occasionally
        # write `entity : ref` in a config file and the leading/trailing spaces
        # would otherwise silently corrupt the ref (Gemini medium on PR #18).
        entity, ref = parts[0].strip(), parts[1].strip()
        if not entity or not ref:
            raise ValueError(f"invalid project spec '{item}' — entity and ref must not be empty")
        projects[entity] = ref

    if not projects:
        raise ValueError("no valid projects found in config")
    return projects


def list_forward_migrations(migrations_dir: Path) -> list[Path]:
    """All forward .sql files (rollback/down excluded), numeric order.

    Duplicate version prefixes (collision cases like 00072_*) are ALL
    returned — the drift check is per-file, not per-version.
    """
    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations directory does not exist: {migrations_dir}")

    out: list[Path] = []
    for p in sorted(migrations_dir.iterdir()):
        if not p.is_file() or not p.name.endswith(".sql"):
            continue
        if p.name.endswith(".rollback.sql") or p.name.endswith(".down.sql"):
            continue
        out.append(p)
    return out


def parse_migration(path: Path) -> MigrationCheck:
    m = VERSION_RE.match(path.name)
    version = m.group(1) if m else path.stem
    asserts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        hit = ASSERT_RE.match(line)
        if hit:
            asserts.append(hit.group(1))
    return MigrationCheck(version=version, name=path.name, path=path, asserts=asserts)


def load_checks(migrations_dir: Path) -> list[MigrationCheck]:
    return [parse_migration(p) for p in list_forward_migrations(migrations_dir)]


def mgmt_api_query(ref: str, token: str, sql: str) -> list[dict]:
    """POST a single SQL to the Supabase Management API and return decoded rows.

    Raises RuntimeError with a compact message on any non-2xx or transport error.
    Retries transient network / 5xx failures up to MGMT_API_RETRIES times with
    exponential backoff.
    """
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    # Defence-in-depth: even though repo-supplied assertion SQL is expected to
    # be `SELECT ...`, the Management API's write-capable endpoint would happily
    # run a writable CTE / function if someone slipped one in. `read_only: true`
    # asks the API to reject anything that would mutate. If a consumer's
    # Supabase project pre-dates the flag, the API still runs the query — no
    # regression, just a stronger guarantee where available. (Codex P1 on PR #18)
    payload = json.dumps({"query": sql, "read_only": True}).encode("utf-8")
    last_err: str | None = None
    for attempt in range(MGMT_API_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # Supabase's edge is Cloudflare-fronted; the default Python-urllib
                # user-agent lands on their bot-protection blocklist (error 1010).
                "User-Agent": "supabase-migration-drift-check/1.0 (+manolii-org/ai-starter-pack)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=MGMT_API_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8")
                try:
                    return json.loads(body) if body else []
                except json.JSONDecodeError as e:
                    # A non-JSON body is not a normal drift verdict — surface as
                    # a RuntimeError so the caller categorises it as exit 2
                    # (op error) rather than a JSONDecodeError → exit 1 (drift). (Gemini medium)
                    raise RuntimeError(f"non-JSON response from Management API: {e}") from e
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            # 429 (rate limited) is transient — a mature repo with many annotated
            # migrations can hit the Supabase Management API quota on a single
            # scheduled run (one HTTP request per predicate). Treating 429 as
            # definitive would turn the drift guard into a persistent op-error
            # until the window clears. Honour Retry-After if present, otherwise
            # fall through to the same exponential backoff as 5xx. (Codex P2 on PR #18)
            if e.code == 429:
                retry_after = e.headers.get("Retry-After", "") if e.headers else ""
                # Retry-After is either seconds (integer string) or an HTTP date;
                # we only handle the seconds form defensively — the exp-backoff
                # below is the safe fallback if parsing fails or a date is sent.
                try:
                    delay = max(0, int(retry_after))
                    if delay and attempt < MGMT_API_RETRIES:
                        time.sleep(min(delay, 30))  # cap so we don't wait forever on a hostile hint
                except (TypeError, ValueError):
                    pass
                last_err = f"HTTP 429 (rate limited): {body}"
            elif 400 <= e.code < 500:
                # Every other 4xx is a definitive answer — bad token, bad ref,
                # bad SQL — do not retry, exit 2 immediately.
                raise RuntimeError(f"HTTP {e.code}: {body}") from e
            else:
                last_err = f"HTTP {e.code}: {body}"
        except urllib.error.URLError as e:
            last_err = f"URLError: {e.reason}"
        except (TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < MGMT_API_RETRIES:
            time.sleep(2 ** attempt)
    raise RuntimeError(last_err or "unknown mgmt-api failure")


def predicate_true(ref: str, token: str, predicate: str) -> bool:
    # Wrap the predicate on its own line so a trailing single-line comment
    # (`SELECT 1 FROM x -- foo`) does not comment out the closing `) AS ok;`.
    # Without newlines, `SELECT EXISTS (SELECT ... -- foo) AS ok;` became
    # `SELECT EXISTS (SELECT ... -- foo) AS ok;` on a single line — the `--`
    # swallows everything to end-of-line and the syntax breaks. (Codex P1 on PR #18)
    sql = f"SELECT EXISTS (\n{predicate.rstrip(';')}\n) AS ok;"
    rows = mgmt_api_query(ref, token, sql)
    # Defensive type checks — a malformed API response used to KeyError /
    # TypeError past this point and crash with exit 1 (misreporting drift).
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or "ok" not in rows[0]:
        raise RuntimeError(f"predicate returned unexpected format: {rows!r}")
    return bool(rows[0]["ok"])


@dataclass
class DriftHit:
    version: str
    file: str
    predicate: str


@dataclass
class EntityResult:
    entity: str
    ref: str
    drift: list[DriftHit] = field(default_factory=list)
    error: str | None = None


def check_entity(entity: str, ref: str, token: str, annotated: list[MigrationCheck]) -> EntityResult:
    result = EntityResult(entity=entity, ref=ref)
    for check in annotated:
        for predicate in check.asserts:
            try:
                applied = predicate_true(ref, token, predicate)
            except Exception as e:
                # Catch Exception (not just RuntimeError) so an unexpected
                # KeyError / TypeError from a malformed API response is
                # classified as op-error (exit 2), not drift (exit 1). (Gemini medium)
                result.error = f"{check.name}: {e}"
                return result
            if not applied:
                result.drift.append(
                    DriftHit(version=check.version, file=check.name, predicate=predicate)
                )
    return result


def write_report(out_dir: Path, results: list[EntityResult],
                 checks: list[MigrationCheck]) -> None:
    """Persist per-entity JSON + a human-readable DRIFT.md."""
    out_dir.mkdir(parents=True, exist_ok=True)
    annotated = [c for c in checks if c.annotated]
    unverified = [c for c in checks if not c.annotated]

    for r in results:
        (out_dir / f"drift-{r.entity}.json").write_text(
            json.dumps(
                {
                    "entity": r.entity,
                    "supabase_ref": r.ref,
                    "annotated_checked": len(annotated),
                    "drift": [d.__dict__ for d in r.drift],
                    "error": r.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # DRIFT.md summary
    lines = [
        "# Migration drift report",
        "",
        f"- Migrations total: **{len(checks)}**",
        f"- Annotated (checkable): **{len(annotated)}**",
        f"- Unverified (no `@assert-applied`): **{len(unverified)}**",
        "",
        "## Per-entity result",
        "",
        "| Entity | Ref | Drifted | Error |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r.entity}` | `{r.ref}` | {len(r.drift)} | "
            f"{'—' if not r.error else r.error[:80]} |"
        )
    lines.append("")

    for r in results:
        if not r.drift and not r.error:
            continue
        lines.append(f"### `{r.entity}` (`{r.ref}`)")
        lines.append("")
        if r.error:
            lines.append(f"- ERROR: `{r.error}`")
        for d in r.drift:
            lines.append(f"- **{d.version}** `{d.file}` — predicate failed: `{d.predicate}`")
        lines.append("")

    lines.extend([
        "## Unverified migrations (no `@assert-applied`)",
        "",
        "These migrations cannot be checked until an `@assert-applied` header is added. "
        "Highest-risk are number collisions (multiple files share a `NNNNN_` prefix) — "
        "each must be individually verifiable.",
        "",
    ])
    # Group unverified by version to surface collisions
    by_ver: dict[str, list[str]] = {}
    for c in unverified:
        by_ver.setdefault(c.version, []).append(c.name)
    for ver in sorted(by_ver):
        names = by_ver[ver]
        marker = " **← collision**" if len(names) > 1 else ""
        lines.append(f"- **{ver}**{marker}")
        for n in names:
            lines.append(f"  - `{n}`")
    lines.append("")

    (out_dir / "DRIFT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--projects",
        type=str,
        default=None,
        help="Project config in format entity:ref,entity:ref,... . "
             "Defaults to MIGRATION_DRIFT_PROJECTS env var if not set.",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path("supabase/migrations"),
        help="Directory containing migration .sql files (default: supabase/migrations)",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Filter to a single entity (by name). Requires --projects to be set.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable stdout.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory to persist per-entity JSON and DRIFT.md.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print annotation coverage only; do not touch any DB.",
    )
    args = parser.parse_args()

    # Resolve projects config
    projects_config = args.projects or os.environ.get("MIGRATION_DRIFT_PROJECTS", "").strip()
    if not projects_config:
        print("ERROR: --projects not provided and MIGRATION_DRIFT_PROJECTS not set",
              file=sys.stderr)
        return 2

    try:
        projects_map = parse_projects_config(projects_config)
    except ValueError as e:
        print(f"ERROR: Invalid projects config: {e}", file=sys.stderr)
        return 2

    # Filter to single project if --project specified
    if args.project:
        if args.project not in projects_map:
            print(f"ERROR: project '{args.project}' not found in config. "
                  f"Available: {', '.join(projects_map.keys())}", file=sys.stderr)
            return 2
        projects_map = {args.project: projects_map[args.project]}

    # Load migrations
    try:
        checks = load_checks(args.migrations_dir)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    annotated = [c for c in checks if c.annotated]
    unverified = [c for c in checks if not c.annotated]

    if args.list:
        print(f"migrations: {len(checks)} total, "
              f"{len(annotated)} annotated, {len(unverified)} unverified")
        if not args.json:
            print("annotated:   " + (", ".join(c.version for c in annotated) or "(none)"))
        return 0

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: SUPABASE_ACCESS_TOKEN not set. Fetch from Doppler or "
              "GitHub Actions secret.", file=sys.stderr)
        return 2

    if not annotated:
        print("no migrations carry an @assert-applied annotation — nothing to verify",
              file=sys.stderr)

    results = [check_entity(e, ref, token, annotated) for e, ref in projects_map.items()]

    has_drift = any(r.drift for r in results)
    has_error = any(r.error for r in results)

    if args.out:
        write_report(args.out, results, checks)

    if args.json:
        print(json.dumps({
            "checked_entities": list(projects_map.keys()),
            "annotated_count": len(annotated),
            "unverified_versions": [c.version for c in unverified],
            "results": [
                {
                    "entity": r.entity,
                    "ref": r.ref,
                    "error": r.error,
                    "drift": [d.__dict__ for d in r.drift],
                }
                for r in results
            ],
        }, indent=2))
    else:
        for r in results:
            if r.error:
                print(f"[{r.entity}] ERROR: {r.error}", file=sys.stderr)
            elif r.drift:
                print(f"[{r.entity}] DRIFT — {len(r.drift)} unapplied assertion(s):")
                for d in r.drift:
                    print(f"    {d.version} {d.file}: {d.predicate}")
            else:
                print(f"[{r.entity}] OK — all {len(annotated)} annotated migrations applied")
        if unverified:
            print(f"\n{len(unverified)} migration(s) unverified (no @assert-applied): "
                  + ", ".join(sorted({c.version for c in unverified})))

    if has_error:
        return 2
    return 1 if has_drift else 0


if __name__ == "__main__":
    # Wrap the whole run so an unhandled exception exits with 2 (op error), not
    # Python's default 1 (which the CI would misread as "drift detected"). (Gemini high)
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as _fatal:  # noqa: BLE001 — intentional catch-all
        import traceback
        print(f"FATAL: {type(_fatal).__name__}: {_fatal}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(2)
