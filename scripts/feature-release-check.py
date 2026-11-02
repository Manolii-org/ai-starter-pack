#!/usr/bin/env python3
"""Validate source policy or qualify a release using an owning live probe."""
import argparse
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK / "packages/feature-controls/python"))
from feature_controls import assess_release, validate_release_manifest  # noqa: E402 -- use this checkout's SDK


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("required repository file is missing or outside the repository")
    return path


def safe_text(value):
    text = str(value)
    text = re.sub(r"\b(?:dp\.[a-z]{2,4}\.|sk-|gh[pso]_|github_pat_|Bearer\s+)[A-Za-z0-9_.-]{12,}", "[redacted]", text, flags=re.I)
    return html.escape(text).replace("|", "&#124;").replace("\n", " ")


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--manifest", default=".feature-controls/releases.json")
    parser.add_argument("--mode", choices=("source", "runtime"), default="source")
    parser.add_argument("--probe-script")
    parser.add_argument("--operation", choices=("deploy", "rollback"), default="deploy")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--summary")
    args = parser.parse_args()
    root = Path(args.repo_root).resolve()
    manifest = json.loads(inside(root, args.manifest).read_text())
    validate_release_manifest(manifest)
    for definition in manifest["features"].values():
        for reference in definition["acceptance"]:
            inside(root, reference)
    report = {"status": "source_validated", "runtime_status": "not_requested", "features": len(manifest["features"]), "rows": []}
    if args.mode == "runtime" and not manifest["features"]:
        report.update(status="no_features", runtime_status="no_features")
    elif args.mode == "runtime":
        if not args.probe_script or re.fullmatch(r"[0-9a-f]{40}", args.expected_source_sha or "") is None:
            raise ValueError("runtime qualification requires an owning probe and exact source SHA")
        probe = inside(root, args.probe_script)
        interpreter = {".py": "python3", ".mjs": "node", ".js": "node", ".sh": "bash"}.get(probe.suffix)
        if not interpreter:
            raise ValueError("unsupported owning probe")
        result = subprocess.run([interpreter, str(probe), "--expected-source-sha", args.expected_source_sha],
                                cwd=root, text=True, capture_output=True, timeout=180)
        if result.returncode:
            # A cloud CLI error may include credentials; never echo probe streams.
            raise ValueError("owning runtime probe failed; inspect its private diagnostics")
        evidence = json.loads(result.stdout)
        if set(evidence) != {"schema_version", "source_sha", "scope", "controls", "snapshots"} or evidence["schema_version"] != 1 or evidence["source_sha"] != args.expected_source_sha:
            raise ValueError("owning probe source/evidence binding mismatch")
        checked = assess_release(manifest, evidence["scope"], evidence["controls"], evidence["snapshots"],
                                 int(time.time() * 1000), args.expected_source_sha, evidence["source_sha"], args.operation)
        report.update(status=checked["status"], runtime_status=checked["status"], rows=checked["rows"])
    if args.summary:
        summary = Path(args.summary)
        with summary.open("a") as handle:
            handle.write("\n### Feature release qualification\n\n")
            handle.write(f"Result: **{safe_text(report['status'])}**. Runtime: **{safe_text(report['runtime_status'])}**.\n\n")
            if report["rows"]:
                handle.write("| Feature | Application / surface | Expected | Actual | Status | Deliberate exception |\n|---|---|---|---|---|---|\n")
                for row in report["rows"]:
                    explanations = "; ".join(f"{key}: {entry['owner']} — {entry['reason']} (review at {entry['review_at']})"
                                             for key, entry in row["exceptions"].items())
                    values = [row["feature_key"], row["application_id"] + " / " + row["surface_id"], row["expected"], row["actual"], row["status"], explanations]
                    handle.write("| " + " | ".join(safe_text(value) for value in values) + " |\n")
    # No raw control state, provider payloads, context scopes or probe output.
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}))
    return 0 if report["status"] in {"source_validated", "passed", "no_features"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print("feature-release-check: " + type(error).__name__ + "; validation or owning probe failed", file=sys.stderr)
        raise SystemExit(1)
