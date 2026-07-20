#!/usr/bin/env python3
"""Resolve a PostgreSQL connection URL from Doppler secret JSON (no values logged)."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import URLError


def _secret_value(secrets: dict, key: str) -> str | None:
    raw = secrets.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        for field in ("computed", "raw", "value"):
            val = raw.get(field)
            if val:
                return str(val)
        return None
    return str(raw) if raw else None


def _pick_url(secrets: dict) -> str | None:
    for key in (
        "SUPABASE_POSTGRESQL_URL",
        "SUPABASE_DB_URL",
        "DATABASE_URL",
        "NEON_DATABASE_URL",
    ):
        val = _secret_value(secrets, key)
        if val:
            return val
    return None


def _pick_password(secrets: dict) -> str | None:
    for key in (
        "SUPABASE_DB_PASSWORD",
        "POSTGRES_PASSWORD",
        "DATABASE_PASSWORD",
        "DB_PASSWORD",
        "NEON_PASSWORD",
        "PGPASSWORD",
    ):
        val = _secret_value(secrets, key)
        if val:
            return val
    return None


def _neon_api_connection_uri(secrets: dict) -> str | None:
    """Fetch connection URI from the Neon API when no URL secret exists."""
    api_key = _secret_value(secrets, "NEON_API_KEY")
    project_id = _secret_value(secrets, "NEON_PROJECT_ID")
    if not api_key or not project_id:
        return None
    db_name = _secret_value(secrets, "NEON_DATABASE_NAME") or "neondb"
    role_name = _secret_value(secrets, "NEON_ROLE_NAME") or "neondb_owner"
    try:
        neon_base = os.environ.get(
            "NEON_API_BASE_URL", "https://console.neon.tech/api/v2"
        ).rstrip("/")
        params = f"database_name={quote(db_name, safe='')}&role_name={quote(role_name, safe='')}"
        req = Request(
            f"{neon_base}/projects/{quote(project_id, safe='')}/connection_uri?{params}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=15) as resp:  # nosec B310 — URL from Doppler secrets + env config
            data = json.loads(resp.read())
        uri = data.get("uri")
        if uri:
            print(
                f"resolved database URL via Neon API (db={db_name}, role={role_name})",
                file=sys.stderr,
            )
            return uri
    except (URLError, json.JSONDecodeError, KeyError) as exc:
        print(f"Neon API fallback failed: {exc}", file=sys.stderr)
    return None


def _with_password(url: str, password: str) -> str:
    parsed = urlparse(url)
    if parsed.password:
        return url
    if not parsed.hostname:
        raise ValueError("database URL missing hostname")
    user = parsed.username or "postgres"
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunparse(
        (
            parsed.scheme or "postgresql",
            netloc,
            parsed.path or "",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def resolve(secrets: dict) -> str:
    url = _pick_url(secrets)
    if not url:
        url = _neon_api_connection_uri(secrets)
    if not url:
        raise ValueError(
            "no database URL secret found (checked SUPABASE_POSTGRESQL_URL, "
            "SUPABASE_DB_URL, DATABASE_URL, NEON_DATABASE_URL, and Neon API "
            "fallback via NEON_API_KEY/NEON_PROJECT_ID)"
        )
    parsed = urlparse(url)
    if parsed.password:
        return url
    password = _pick_password(secrets)
    if not password:
        raise ValueError(
            "database URL has no password and no SUPABASE_DB_PASSWORD/POSTGRES_PASSWORD in Doppler"
        )
    return _with_password(url, password)


def main() -> int:
    try:
        secrets = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid secrets json: {exc}", file=sys.stderr)
        return 1
    try:
        resolved = resolve(secrets)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
