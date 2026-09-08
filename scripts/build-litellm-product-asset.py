#!/usr/bin/env python3
"""Validate and deterministically package a vendored LiteLLM product release."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "litellm-product/releases"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_release(version: str) -> tuple[Path, dict[str, object]]:
    release_dir = RELEASES / version
    metadata = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("release metadata must be a JSON object")
    required = {
        "asset", "files", "manifest_sha256", "product_version", "schema_version",
        "source_repository", "source_revision",
    }
    if set(metadata) != required or metadata.get("schema_version") != 1:
        raise ValueError("release metadata has an unsupported shape or schema version")
    if metadata.get("product_version") != version:
        raise ValueError("release directory and product_version differ")
    if metadata.get("source_repository") != "Manolii-org/master":
        raise ValueError("unexpected canonical source repository")
    revision = metadata.get("source_revision")
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("source_revision must be a full lowercase Git commit SHA")
    return release_dir, metadata


def validate_release(release_dir: Path, metadata: dict[str, object]) -> None:
    files = metadata.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object")
    source_dir = release_dir / "source"
    expected_paths = set()
    for name, expected in files.items():
        path = PurePosixPath(str(name))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe release path: {name}")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"invalid SHA-256 for {name}")
        expected_paths.add(path.as_posix())
        actual = _sha256((source_dir / path).read_bytes())
        if actual != expected:
            raise ValueError(f"source digest mismatch: {name}")

    manifest_path = source_dir / "config/litellm-product-source-manifest.json"
    if _sha256(manifest_path.read_bytes()) != metadata.get("manifest_sha256"):
        raise ValueError("source manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("product_version") != metadata.get("product_version") or manifest.get("files") != files:
        raise ValueError("source manifest content differs from release metadata")
    actual_paths = {
        path.relative_to(source_dir).as_posix()
        for path in source_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_paths != expected_paths:
        raise ValueError("vendored source inventory differs from release metadata")


def build_asset(release_dir: Path, metadata: dict[str, object], output: Path) -> str:
    validate_release(release_dir, metadata)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(p for p in release_dir.rglob("*") if p.is_file()):
            relative = path.relative_to(release_dir)
            info = tarfile.TarInfo(f"litellm-product-{metadata['product_version']}/{relative.as_posix()}")
            content = path.read_bytes()
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(buffer.getvalue())
    digest = _sha256(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        release_dir, metadata = load_release(args.version)
        output = args.output or ROOT / "dist" / str(metadata["asset"])
        digest = build_asset(release_dir, metadata, output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{output}: sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
