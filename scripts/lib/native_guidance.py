"""Native policy delivery for curated registry consumers (no settings merge).

Uses the resolver's scope/pin validation, repository mutex, no-follow reads and
atomic writer. Owns named blocks and three skills with eval fixtures, never whole local trees.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

NAMES = ("adaptable-development", "design-feature-adoption", "compatibility-improvement")
LOCK = ".ai/native-guidance-lock.json"
BEGIN = b"<!-- ai-starter-pack:development-policy:begin -->"
END = b"<!-- ai-starter-pack:development-policy:end -->"
SCHEMA = "native-guidance/v1"
SURFACES = {"claude-code", "codex", "cursor", "devin"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def body(data: bytes) -> bytes:
    if not data.startswith(b"---\n") or b"\n---\n" not in data[4:]:
        raise ValueError("policy must have YAML frontmatter")
    return data.split(b"\n---\n", 1)[1].strip() + b"\n"


def block(data: bytes) -> bytes | None:
    if BEGIN not in data and END not in data:
        return None
    if data.count(BEGIN) != 1 or data.count(END) != 1:
        raise ValueError("duplicate or incomplete policy markers; reconcile manually")
    start, end = data.index(BEGIN), data.index(END) + len(END)
    if end <= start:
        raise ValueError("reversed policy markers")
    return data[start:end]


def merge(data: bytes, wanted: bytes, recorded: str | None) -> bytes:
    previous = block(data)
    if previous is not None:
        if previous != wanted and (not recorded or digest(previous) != recorded):
            raise ValueError("managed policy block was edited; reconcile manually")
        return data.replace(previous, wanted, 1)
    if recorded:
        raise ValueError("managed policy block was removed; reconcile manually")
    # Keep the entire pre-existing file byte-for-byte after the new block.
    return wanted + (b"\n\n" + data if data else b"\n")


def live_import(data: bytes, target: bytes) -> bool:
    """Recognise a standalone live import, never an example in a code fence."""
    fence, comment = None, False
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if fence is None:
            # Comments may start after prose, not just at column zero.
            while line:
                if comment:
                    if b"-->" not in line:
                        line = b""
                        break
                    line = line.split(b"-->", 1)[1].strip()
                    comment = False
                start = line.find(b"<!--")
                if start < 0:
                    break
                closing = line.find(b"-->", start + 4)
                if closing < 0:
                    line, comment = line[:start].rstrip(), True
                    break
                line = line[:start] + line[closing + 3:]
        marker = re.match(rb"(`{3,}|~{3,})", line)
        if marker:
            delimiter = marker[1]
            if fence is None:
                fence = delimiter
            elif (delimiter[:1] == fence[:1] and len(delimiter) >= len(fence)
                  and not line[len(delimiter):].strip()):
                fence = None
            continue
        if fence is None and not raw_line.startswith((b"    ", b"\t")) and line == target:
            return True
    return False


def file_snapshot(root: Path, rel: str, resolver):
    """Read an existing regular file, or prove a missing leaf is not linked."""
    if not os.path.lexists(root / rel):
        p = root / rel
        if p.resolve() != p:
            raise ValueError(f"unsafe linked path: {rel}")
        return None
    snap = resolver._read_source(root, rel)
    if snap is None:
        raise ValueError(f"unsafe or unreadable destination: {rel}")
    return snap[0], snap[1].st_mode & 0o777


def apply_outputs(root: Path, outputs: dict, snapshots: dict, resolver) -> None:
    """Install a fully preflighted batch with guarded, atomic file recovery.

    Caller holds the existing repository mutex. Ordered outputs put a native
    ownership lock last; bounded mirror refresh uses the same file transaction.
    External editors/generators must be quiescent for the affected paths. The
    mutex coordinates participating writers only; snapshot checks plus rename
    are not a content compare-and-swap against uncoordinated writers.
    """
    changes = [rel for rel, data in outputs.items() if snapshots[rel] is None or snapshots[rel][0] != data]
    dfds, written = {}, []
    snapshot = lambda rel: file_snapshot(root, rel, resolver)

    def require_snapshot(rel, expected, stage):
        if snapshot(rel) != expected:
            raise ValueError(f"{rel}: changed {stage}; reconcile concurrent work")

    try:
        for rel in changes:
            parent = Path(rel).parent.as_posix()
            if parent not in dfds:
                if not resolver._HAS_DIRFD:
                    raise ValueError("native guidance apply requires descriptor-relative file support")
                dfds[parent] = resolver.secure_dir_fd(root, parent)
        for rel in outputs:
            if snapshot(rel) != snapshots[rel]:
                raise ValueError(f"{rel}: changed during planning; retry after reconciliation")
        for rel in changes:
            if snapshot(rel) != snapshots[rel]:
                raise ValueError(f"{rel}: changed before write; reconcile concurrent work")
            data = outputs[rel]
            resolver.atomic_replace(root / rel, lambda f, data=data: f.write(data),
                                    mode=snapshots[rel][1] if snapshots[rel] else 0o644,
                                    dfd=dfds[Path(rel).parent.as_posix()],
                                    before_replace=lambda rel=rel: require_snapshot(
                                        rel, snapshots[rel], "before replacement"))
            written.append(rel)
    except Exception:
        for rel in reversed(written):
            if snapshot(rel) != (outputs[rel], snapshots[rel][1] if snapshots[rel] else 0o644):
                raise ValueError(f"{rel}: concurrent edit during rollback; retained for manual recovery")
            dfd = dfds[Path(rel).parent.as_posix()]
            old = snapshots[rel]
            if old is None:
                require_snapshot(rel, (outputs[rel], 0o644),
                                 "before rollback removal; retained for manual recovery")
                os.unlink(Path(rel).name, dir_fd=dfd)
            else:
                expected = (outputs[rel], old[1])
                resolver.atomic_replace(root / rel, lambda f, data=old[0]: f.write(data), mode=old[1], dfd=dfd,
                                        before_replace=lambda rel=rel, expected=expected: require_snapshot(
                                            rel, expected, "during rollback; retained for manual recovery"))
        raise
    finally:
        for dfd in dfds.values():
            os.close(dfd)


def instruction_audit(root: Path, outputs: dict[str, bytes]) -> dict:
    """Measure discovered chains, including overrides and configured fallbacks.

    This is static discoverability evidence, not an assertion of host loading.
    Host-injected instructions and command-line config overrides remain unknown.
    """
    limit, fallbacks = 32768, []
    config_sources = []
    try:
        import tomllib
    except ImportError:
        tomllib = None
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for config in (codex_home / "config.toml", root / ".codex/config.toml"):
        if config.is_file():
            if tomllib is None:
                raise ValueError("Python 3.11+ required to inspect configured Codex discovery")
            settings = tomllib.loads(config.read_text())
            limit = settings.get("project_doc_max_bytes", limit)
            fallbacks = settings.get("project_doc_fallback_filenames", fallbacks)
            config_sources.append(str(config))
    if type(limit) is not int or limit <= 0 or not isinstance(fallbacks, list) or any(
            not isinstance(n, str) or Path(n).name != n for n in fallbacks):
        raise ValueError("invalid configured Codex instruction discovery")
    global_bytes = 0
    for name in ("AGENTS.override.md", "AGENTS.md"):
        p = codex_home / name
        if p.is_file() and p.stat().st_size:
            global_bytes = p.stat().st_size + 2
            break
    names = ("AGENTS.override.md", "AGENTS.md", *fallbacks)
    # Git ignores do not affect Codex discovery. Restrict enumeration to literal
    # instruction filenames so ignored dependency/build trees do not flood stdout.
    patterns = [":(glob)**/" + glob.escape(name).replace("\\", "\\\\") for name in names]
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--", *patterns],
        capture_output=True, timeout=30)
    paths = (proc.stdout.decode().split("\0") if proc.returncode == 0 else
             [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()])
    dirs = {Path(".")} | {Path(p).parent for p in paths if Path(p).name in names}
    chains = []
    for directory in sorted(dirs):
        chain, size = [], global_bytes
        for parent in reversed((directory, *directory.parents)):
            for name in names:
                rel = (parent / name).as_posix()
                content = outputs.get(rel)
                if content is None:
                    p = root / rel
                    content = p.read_bytes() if p.is_file() else b""
                if content:
                    chain.append(rel)
                    size += len(content) + 2
                    break
        chains.append({"directory": directory.as_posix(), "files": chain, "bytes": size})
    largest = max(chains, key=lambda c: c["bytes"])
    if largest["bytes"] > limit:
        raise ValueError(f"Codex chain {largest['directory']} is {largest['bytes']} bytes; limit {limit}; reconcile guidance, do not raise the limit to hide it")
    return {"codex_limit": limit, "global_bytes": global_bytes,
            "config_sources": config_sources, "chains": chains,
            "host_overrides": "not inspected; confirm in a fresh target session"}


def resolve(args, root: Path, resolver, manifest: dict, registry: Path, index: dict) -> int:
    try:
        return _resolve(args, root, resolver, manifest, registry, index)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"CONFLICT: native guidance: {exc}")
        return 1


def _resolve(args, root, resolver, manifest, registry, index):
    selected = manifest.get("surfaces", ["claude-code"])
    surfaces = [surface for surface in selected if surface in SURFACES]
    for surface in selected:
        if surface not in SURFACES:
            print(f"  note    surface '{surface}' is advisory-only for native guidance")
    if not surfaces:
        raise ValueError("native policy supports claude-code, codex, cursor and devin only")
    plan = resolver.Plan()
    for req in manifest["requires"]:
        resolver.plan_requirement(req["plugin"], req["ref"], str(manifest["universe"]),
                                  registry, index, root, {}, plan, write_components=False)
    if plan.conflicts:
        raise ValueError("; ".join(reason for _, reason in plan.conflicts))
    entries = [r for r in plan.resolved if r["plugin"] == "platform/framework"]
    if len(entries) != 1:
        raise ValueError("requires must select platform/framework exactly once")
    entry = entries[0]
    plugin = registry.parent / entry["source"]
    plugin_relative = plugin.relative_to(registry).as_posix()
    pinned_commit = plan.pins.get("platform/framework")
    flags = manifest.get("feature_flags", {})
    if not isinstance(flags, dict):
        raise ValueError("feature_flags must be a mapping")
    skill_root = flags.get("native_guidance_skill_root", ".claude/skills")
    if not isinstance(skill_root, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", skill_root) or any(
            part in {".", "..", ".git"} for part in Path(skill_root).parts):
        raise ValueError("invalid native_guidance_skill_root")
    if skill_root != ".claude/skills":
        canonical = root / skill_root
        if canonical.resolve() != canonical or (root / ".claude/skills").resolve() != canonical:
            raise ValueError("custom skill root must be the explicit repository-local canonical target of .claude/skills")
    sources, evals = {}, {}
    for name in NAMES:
        for leaf, destination in (("SKILL.md", sources), ("evals/cases.json", evals)):
            rel = f"skills/{name}/{leaf}"
            registry_rel = f"{plugin_relative}/{rel}"
            snapshot = resolver._read_source(registry, registry_rel)
            if snapshot is None:
                raise ValueError(f"missing or unsafe policy source: {rel}; use a pack revision containing this policy")
            data = snapshot[0]
            if pinned_commit:
                blob = subprocess.run(["git", "-C", str(registry), "show", f"{pinned_commit}:./{registry_rel}"],
                                      capture_output=True, timeout=10)
                if blob.returncode or blob.stdout != data:
                    raise ValueError(f"{rel} differs from the pinned object")
            destination[name] = data
    metadata = resolver._frontmatter(sources[NAMES[0]])
    version = metadata.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("policy needs a semantic version")
    policy = body(sources[NAMES[0]])
    if not 250 <= len(policy.split()) <= 350:
        raise ValueError("standing policy must remain 250–350 words")
    wanted = BEGIN + b"\n" + (
        f"<!-- Generated from platform/framework/skills/adaptable-development/SKILL.md; policy {version}; sha256 {digest(sources[NAMES[0]])}. -->\n"
    ).encode() + policy + END

    def snapshot(rel):
        return file_snapshot(root, rel, resolver)

    old_lock = snapshot(LOCK)
    lock = json.loads(old_lock[0]) if old_lock else {"schema": SCHEMA, "files": {}}
    if lock.get("schema") != SCHEMA or not isinstance(lock.get("files"), dict):
        raise ValueError("invalid native guidance lock; reconcile manually")
    records = lock["files"]
    snapshots, outputs, kinds = {}, {}, {}

    def add(rel, data, kind="file"):
        previous = snapshots.setdefault(rel, snapshot(rel))
        current = previous[0] if previous else b""
        record = records.get(rel, {})
        if not isinstance(record, dict):
            raise ValueError(f"{rel}: invalid native ownership record")
        if kind == "block":
            result = merge(current, data, record.get("sha256"))
        else:
            if previous and current != data and digest(current) != record.get("sha256"):
                raise ValueError(f"{rel}: local file differs; reconcile manually")
            result = data
        outputs[rel], kinds[rel] = result, kind

    add("AGENTS.md", wanted, "block")
    if (root / "AGENTS.override.md").is_file():
        add("AGENTS.override.md", wanted, "block")
    if "claude-code" in surfaces:
        rel = ".claude/CLAUDE.md" if not (root / "CLAUDE.md").exists() and (root / ".claude/CLAUDE.md").exists() else "CLAUDE.md"
        # Import only the canonical standing skill: curated AGENTS tails can be
        # large and repeat Claude's existing operating rules. Preserve an
        # unowned live AGENTS import, which already loads the standing policy.
        prefix = "../" if rel.startswith(".claude/") else ""
        target = f"@{prefix}{skill_root}/adaptable-development/SKILL.md".encode()
        legacy_target = b"@../AGENTS.md" if rel.startswith(".claude/") else b"@AGENTS.md"
        previous = snapshot(rel)
        loaded = previous and (live_import(previous[0], target) or live_import(previous[0], legacy_target))
        if not loaded or block(previous[0]):
            add(rel, BEGIN + b"\n" + target + b"\n" + END, "block")
    if "cursor" in surfaces:
        add(".cursor/rules/adaptable-development.mdc", b"---\ndescription: Apply the shared development policy and select the relevant task procedure.\nalwaysApply: true\n---\n\nEssential policy is in the discovered root AGENTS.md. Before design handoffs, new features or substantial redesigns read `.claude/skills/design-feature-adoption/SKILL.md`. Before existing-app refactoring read `.claude/skills/compatibility-improvement/SKILL.md`. Keep trivial unrelated edits proportionate. Referenced procedure contents require an explicit read.\n")
    for name, data in sources.items():
        add(f"{skill_root}/{name}/SKILL.md", data)
        add(f"{skill_root}/{name}/evals/cases.json", evals[name])
    # Never silently abandon a surface that a previous install managed.
    if set(records) - set(outputs):
        raise ValueError("surface selection removes managed files; revert their owning commit explicitly")
    for rel in ("AGENTS.md", "AGENTS.override.md"):
        if "devin" in surfaces and rel in outputs and outputs[rel].index(END) + len(END) > 16384:
            raise ValueError(f"{rel}: policy lies beyond Devin's 16 KiB inclusion limit")
    audit = instruction_audit(root, outputs) if "codex" in surfaces else None
    new_lock = {"schema": SCHEMA, "plugin": "platform/framework", "ref": entry["ref"],
                "resolved_version": entry["resolved_version"], "policy_version": version,
                "source_sha256": digest(sources[NAMES[0]]), "surfaces": surfaces,
                "files": {rel: {"kind": kinds[rel], "sha256": digest(block(data) if kinds[rel] == "block" else data)}
                          for rel, data in sorted(outputs.items())}}
    if pinned_commit:
        new_lock["resolved_commit"] = pinned_commit
    snapshots[LOCK] = old_lock
    outputs[LOCK] = (json.dumps(new_lock, indent=2, sort_keys=True) + "\n").encode()
    changes = [rel for rel, data in outputs.items() if snapshots[rel] is None or snapshots[rel][0] != data]
    print(f"Native policy {version}: {', '.join(surfaces)}; {len(policy.split())} standing words")
    if audit is not None:
        print(f"Codex largest instruction chain: {max(c['bytes'] for c in audit['chains'])}/{audit['codex_limit']} bytes; global {audit['global_bytes']} bytes")
    else:
        print("Codex instruction audit: not requested (codex not selected)")
    for rel in changes:
        print(f"  {'write' if args.apply else 'would-write'} {rel}")
    if args.check:
        print("DRIFT" if changes else "OK: installed native guidance matches generated sources; loading/behaviour require fresh-session probes")
        return int(bool(changes))
    if not args.apply:
        print("DRY RUN: no files written; loading and behaviour not verified")
        return 0
    apply_outputs(root, outputs, snapshots, resolver)
    print("INSTALLED: native guidance; loading and behaviour not verified")
    return 0
