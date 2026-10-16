"""Durable dispatch admission for trusted, single-host background producers.

The caller supplies a stable incident/episode key and an already filtered
actionable signal, never an LLM classification. Dispatch is at-most-once:
an ambiguous write stays reserved for reconciliation, not automatic retry.
Use a persistent local volume; caches and ephemeral Actions disks are unsafe.
"""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing


def admit(database, namespace, episode_key, actionable, payload, dispatch, *, dry_run=False):
    if type(actionable) is not bool:
        raise ValueError("actionable must be an explicit boolean")
    if any(not isinstance(key, str) or not key.strip() for key in (namespace, episode_key)):
        raise ValueError("namespace and episode key required")
    if not actionable:
        return {"decision": "noop"}
    database = os.fspath(database)
    if not database or database == ":memory:" or database.startswith("file:"):
        raise ValueError("a persistent filesystem database path is required")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if dry_run:
        return {"decision": "would_dispatch", "payload_sha256": digest}
    with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS admissions (namespace TEXT NOT NULL, episode TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, primary_ref TEXT, updated INTEGER NOT NULL, PRIMARY KEY(namespace, episode))")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT state, primary_ref FROM admissions WHERE namespace=? AND episode=?", (namespace, episode_key)).fetchone()
        if existing:
            return {"decision": "duplicate", "state": existing[0], "primary_ref": existing[1]}
        connection.execute("INSERT INTO admissions VALUES (?,?,?,?,?,?)", (namespace, episode_key, digest, "reserved", None, int(time.time())))
    # Commit BEFORE dispatch. A process crash or timeout never releases this key.
    primary_ref = dispatch(payload)
    if not isinstance(primary_ref, str) or not primary_ref:
        raise ValueError("dispatch must return its primary session/incident reference; reservation retained")
    with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
        connection.execute("UPDATE admissions SET state='dispatched', primary_ref=?, updated=? WHERE namespace=? AND episode=?", (primary_ref, int(time.time()), namespace, episode_key))
    return {"decision": "dispatched", "primary_ref": primary_ref}
