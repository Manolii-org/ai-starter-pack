#!/usr/bin/env python3
"""SAST completion evidence for the existing trusted assessment jobs.

Hashes bind bytes, not authority. Hosted authority comes from restored/pinned
runtime, runner event controls, producer job outputs and run-scoped transfer.
No standalone invocation, candidate field or model output establishes that trust.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

LIMIT = 32 * 1024 * 1024
CONFIG = {
    "semgrep": ["semgrep", "scan", "--config", "auto", "--json", "--disable-nosem"],
    "bandit": ["bandit", "-r", ".", "-f", "json", "--ignore-nosec", "--exclude", "./.venv,./node_modules"],
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True) + "\n").encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def decode(raw: bytes):
    if not raw or len(raw) > LIMIT:
        raise ValueError("missing or oversized SAST JSON")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def regular(path: Path) -> bytes:
    # Refuse redirected parents as well as redirected leaf files.
    if any(p.is_symlink() for p in (path, *path.parents)) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("SAST input must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("oversized SAST input")
    return raw


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=root,
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=30).stdout


def identity(root: Path, env: dict) -> dict:
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ValueError("SAST requires trusted pull-request launcher context")
    event = decode(regular(Path(env["GITHUB_EVENT_PATH"])))
    pr = event["pull_request"]
    repository = env["GITHUB_REPOSITORY"]
    if event["repository"]["full_name"] != repository or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("SAST repository mismatch")
    result = {"repository": repository, "head": pr["head"]["sha"], "base": pr["base"]["sha"],
              "run": env["GITHUB_RUN_ID"], "attempt": env["GITHUB_RUN_ATTEMPT"],
              "workflow_ref": env["GITHUB_WORKFLOW_REF"], "workflow_sha": env["GITHUB_WORKFLOW_SHA"]}
    if (any(not re.fullmatch(r"[0-9a-f]{40}", result[k]) for k in ("head", "base", "workflow_sha"))
            or any(not re.fullmatch(r"[1-9][0-9]*", result[k]) for k in ("run", "attempt"))):
        raise ValueError("invalid SAST launcher identity")
    for key in ("head", "base"):
        if git(root, "rev-parse", "--verify", result[key] + "^{commit}").decode().strip() != result[key]:
            raise ValueError("SAST source object unavailable")
    result["tree"] = git(root, "rev-parse", result["head"] + "^{tree}").decode().strip()
    return result


def source_files(root: Path, source: dict) -> list[str]:
    """Require actual scanner bytes to match every immutable tracked blob."""
    if git(root, "rev-parse", "HEAD").decode().strip() != source["head"]:
        raise ValueError("scanner checkout is not exact PR head")
    paths = []
    for row in git(root, "ls-tree", "-rz", source["head"]).split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, kind, blob = metadata.split()
        name = name.decode("utf-8")
        path = PurePosixPath(name)
        if mode not in (b"100644", b"100755") or kind != b"blob" or path.is_absolute() or ".." in path.parts:
            raise ValueError("unsupported immutable scanner source")
        raw = regular(root / name)
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
        if actual != blob.decode():
            raise ValueError("scanner source differs from immutable blob")
        paths.append(name)
    return paths


def normalize(name: str, raw: bytes, exit_code: int | None, version: str | None) -> tuple[dict, list]:
    """Findings exits are successful execution only with a complete valid report."""
    record = {"scanner": name, "configuration": CONFIG[name], "version": version,
              "exit_code": exit_code, "report_sha256": digest(raw) if raw else None,
              "status": "incomplete", "reason": "report_unavailable", "count": 0,
              "resolved_rules_identity": None, "reported_findings": None}
    findings = []
    try:
        report = decode(raw)
        rows, errors = report["results"], report.get("errors")
        if not isinstance(rows, list):
            raise ValueError("invalid scanner report")
        record["reported_findings"] = len(rows)
        malformed = False
        for row in rows:
            try:
                if name == "semgrep":
                    finding = dict(finding_id=f"semgrep-{row['check_id']}-{row['path']}", rule=row["check_id"],
                                   file=row["path"], line=row["start"]["line"], message=row["extra"]["message"],
                                   severity=row["extra"]["severity"].upper())
                else:
                    finding = dict(finding_id=f"bandit-{row['test_id']}-{row['filename']}", rule=row["test_id"],
                                   file=row["filename"], line=row["line_number"], message=row["issue_text"],
                                   severity=row["issue_severity"].upper())
                if (any(not isinstance(finding[k], str) or not finding[k].strip()
                        for k in ("finding_id", "rule", "file", "message", "severity"))
                        or type(finding["line"]) is not int or finding["line"] < 1):
                    raise ValueError("malformed scanner finding")
                findings.append(finding)
            except (ValueError, KeyError, TypeError, AttributeError):
                malformed = True
        if not isinstance(errors, list):
            raise ValueError("missing scanner completion fields")
        if name == "semgrep":
            scanned = report["paths"]["scanned"]
            if report.get("skipped_rules"):
                raise ValueError("incomplete scanner rules")
        else:
            metrics = report["metrics"]
            if not isinstance(metrics, dict) or "_totals" not in metrics:
                raise ValueError("missing scanner scope")
            if any(not isinstance(item, dict) or type(item.get("loc")) is not int or item["loc"] < 0
                   for item in metrics.values()):
                raise ValueError("incomplete scanner metrics")
            if metrics["_totals"]["loc"] != sum(item["loc"] for key, item in metrics.items() if key != "_totals"):
                raise ValueError("partial scanner metrics")
            scanned = [key for key in metrics if key != "_totals"]
        if not isinstance(scanned, list) or not all(isinstance(p, str) and p for p in scanned):
            raise ValueError("missing scanner scope")
        record["scanned_paths"] = scanned
        record["reported_errors"] = len(errors)
        record["scope"] = "configured scanner targets; not whole data-flow coverage"
        if malformed:
            record["reason"] = "malformed_report"
        elif errors:
            record["reason"] = "scanner_reported_errors"
        elif not scanned:
            record["reason"] = "no_observed_targets"
        # Semgrep scan does not use --error: its successful findings exit is 0.
        # Only Bandit's configured invocation documents a findings-only exit 1.
        elif exit_code != 0 and not (name == "bandit" and exit_code == 1 and rows):
            record["reason"] = "execution_failed"
        elif not version:
            record["reason"] = "scanner_identity_unavailable"
        else:
            record.update(status="complete", reason=None)
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        record["reason"] = "malformed_report" if raw else "report_unavailable"
    record["count"] = len(findings)
    if exit_code is None:
        record.update(status="incomplete", reason="outcome_unknown")
    elif exit_code < 0 or exit_code in (124, 130, 137, 143):
        record.update(status="incomplete", reason="process_terminated")
    elif exit_code not in (0, 1):
        record.update(status="incomplete", reason="execution_failed")
    return record, findings


def _source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or "\\" in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError("invalid scanner source path")
    return str(path)


def _configuration_inputs(root: Path, source: dict) -> list[dict]:
    """Candidate configuration cannot select its own successful scan policy."""
    def relevant(name):
        path = PurePosixPath(name)
        return path.name in (".bandit", ".semgrepignore", ".gitignore") or ".semgrep" in path.parts

    versions = []
    for ref in (source["base"], source["head"]):
        entries = {}
        for row in git(root, "ls-tree", "-rz", ref).split(b"\0"):
            if row:
                metadata, raw_name = row.split(b"\t", 1)
                name = raw_name.decode("utf-8")
                if relevant(name):
                    entries[name] = metadata.split()
        versions.append(entries)
    if versions[0] != versions[1]:
        raise ValueError("scanner configuration differs from trusted base")
    index = {}
    for row in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if row:
            metadata, raw_name = row.split(b"\t", 1)
            name = raw_name.decode("utf-8")
            if relevant(name):
                mode, blob, stage = metadata.split()
                if stage != b"0" or name in index:
                    raise ValueError("ambiguous scanner configuration index")
                index[name] = [mode, b"blob", blob]
    if index != versions[1]:
        raise ValueError("scanner configuration index differs from immutable source")
    if any(relevant(name.decode("utf-8"))
           for name in git(root, "ls-files", "--others", "-z").split(b"\0") if name):
        raise ValueError("untracked scanner configuration")
    result = []
    for name, (mode, kind, blob) in versions[1].items():
        if mode not in (b"100644", b"100755") or kind != b"blob" or _source_path(name) != name:
            raise ValueError("unsupported scanner configuration")
        raw = regular(root / name)
        observed = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
        if observed != blob.decode():
            raise ValueError("scanner configuration differs from immutable source")
        result.append({"path": name, "sha256": digest(raw)})
    return result


def produce(root: Path, output: Path, env: dict, run=subprocess.run) -> dict:
    source = identity(root, env)
    paths = source_files(root, source)
    tracked = frozenset(paths)
    configuration_inputs = _configuration_inputs(root, source)
    if output.exists() or any(p.is_symlink() for p in output.parents):
        raise ValueError("SAST output must be a fresh nonredirected directory")
    output.mkdir(parents=True)
    scanners, findings = [], []
    for name in CONFIG:
        if name == "bandit" and not any(p.endswith(".py") for p in paths):
            scanners.append(dict(scanner=name, configuration=CONFIG[name], version=None, exit_code=None,
                                 report_sha256=None, status="skipped", reason="no_python_sources", count=0))
            continue
        report_path = output / (name + ".json")
        version = None
        exit_code = None
        try:
            observed = run([name, "--version"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=30, check=False)
            if observed.returncode == 0:
                text = observed.stdout.decode("utf-8").strip()
                # Only the bounded scanner version, never arbitrary process text.
                matched = re.search(r"(?:^|\s)([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[a-zA-Z0-9.+-]*))(?=\s|$)", text)
                version = matched.group(1) if matched else None
            command = CONFIG[name] + (["--output", str(report_path), "."] if name == "semgrep" else ["-o", str(report_path)])
            observed = run(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=600, check=False)
            exit_code = observed.returncode
        except OSError:
            launch_failed = True
        except subprocess.TimeoutExpired:
            launch_failed = False  # Never claim all child work was cancelled.
        else:
            launch_failed = False
        try:
            raw = regular(report_path)
        except (OSError, ValueError):
            raw = b""
        record, rows = normalize(name, raw, exit_code, version)
        if launch_failed:
            record.update(status="incomplete", reason="scanner_unavailable")
        try:
            observed_paths = [_source_path(p) for p in record.get("scanned_paths", [])]
            if any(p not in tracked for p in observed_paths):
                raise ValueError("scanner scope outside immutable source")
            for row in rows:
                path = _source_path(row["file"])
                if path not in tracked or row["line"] > len(regular(root / path).splitlines()):
                    raise ValueError("finding outside immutable source")
        except (ValueError, OSError):
            record.update(status="incomplete", reason="source_scope_mismatch")
        scanners.append(record)
        findings.extend(rows)
        # Raw code excerpts stay runner-local; the existing normalized fields
        # and complete report digest travel, without logging raw provider text.
        if report_path.exists() and not report_path.is_symlink():
            report_path.unlink()
    source_files(root, source)  # scanner-side source mutation invalidates completion
    if _configuration_inputs(root, source) != configuration_inputs:
        raise ValueError("scanner configuration changed during execution")
    raw = encoded(findings)
    evidence = {"schema_version": 1, "identity": source, "runtime_sha256": digest(Path(__file__).read_bytes()),
                "configuration_inputs": configuration_inputs,
                "scanners": scanners, "findings_sha256": digest(raw),
                "entries": [{"position": n, "sha256": digest(encoded(row))} for n, row in enumerate(findings)],
                "complete": all(s["status"] == "complete" or s.get("reason") == "no_python_sources" for s in scanners)}
    if len(raw) > LIMIT or len(encoded(evidence)) > LIMIT:
        raise ValueError("SAST artifact exceeds input bound")
    (output / "sast-findings.json").write_bytes(raw)
    (output / "sast-evidence.json").write_bytes(encoded(evidence))
    return evidence


def prepare(root: Path, directory: Path) -> None:
    if directory != root / ".ai" / "sast":
        raise ValueError("invalid SAST consumer directory")
    legacy = root / ".ai/sast-findings.json"
    for path in (root, root / ".ai", directory):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("redirected SAST consumer directory")
    if legacy.is_symlink() or (legacy.exists() and not legacy.is_file()):
        raise ValueError("redirected legacy SAST input")
    legacy.unlink(missing_ok=True)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)


def verify(root: Path, directory: Path, env: dict, evidence_hash: str, findings_hash: str,
           sast_result: str, *, publish: bool = False) -> dict:
    source = identity(root, env)
    if sast_result == "skipped":
        if git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", source["base"] + "..." + source["head"]).strip():
            raise ValueError("required SAST was skipped")
        return {"status": "skipped", "reason": "empty_diff", "identity": source}
    if sast_result != "success" or not all(re.fullmatch(r"[0-9a-f]{64}", s) for s in (evidence_hash, findings_hash)):
        raise ValueError("required SAST producer unavailable")
    raw, findings = regular(directory / "sast-evidence.json"), regular(directory / "sast-findings.json")
    evidence, rows = decode(raw), decode(findings)
    if evidence.get("configuration_inputs") != _configuration_inputs(root, source):
        raise ValueError("incomplete or mismatched scanner configuration")
    if (digest(raw) != evidence_hash or digest(findings) != findings_hash
            or evidence.get("schema_version") != 1 or evidence.get("identity") != source
            or evidence.get("runtime_sha256") != digest(Path(__file__).read_bytes())
            or evidence.get("complete") is not True or evidence.get("findings_sha256") != findings_hash
            or not isinstance(rows, list)
            or evidence.get("entries") != [{"position": n, "sha256": digest(encoded(row))} for n, row in enumerate(rows)]):
        raise ValueError("incomplete or mismatched SAST artifact")
    scanners = evidence.get("scanners")
    if (not isinstance(scanners, list) or [s.get("scanner") for s in scanners] != list(CONFIG)
            or sum(s.get("count", -1) for s in scanners) != len(rows)
            or any(s.get("configuration") != CONFIG[s["scanner"]] or
                   not (s.get("status") == "complete" or
                        (s["scanner"] == "bandit" and s.get("status") == "skipped" and s.get("reason") == "no_python_sources"))
                   for s in scanners)):
        raise ValueError("incomplete SAST scanners")
    if publish:
        legacy = root / ".ai/sast-findings.json"
        if legacy.is_symlink() or any(p.is_symlink() for p in legacy.parents) or legacy.exists():
            raise ValueError("legacy SAST input was not cleared")
        # The exact verified bytes, never a stale candidate-committed file.
        with legacy.open("xb") as stream:
            stream.write(findings)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("produce", "prepare", "verify"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--evidence-sha256", default="")
    parser.add_argument("--findings-sha256", default="")
    parser.add_argument("--sast-result", default="")
    args = parser.parse_args()
    root = args.root.absolute()
    directory = args.directory.absolute()
    try:
        if args.mode == "prepare":
            prepare(root, directory)
        elif args.mode == "produce":
            evidence = produce(root, directory, dict(os.environ))
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                out.write(f"evidence_sha256={digest(regular(directory / 'sast-evidence.json'))}\n")
                out.write(f"findings_sha256={evidence['findings_sha256']}\n")
            return 0 if evidence["complete"] else 1
        else:
            verify(root, directory, dict(os.environ), args.evidence_sha256, args.findings_sha256,
                   args.sast_result, publish=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, RecursionError):
        print("SAST evidence unavailable or incomplete", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
