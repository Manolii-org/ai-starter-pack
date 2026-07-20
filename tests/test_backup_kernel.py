"""Backup kernel scaffold guards (WS-2 / PR-H6).

The kernel is a verbatim copy of master's backup scripts plus a manifest
contract; these tests keep the provenance record honest and the validator
fail-closed on the two failure modes that matter most (inlined secrets,
malformed cadence).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL = ROOT / "kernel" / "backup"
VALIDATOR = KERNEL / "bin" / "validate-backup-manifest.py"
EXAMPLE = KERNEL / "manifest" / "examples" / "manolii.yaml"


def _run_validator(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *map(str, paths)],
        capture_output=True,
        text=True,
    )


def test_provenance_hashes_match_shipped_files():
    rows = re.findall(
        r"^\| (scripts/\S+) \| \S+ \| ([0-9a-f]{64}) \|",
        (KERNEL / "PROVENANCE.md").read_text(encoding="utf-8"),
        flags=re.M,
    )
    assert rows, "PROVENANCE.md must list kernel files"
    for rel, expected in rows:
        actual = hashlib.sha256((KERNEL / rel).read_bytes()).hexdigest()
        assert actual == expected, f"{rel} drifted from PROVENANCE.md — re-sync from master and update the table"


def test_example_manifest_validates():
    result = _run_validator(EXAMPLE)
    assert result.returncode == 0, result.stderr


def test_validator_rejects_secret_shaped_values(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "custody_note: \"pending D5\"",
            "custody_note: \"postgresql://user:hunter2@db.example.com/x\"",
        ),
        encoding="utf-8",
    )
    result = _run_validator(bad)
    assert result.returncode == 1
    assert "names only" in result.stderr


def test_validator_rejects_bad_cron(tmp_path):
    bad = tmp_path / "badcron.yaml"
    bad.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace('cadence: "0 3 1 * *"', 'cadence: "monthly"'),
        encoding="utf-8",
    )
    result = _run_validator(bad)
    assert result.returncode == 1


def test_validator_reports_structural_garbage_without_crashing(tmp_path):
    bad = tmp_path / "garbage.yaml"
    bad.write_text("version: 0\nsystems: 'not-a-list'\ndrill: 'not-a-dict'\n", encoding="utf-8")
    result = _run_validator(bad)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_kernel_scripts_parse():
    for script in [KERNEL / "scripts" / "lib" / "backup-db-lib.sh", *sorted((KERNEL / "scripts").glob("*.sh"))]:
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"
