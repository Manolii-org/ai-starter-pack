#!/usr/bin/env python3
"""Validate migration identity/graph invariants without starting a database."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys

DRIZZLE_NAME = re.compile(r"^(?P<id>\d+)_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$")
SUPABASE_NAME = re.compile(r"^(?P<id>(?:\d{5}|\d{14}))_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$")
FLYWAY_NAME = re.compile(r"^V(?P<id>[0-9][0-9.]*)__[A-Za-z0-9][A-Za-z0-9_-]*\.sql$", re.I)
PRISMA_NAME = re.compile(r"^(?P<id>\d{14})_[A-Za-z0-9][A-Za-z0-9_-]*$")


def _duplicates(values: list[str]) -> set[str]:
    return {value for value in values if values.count(value) > 1}


def _sql_files(path: Path, *, ignored_suffixes: tuple[str, ...] = ()) -> list[Path]:
    files = sorted(path.glob("*.sql")) if path.is_dir() else []
    if ignored_suffixes:
        files = [file for file in files if not file.name.endswith(ignored_suffixes)]
    return files


def validate_names(
    path: Path,
    pattern: re.Pattern[str],
    *,
    ignored_suffixes: tuple[str, ...] = (),
    allowed_duplicates: dict[str, list[str]] | None = None,
) -> list[str]:
    files = _sql_files(path, ignored_suffixes=ignored_suffixes)
    if not files:
        return [f"{path}: no migration SQL files found"]
    errors: list[str] = []
    identifiers: list[str] = []
    for file in files:
        match = pattern.fullmatch(file.name)
        if not match:
            errors.append(f"{file}: malformed migration filename")
        else:
            identifiers.append(match.group("id"))
    allowed_duplicates = allowed_duplicates or {}
    for identifier in sorted(_duplicates(identifiers)):
        actual = sorted(file.name for file in files if pattern.fullmatch(file.name) and pattern.fullmatch(file.name).group("id") == identifier)
        expected = sorted(allowed_duplicates.get(identifier, []))
        if actual != expected:
            errors.append(f"{path}: duplicate migration identifier {identifier}")
    return errors


def validate_drizzle(path: Path, journal: Path | None) -> list[str]:
    errors = validate_names(path, DRIZZLE_NAME, ignored_suffixes=(".rollback.sql", ".down.sql"))
    journal = journal or path / "meta" / "_journal.json"
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"{journal}: unreadable Drizzle journal ({type(exc).__name__})"]
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return errors + [f"{journal}: entries must be an array"]
    tags: list[str] = []
    indices: list[int] = []
    timestamps: list[int] = []
    for offset, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{journal}: entry {offset} must be an object")
            continue
        tag, index, timestamp = entry.get("tag"), entry.get("idx"), entry.get("when")
        if not isinstance(tag, str) or not DRIZZLE_NAME.fullmatch(f"{tag}.sql"):
            errors.append(f"{journal}: entry {offset} has malformed tag")
        else:
            tags.append(tag)
            if not (path / f"{tag}.sql").is_file():
                errors.append(f"{journal}: tag {tag} has no matching SQL file")
        if not isinstance(index, int) or isinstance(index, bool):
            errors.append(f"{journal}: entry {offset} has invalid idx")
        else:
            indices.append(index)
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            errors.append(f"{journal}: entry {offset} has invalid when")
        else:
            timestamps.append(timestamp)
    if indices != list(range(len(indices))):
        errors.append(f"{journal}: idx values must be contiguous and ordered from zero")
    for label, values in (("tag", tags), ("idx", [str(v) for v in indices]), ("when", [str(v) for v in timestamps])):
        for value in sorted(_duplicates(values)):
            errors.append(f"{journal}: duplicate {label} {value}")
    forward_tags = {file.stem for file in _sql_files(path, ignored_suffixes=(".rollback.sql", ".down.sql"))}
    for tag in sorted(forward_tags - set(tags)):
        errors.append(f"{path / (tag + '.sql')}: missing from Drizzle journal")
    return errors


def _literal_assignment(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return object()
    return object()


def validate_alembic(path: Path) -> list[str]:
    files = sorted(path.glob("*.py")) if path.is_dir() else []
    if not files:
        return [f"{path}: no Alembic revision files found"]
    errors: list[str] = []
    revisions: dict[str, Path] = {}
    parents: dict[str, tuple[str, ...]] = {}
    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (OSError, SyntaxError) as exc:
            errors.append(f"{file}: unreadable Python revision ({type(exc).__name__})")
            continue
        revision = _literal_assignment(tree, "revision")
        down = _literal_assignment(tree, "down_revision")
        if not isinstance(revision, str) or not revision:
            errors.append(f"{file}: revision must be a non-empty string literal")
            continue
        if revision in revisions:
            errors.append(f"{file}: duplicate Alembic revision {revision} (also {revisions[revision]})")
        revisions[revision] = file
        if down is None:
            parents[revision] = ()
        elif isinstance(down, str):
            parents[revision] = (down,)
        elif isinstance(down, (tuple, list)) and all(isinstance(value, str) for value in down):
            parents[revision] = tuple(down)
        else:
            errors.append(f"{file}: down_revision must be null, a string, or a string sequence literal")
            parents[revision] = ()
    referenced = {parent for values in parents.values() for parent in values}
    for parent in sorted(referenced - set(revisions)):
        errors.append(f"{path}: missing Alembic parent revision {parent}")
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        errors.append(f"{path}: expected one Alembic head, found {len(heads)} ({', '.join(heads)})")
    return errors


def validate_prisma(path: Path) -> list[str]:
    directories = sorted(item for item in path.iterdir()) if path.is_dir() else []
    directories = [item for item in directories if item.is_dir()]
    if not directories:
        return [f"{path}: no Prisma migration directories found"]
    errors: list[str] = []
    identifiers: list[str] = []
    for directory in directories:
        match = PRISMA_NAME.fullmatch(directory.name)
        if not match:
            errors.append(f"{directory}: malformed Prisma migration directory")
        else:
            identifiers.append(match.group("id"))
        if not (directory / "migration.sql").is_file():
            errors.append(f"{directory}: migration.sql is missing")
    for identifier in sorted(_duplicates(identifiers)):
        errors.append(f"{path}: duplicate Prisma migration identifier {identifier}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, choices=["alembic", "drizzle", "flyway", "prisma", "supabase"])
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--baseline", type=Path, help="JSON allowlist for immutable historical duplicate groups")
    args = parser.parse_args(argv)
    allowed_duplicates: dict[str, list[str]] = {}
    if args.baseline:
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            allowed_duplicates = baseline["allowed_duplicate_identifiers"]
            if not isinstance(allowed_duplicates, dict) or not all(
                isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value)
                for key, value in allowed_duplicates.items()
            ):
                raise ValueError("invalid allowed_duplicate_identifiers")
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"Migration validation failed:\n- {args.baseline}: invalid baseline ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.adapter == "drizzle":
        errors = validate_drizzle(args.path, args.journal)
    elif args.adapter == "alembic":
        errors = validate_alembic(args.path)
    elif args.adapter == "prisma":
        errors = validate_prisma(args.path)
    else:
        pattern = SUPABASE_NAME if args.adapter == "supabase" else FLYWAY_NAME
        ignored = (".rollback.sql", ".down.sql") if args.adapter == "supabase" else ()
        errors = validate_names(
            args.path,
            pattern,
            ignored_suffixes=ignored,
            allowed_duplicates=allowed_duplicates,
        )
    if errors:
        print("Migration validation failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Migration validation passed: adapter={args.adapter} path={args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
