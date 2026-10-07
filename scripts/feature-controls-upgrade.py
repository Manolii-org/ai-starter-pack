#!/usr/bin/env python3
"""Report-only, explicitly scoped feature-control dependency upgrade proposals."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("feature_controls_release", Path(__file__).with_name("feature-controls-release.py"))
assert SPEC and SPEC.loader
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
SLUG = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
SCOPE = r"[a-z][a-z0-9_-]*"
BRANCH = r"[A-Za-z0-9][A-Za-z0-9_./-]*"
INVENTORY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Feature-control opt-in consumer inventory",
    "type": "object", "additionalProperties": False,
    "required": ["manifest_version", "consumers"],
    "properties": {
        "manifest_version": {"const": 1},
        "consumers": {"type": "array", "items": {"$ref": "#/$defs/consumer"}},
    },
    "$defs": {
        "pin": {
            "type": "object", "additionalProperties": False,
            "required": ["manager", "file", "name", "pin"],
            "properties": {
                "manager": {"enum": ["npm", "python"]},
                "file": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "pin": {"type": "string", "minLength": 1},
                "section": {"enum": ["dependencies", "devDependencies", "optionalDependencies"]},
            },
            "allOf": [{"if": {"properties": {"manager": {"const": "npm"}}},
                       "then": {"required": ["section"]},
                       "else": {"not": {"required": ["section"]}}}],
        },
        "consumer": {
            "type": "object", "additionalProperties": False,
            "required": ["repository", "ecosystem", "environment", "base", "checkout", "opt_in", "installed"],
            "properties": {
                "repository": {"type": "string", "pattern": f"^{SLUG}$"},
                "ecosystem": {"type": "string", "pattern": f"^{SCOPE}$"},
                "environment": {"type": "string", "pattern": f"^{SCOPE}$"},
                "base": {"type": "string", "pattern": f"^{BRANCH}$"},
                "checkout": {"type": "string", "minLength": 1},
                "opt_in": {"type": "boolean"},
                "installed": {
                    "type": "object", "additionalProperties": False,
                    "required": ["sdk_version", "source_revision", "pins"],
                    "properties": {
                        "sdk_version": {"type": "string", "pattern": "^" + RELEASE.VERSION.pattern + "$"},
                        "source_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                        "pins": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/pin"}},
                    },
                },
            },
        },
    },
}


def pin_for(manager: str, name: str, sha: str) -> str:
    if manager == "npm":
        return f"git+{RELEASE.SOURCE_URL}#{sha}"
    if manager == "python":
        return f"{name} @ git+{RELEASE.SOURCE_URL}@{sha}#subdirectory={RELEASE.PYTHON_SUBDIRECTORY}"
    raise ValueError("unsupported dependency manager")


def npm_pin_matches(value: str, sha: str) -> bool:
    if not isinstance(value, str):
        return False
    source, separator, revision = value.partition("#")
    sources = {f"git+{RELEASE.SOURCE_URL}".lower(),
               f"git+ssh://git@github.com/{RELEASE.SOURCE_REPOSITORY}.git".lower(),
               f"github:{RELEASE.SOURCE_REPOSITORY}".lower()}
    return bool(separator and revision == sha and RELEASE.SHA.fullmatch(revision)
                and source.lower() in sources)


def version_key(version: str) -> tuple:
    RELEASE.python_version(version)
    match = RELEASE.VERSION.fullmatch(version)
    rank = {None: 3, "alpha": 0, "beta": 1, "rc": 2}[match[4]]
    return tuple(map(int, match.group(1, 2, 3))) + (rank, int(match[5] or 0))


def patch_for(relative: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile="a/" + relative, tofile="b/" + relative)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines)


def validate_inventory(value: dict) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {"manifest_version", "consumers"} or type(value["manifest_version"]) is not int or value["manifest_version"] != 1:
        raise ValueError("unsupported inventory manifest")
    if not isinstance(value["consumers"], list):
        raise TypeError("consumers must be an explicit list")
    seen = set()
    for consumer in value["consumers"]:
        expected = {"repository", "ecosystem", "environment", "base", "checkout", "opt_in", "installed"}
        if not isinstance(consumer, dict) or set(consumer) != expected:
            raise ValueError("consumer requires repository/ecosystem/environment/base/checkout/opt_in/installed")
        for field, pattern in (("repository", SLUG), ("ecosystem", SCOPE), ("environment", SCOPE), ("base", BRANCH)):
            if not isinstance(consumer[field], str) or not re.fullmatch(pattern, consumer[field]):
                raise ValueError(f"invalid consumer {field}")
        if ".." in consumer["base"] or consumer["base"].endswith("/") or consumer["base"].startswith("-"):
            raise ValueError("invalid base branch")
        if not isinstance(consumer["opt_in"], bool):
            raise TypeError("opt_in must be explicit boolean")
        RELEASE.safe_relative(consumer["checkout"])
        identity = (consumer["repository"].lower(), consumer["environment"])
        if identity in seen:
            raise ValueError("duplicate consumer repository/environment")
        seen.add(identity)
        installed = consumer["installed"]
        if not isinstance(installed, dict) or set(installed) != {"sdk_version", "source_revision", "pins"}:
            raise ValueError("installed version/source/pins are required")
        RELEASE.python_version(installed["sdk_version"])
        if not isinstance(installed["source_revision"], str) or not RELEASE.SHA.fullmatch(installed["source_revision"]):
            raise ValueError("installed source must be a full SHA")
        if not isinstance(installed["pins"], list) or not installed["pins"]:
            raise ValueError("explicit dependency pins required")
        files = set()
        for pin in installed["pins"]:
            if not isinstance(pin, dict):
                raise TypeError("dependency pin must be an object")
            manager = pin.get("manager")
            required = {"manager", "file", "name", "pin"} | ({"section"} if manager == "npm" else set())
            if set(pin) != required or manager not in {"npm", "python"}:
                raise ValueError("unsupported dependency pin shape")
            path = RELEASE.safe_relative(pin["file"])
            if pin["file"] in files:
                raise ValueError("duplicate consumer dependency file")
            files.add(pin["file"])
            if manager == "npm":
                if path.name != "package.json" or pin["section"] not in {"dependencies", "devDependencies", "optionalDependencies"}:
                    raise ValueError("npm pin must target an explicit package.json dependency section")
            elif not re.fullmatch(r"requirements(?:[-_.][a-z0-9_-]+)?\.(txt|in|lock)", path.name):
                raise ValueError("Python bridge supports requirements files only")
            if not isinstance(pin["name"], str) or not re.fullmatch(r"[@a-zA-Z0-9_./-]+", pin["name"]):
                raise ValueError("invalid dependency name")
            valid = (npm_pin_matches(pin["pin"], installed["source_revision"]) if manager == "npm"
                     else pin["pin"] == pin_for(manager, pin["name"], installed["source_revision"]))
            if not valid:
                raise ValueError("installed dependency pin must use canonical Git source/full SHA")
    return value["consumers"]


def local_file(root: Path, relative: str) -> Path:
    path = root / RELEASE.safe_relative(relative)
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError("dependency file escapes checkout or is absent")
    return path


def dependency_content(root: Path, relative: str, committed: bool = False) -> str:
    content = local_file(root, relative).read_bytes().decode("utf-8")
    if committed and content != RELEASE.run(["git", "show", f"HEAD:{relative}"], root):
        raise ValueError("dependency file differs from consumer commit")
    return content


def checkout_identity(root: Path, consumer: dict) -> str:
    if Path(RELEASE.run(["git", "rev-parse", "--show-toplevel"], root).strip()).resolve() != root:
        raise ValueError("checkout must be its own Git root")
    remote = RELEASE.configured_origin(root)
    if RELEASE.repository_slug(remote).lower() != consumer["repository"].lower():
        raise ValueError("consumer origin/repository mismatch")
    if RELEASE.run(["git", "branch", "--show-current"], root).strip() != consumer["base"]:
        raise ValueError("consumer checkout is not on its declared base")
    if RELEASE.run(["git", "status", "--porcelain", "--untracked-files=all"], root).strip():
        raise ValueError("dirty consumer checkout")
    return RELEASE.run(["git", "rev-parse", "HEAD"], root).strip()


def replace_pin(content: str, pin: dict, wanted: str, checking: bool) -> tuple[str, bool]:
    if pin["manager"] == "npm":
        data = RELEASE.load_json(content)
        actual = data.get(pin["section"], {}).get(pin["name"])
        if checking:
            return content, npm_pin_matches(actual, wanted.rsplit("#", 1)[1])
        if actual != pin["pin"]:
            raise ValueError("installed npm pin drift; refusing replacement")
        if npm_pin_matches(actual, wanted.rsplit("#", 1)[1]):
            return content, True
        # Edit only the exact dependency token, preserving all unrelated bytes.
        token = re.compile(r'("' + re.escape(pin["name"]) + r'"\s*:\s*)' + re.escape(json.dumps(actual)))
        matches = list(token.finditer(content))
        if len(matches) != 1:
            raise ValueError("ambiguous npm dependency token")
        return token.sub(lambda match: match[1] + json.dumps(wanted), content), False
    lines = content.splitlines(keepends=True)
    entries = [index for index, line in enumerate(lines) if re.match(r"^\s*" + re.escape(pin["name"]) + r"\s*@", line)]
    if len(entries) != 1:
        raise ValueError("Python dependency is missing/ambiguous")
    index = entries[0]
    actual = lines[index].rstrip("\r\n")
    if checking:
        return content, actual == wanted
    if actual != pin["pin"]:
        raise ValueError("installed Python pin drift; refusing replacement")
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    lines[index] = wanted + newline
    return "".join(lines), actual == wanted


def check_npm_lock(root: Path, pin: dict, wanted: str, sha: str, committed: bool = False) -> bool:
    if not npm_pin_matches(wanted, sha):
        return False
    relative = (Path(pin["file"]).parent / "package-lock.json").as_posix()
    lock = RELEASE.load_json(dependency_content(root, relative, committed))
    packages = lock.get("packages", {})
    entry = packages.get("node_modules/" + pin["name"], {})
    resolved = entry.get("resolved")
    return (npm_pin_matches(packages.get("", {}).get(pin["section"], {}).get(pin["name"]), sha)
            and npm_pin_matches(resolved, sha))


def propose(inventory: dict, manifest: dict, workspace: Path, owner: str,
            ecosystem: str, environment: str, checking: bool = False) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", owner):
        raise ValueError("explicit GitHub owner required")
    if not re.fullmatch(SCOPE, ecosystem) or not re.fullmatch(SCOPE, environment):
        raise ValueError("explicit ecosystem/environment scope required")
    workspace = workspace.resolve(strict=True)
    consumers = validate_inventory(inventory)
    selected = [consumer for consumer in consumers if consumer["opt_in"]
                and consumer["ecosystem"] == ecosystem and consumer["environment"] == environment]
    if not selected:
        raise ValueError("no opted-in consumers in the requested scope")
    if any(consumer["repository"].split("/")[0].lower() != owner.lower() for consumer in selected):
        raise ValueError("selected scope crosses the explicit GitHub owner boundary")
    report = {"manifest_version": 1, "source_revision": manifest["source_revision"],
              "sdk_version": manifest["sdk_version"], "wire_schema_version": manifest["schema_version"],
              "scope": {"owner": owner, "ecosystem": ecosystem, "environment": environment},
              "mode": "check" if checking else "proposal", "consumers": []}
    roots = set()
    for consumer in selected:
        if version_key(manifest["sdk_version"]) < version_key(consumer["installed"]["sdk_version"]):
            raise ValueError("upgrade proposal cannot downgrade installed SDK version")
        root = workspace / consumer["checkout"]
        if root.is_symlink() or not root.resolve().is_relative_to(workspace):
            raise ValueError("checkout escapes explicit workspace")
        root = root.resolve(strict=True)
        if root in roots:
            raise ValueError("duplicate checkout in selected scope")
        roots.add(root)
        head = checkout_identity(root, consumer)
        item = {"repository": consumer["repository"], "base": consumer["base"], "base_revision": head,
                "changes": [], "current": True, "follow_up": [], "pin_record": None}
        for pin in consumer["installed"]["pins"]:
            if pin["name"] != manifest["packages"][pin["manager"]]["name"]:
                raise ValueError("consumer dependency name differs from release package")
            content = dependency_content(root, pin["file"], committed=True)
            wanted = pin_for(pin["manager"], pin["name"], manifest["source_revision"])
            updated, current = replace_pin(content, pin, wanted, checking)
            if checking and pin["manager"] == "npm":
                current = current and check_npm_lock(root, pin, wanted, manifest["source_revision"], committed=True)
            item["current"] = item["current"] and current
            if updated != content:
                item["changes"].append({
                    "file": pin["file"], "before_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "patch": patch_for(pin["file"], content, updated),
                })
                item["follow_up"].append("Regenerate dependency locks with the consumer package manager; run compatibility CI; review PR. No activation/deployment authorization.")
        if checkout_identity(root, consumer) != head:
            raise ValueError("consumer changed while proposal was being read")
        if checking and item["current"]:
            item["pin_record"] = {"sourceSHA": manifest["source_revision"], "sdkVersion": manifest["sdk_version"],
                                  "wireSchemaVersion": manifest["schema_version"], "consumerSHA": head}
        report["consumers"].append(item)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["schema", "propose", "check"])
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--source-root", type=Path, default=RELEASE.ROOT)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--owner")
    parser.add_argument("--ecosystem")
    parser.add_argument("--environment")
    args = parser.parse_args()
    if args.mode == "schema":
        print(json.dumps(INVENTORY_SCHEMA, indent=2))
        return 0
    if any(getattr(args, key) is None for key in ("inventory", "release", "workspace", "owner", "ecosystem", "environment")):
        parser.error("inventory/release/workspace/owner/ecosystem/environment are required")
    try:
        manifest = RELEASE.verify_artifacts(args.release)
        RELEASE.verify_source(args.source_root, manifest, allow_untagged=True)
        report = propose(RELEASE.load_json(args.inventory.read_text()), manifest, args.workspace,
                         args.owner, args.ecosystem, args.environment, args.mode == "check")
        print(json.dumps(report, indent=2))
        return int(args.mode == "check" and not all(item["current"] for item in report["consumers"]))
    except (OSError, ValueError, KeyError, TypeError, RELEASE.configparser.Error,
            RELEASE.tarfile.TarError, RELEASE.zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"feature-controls-upgrade: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
