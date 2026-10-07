#!/usr/bin/env python3
"""Prepare and verify feature-control artifacts; never tag, push or publish."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import tomllib

SOURCE_REPOSITORY = "Manolii-org/ai-starter-pack"
SOURCE_URL = f"https://github.com/{SOURCE_REPOSITORY}.git"
PYTHON_SUBDIRECTORY = "packages/feature-controls/python"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?")
ROOT = Path(__file__).resolve().parents[1]


def load_json(text: str) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def run(args: list[str], cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=300, check=False)
    if result.returncode:
        raise ValueError(f"{args[0]} command failed (exit {result.returncode}); no release produced")
    return result.stdout.decode("utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid relative path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("unsafe relative path")
    return path


def python_version(version: str) -> str:
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError("SDK version must be X.Y.Z or X.Y.Z-{alpha,beta,rc}.N")
    base = ".".join(match.groups()[:3])
    if match[4]:
        return base + {"alpha": "a", "beta": "b", "rc": "rc"}[match[4]] + match[5]
    return base


def schema_version(schema: dict) -> str:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    version = properties.get("schema_version", {}) if isinstance(properties, dict) else {}
    value = version.get("const") if isinstance(version, dict) else None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError("contract requires properties.schema_version.const")
    return str(value)


def repository_slug(remote: str) -> str:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?", remote)
    if not match:
        raise ValueError("origin must be an explicit GitHub HTTPS/SSH repository")
    return match[1]


def configured_origin(root: Path) -> str:
    # Git get-url expands host-specific insteadOf rules; inspect the literal origin.
    config_path = Path(run(["git", "rev-parse", "--git-path", "config"], root).strip())
    if not config_path.is_absolute():
        config_path = root / config_path
    config = configparser.RawConfigParser()
    config.read(config_path)
    if not config.has_option('remote "origin"', "url"):
        raise ValueError("explicit origin URL required")
    return config.get('remote "origin"', "url")


def source_identity(root: Path, sha: str, version: str, tag: str, allow_untagged: bool) -> dict:
    python_version(version)
    if not SHA.fullmatch(sha) or tag != f"feature-controls-v{version}":
        raise ValueError("full source SHA and matching component-prefixed tag required")
    if Path(run(["git", "rev-parse", "--show-toplevel"], root).strip()).resolve() != root.resolve():
        raise ValueError("repo-root must be the Git worktree root")
    if run(["git", "status", "--porcelain", "--untracked-files=all"], root).strip():
        raise ValueError("dirty source worktree")
    if run(["git", "rev-parse", "HEAD"], root).strip() != sha:
        raise ValueError("source SHA differs from HEAD")
    remote = configured_origin(root)
    if repository_slug(remote).lower() != SOURCE_REPOSITORY.lower():
        raise ValueError("wrong canonical source repository")
    exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
                            cwd=root, timeout=30, check=False).returncode == 0
    if not exists:
        if not allow_untagged:
            raise ValueError("release tag absent; --allow-untagged only prepares a Git bridge candidate")
        return {"status": "pending", "object": None}
    if run(["git", "cat-file", "-t", f"refs/tags/{tag}"], root).strip() != "tag":
        raise ValueError("release tag must be annotated")
    if run(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], root).strip() != sha:
        raise ValueError("release tag/source mismatch")
    return {"status": "annotated", "object": run(["git", "rev-parse", f"refs/tags/{tag}"], root).strip()}


def export_source(root: Path, sha: str, destination: Path) -> None:
    result = subprocess.run(["git", "archive", sha], cwd=root, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError("cannot export committed source")
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        members = []
        for member in archive.getmembers():
            safe_relative(member.name.rstrip("/"))
            if not member.isdir() and not member.isfile():
                raise ValueError("release source cannot contain links or special files")
            members.append(member)
        archive.extractall(destination, members=members, filter="data")


def package_metadata(source: Path, version: str) -> dict:
    npm = load_json((source / "package.json").read_text())
    project = tomllib.loads((source / PYTHON_SUBDIRECTORY / "pyproject.toml").read_text())["project"]
    return package_identity(npm, project, version)


def package_identity(npm: dict, project: dict, version: str) -> dict:
    if npm.get("version") != version or project.get("version") != python_version(version):
        raise ValueError("SDK/package/source version mismatch")
    names = (npm.get("name"), project.get("name"))
    if not all(isinstance(name, str) and re.fullmatch(r"[@a-zA-Z0-9_./-]+", name) for name in names):
        raise ValueError("invalid package name")
    return {"npm": {"name": names[0], "version": version},
            "python": {"name": names[1], "version": project["version"]}}


def build_artifacts(source: Path, output: Path) -> list[Path]:
    output.mkdir()
    home = output / "build-home"
    home.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), SOURCE_DATE_EPOCH="315532800", TZ="UTC", LC_ALL="C",
               PYTHONHASHSEED="0", PIP_DISABLE_PIP_VERSION_CHECK="1")
    npm = load_json((source / "package.json").read_text())
    if npm.get("dependencies") or npm.get("devDependencies"):
        if not (source / "package-lock.json").is_file():
            raise ValueError("npm builds require an exact package-lock.json")
        run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], source, env)
    scripts = npm.get("scripts", {})
    build = "build" if "build" in scripts else "prepare" if "prepare" in scripts else None
    if build:
        run(["npm", "run", build], source, env)
    packed = json.loads(run(["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(output)], source, env))
    if not isinstance(packed, list) or len(packed) != 1:
        raise ValueError("npm pack must produce one artifact")
    if len(safe_relative(packed[0]["filename"]).parts) != 1:
        raise ValueError("npm artifact filename must be a basename")
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(output),
         str(source / PYTHON_SUBDIRECTORY)], source, env)
    artifacts = [output / packed[0]["filename"], *sorted(output.glob("*.whl"))]
    if len(artifacts) != 2 or not all(path.is_file() for path in artifacts):
        raise ValueError("expected exactly one npm tarball and Python wheel")
    return artifacts


def artifact_identity(path: Path, kind: str) -> dict:
    if kind == "npm":
        with tarfile.open(path) as archive:
            seen = set()
            for member in archive.getmembers():
                relative = safe_relative(member.name.rstrip("/"))
                if relative.parts[0] != "package" or relative in seen:
                    raise ValueError("invalid/duplicate npm artifact member")
                seen.add(relative)
                if not member.isfile() and not member.isdir():
                    raise ValueError("unsafe npm artifact member")
            metadata = archive.extractfile("package/package.json")
            if metadata is None:
                raise ValueError("npm metadata missing")
            value = load_json(metadata.read().decode())
            return {"name": value["name"], "version": value["version"]}
    if kind != "python":
        raise ValueError("unknown artifact format")
    with zipfile.ZipFile(path) as archive:
        seen = set()
        for info in archive.infolist():
            relative = safe_relative(info.filename.rstrip("/"))
            if relative in seen:
                raise ValueError("duplicate wheel member")
            seen.add(relative)
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("unsafe wheel symlink")
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("wheel metadata missing/ambiguous")
        value = BytesParser().parsebytes(archive.read(names[0]))
        name = re.sub(r"[-_.]+", "-", value["Name"]).lower()
        return {"name": name, "version": value["Version"]}


def verify_artifacts(directory: Path) -> dict:
    if (directory / "release.json").is_symlink():
        raise ValueError("release manifest cannot be a symlink")
    manifest = load_json((directory / "release.json").read_text())
    expected = {"manifest_version", "sdk_version", "schema_version", "source_repository",
                "source_revision", "tag", "tag_identity", "packages", "contract", "artifacts"}
    if not isinstance(manifest, dict) or set(manifest) != expected or type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
        raise ValueError("unsupported release manifest")
    version = manifest["sdk_version"]
    py_version = python_version(version)
    if manifest["source_repository"] != SOURCE_REPOSITORY or not SHA.fullmatch(manifest["source_revision"]):
        raise ValueError("invalid source identity")
    if manifest["tag"] != f"feature-controls-v{version}":
        raise ValueError("wrong component release tag")
    identity = manifest["tag_identity"]
    if not isinstance(identity, dict) or set(identity) != {"status", "object"}:
        raise ValueError("invalid tag identity")
    if identity != {"status": "pending", "object": None} and (
        identity["status"] != "annotated" or not isinstance(identity["object"], str)
        or not SHA.fullmatch(identity["object"])
    ):
        raise ValueError("invalid tag object")
    packages = manifest["packages"]
    if not isinstance(packages, dict) or set(packages) != {"npm", "python"}:
        raise ValueError("invalid package metadata")
    if packages["npm"]["version"] != version or packages["python"]["version"] != py_version:
        raise ValueError("package versions differ from SDK version")
    records = [manifest["contract"], *manifest["artifacts"]]
    if len(records) != 3 or {record.get("format") for record in manifest["artifacts"]} != {"npm", "python"}:
        raise ValueError("invalid artifact inventory")
    seen = {"release.json"}
    for record in records:
        required = {"file", "sha256", "source_path"} if record is manifest["contract"] else {"file", "sha256", "format"}
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("invalid artifact/contract record")
        name = record["file"]
        relative = safe_relative(name)
        if len(relative.parts) != 1 or name in seen:
            raise ValueError("duplicate/nested artifact path")
        seen.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file() or not DIGEST.fullmatch(record["sha256"]) or digest(path) != record["sha256"]:
            raise ValueError(f"artifact digest mismatch: {name}")
        if record is manifest["contract"]:
            safe_relative(record["source_path"])
            if schema_version(load_json(path.read_text())) != manifest["schema_version"]:
                raise ValueError("contract/schema version mismatch")
        else:
            actual = artifact_identity(path, record["format"])
            wanted = packages[record["format"]].copy()
            if record["format"] == "python":
                wanted["name"] = re.sub(r"[-_.]+", "-", wanted["name"]).lower()
            if actual != wanted:
                raise ValueError("artifact/package identity mismatch")
    if {path.name for path in directory.iterdir()} != seen:
        raise ValueError("unmanifested release files")
    return manifest


def verify_source(root: Path, manifest: dict, allow_untagged: bool) -> None:
    root = root.resolve()
    identity = source_identity(root, manifest["source_revision"], manifest["sdk_version"],
                               manifest["tag"], allow_untagged)
    if identity != manifest["tag_identity"]:
        raise ValueError("tag object/source drift")
    sha = manifest["source_revision"]
    npm = load_json(run(["git", "show", f"{sha}:package.json"], root))
    project = tomllib.loads(run(["git", "show", f"{sha}:{PYTHON_SUBDIRECTORY}/pyproject.toml"], root))["project"]
    if package_identity(npm, project, manifest["sdk_version"]) != manifest["packages"]:
        raise ValueError("source/package metadata mismatch")
    path = safe_relative(manifest["contract"]["source_path"])
    contract = run(["git", "show", f"{sha}:{path}"], root)
    if hashlib.sha256(contract.encode()).hexdigest() != manifest["contract"]["sha256"]:
        raise ValueError("source/contract mismatch")


def prepare(root: Path, output: Path, sha: str, version: str, tag: str,
            schema: str, allow_untagged: bool = False) -> dict:
    root = root.resolve()
    output = output.absolute()
    if output.resolve().is_relative_to(root) or output.exists() or not output.parent.is_dir():
        raise ValueError("output must be a new directory outside the source worktree")
    identity = source_identity(root, sha, version, tag, allow_untagged)
    schema_path = safe_relative(schema)
    with tempfile.TemporaryDirectory(prefix="feature-controls-") as temp:
        staging = Path(temp)
        packages = None
        first = None
        for index in (1, 2):
            source = staging / f"source-{index}"
            source.mkdir()
            export_source(root, sha, source)
            packages = package_metadata(source, version)
            built = staging / f"build-{index}"
            artifacts = build_artifacts(source, built)
            current = {path.name: digest(path) for path in artifacts}
            if first is not None and current != first:
                raise ValueError("artifact build is not reproducible")
            first = current
        candidate = staging / "release"
        candidate.mkdir()
        contract = source / schema_path
        shutil.copyfile(contract, candidate / "contract.schema.json")
        records = []
        for artifact in artifacts:
            shutil.copyfile(artifact, candidate / artifact.name)
            records.append({"file": artifact.name, "format": "python" if artifact.suffix == ".whl" else "npm",
                            "sha256": digest(artifact)})
        manifest = {"manifest_version": 1, "sdk_version": version,
                    "schema_version": schema_version(load_json(contract.read_text())),
                    "source_repository": SOURCE_REPOSITORY, "source_revision": sha, "tag": tag,
                    "tag_identity": identity, "packages": packages,
                    "contract": {"file": "contract.schema.json", "source_path": schema,
                                 "sha256": digest(contract)},
                    "artifacts": records}
        (candidate / "release.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        verify_artifacts(candidate)
        verify_source(root, manifest, allow_untagged)
        output.mkdir()
        for path in candidate.iterdir():
            shutil.copyfile(path, output / path.name)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    build.add_argument("--repo-root", type=Path, default=ROOT)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--tag", required=True)
    build.add_argument("--schema", required=True, help="committed JSON schema with schema_version const")
    build.add_argument("--allow-untagged", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("--release", type=Path, required=True)
    check.add_argument("--repo-root", type=Path, default=ROOT)
    check.add_argument("--allow-untagged", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            manifest = prepare(args.repo_root, args.output, args.source_sha, args.version,
                               args.tag, args.schema, args.allow_untagged)
        else:
            manifest = verify_artifacts(args.release)
            verify_source(args.repo_root, manifest, args.allow_untagged)
        print(json.dumps(manifest, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, configparser.Error,
            subprocess.TimeoutExpired, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"feature-controls-release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
