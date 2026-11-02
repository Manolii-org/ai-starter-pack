import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_pack_is_governed_by_ecosystem_renovate_policy() -> None:
    config = json.loads((ROOT / "renovate.json").read_text())

    assert config["extends"] == ["github>manolii-org/master"]


def test_pack_only_renovate_files_are_not_rendered_into_consumers() -> None:
    copier_config = yaml.safe_load((ROOT / "copier.yml").read_text())
    exclusions = set(copier_config["_exclude"])

    assert {"renovate.json", "tests/test_renovate_config.py"} <= exclusions


def test_preset_pin_manager_extracts_supported_consumer_pins() -> None:
    preset = json.loads((ROOT / "default.json").read_text())
    manager = next(
        item
        for item in preset["customManagers"]
        if item["description"].startswith("Track versioned AI Starter Pack")
    )
    # Renovate/RE2 uses (?<name>...); Python spells the same named capture
    # (?P<name>...). Translating just that token lets this unit test exercise
    # the shipped expression rather than maintaining a second regex.
    pattern = re.compile(manager["matchStrings"][0].replace("(?<", "(?P<"))

    for owner in ("Manolii-org", "manolii-org"):
        match = pattern.search(
            f'{{"extends":["github>{owner}/ai-starter-pack#v1.9.0"]}}'
        )
        assert match is not None
        assert match.group("currentValue") == "v1.9.0"

    assert pattern.search(
        '{"extends":["github>Manolii-org/ai-starter-pack"]}'
    ) is None


def test_pack_ref_inputs_track_the_uses_pin() -> None:
    """`pack_ref: <sha> # vX.Y.Z` must be updated with the `uses:` pin, or a
    release PR runs new workflow YAML against old pack code."""
    preset = json.loads((ROOT / "default.json").read_text())
    manager = next(m for m in preset["customManagers"]
                   if "pack_ref" in m["description"])
    assert manager["depNameTemplate"] == "Manolii-org/ai-starter-pack"
    assert manager["datasourceTemplate"] == "github-tags"
    pattern = re.compile(manager["matchStrings"][0].replace("(?<", "(?P<"))
    files = [re.compile(f.strip("/")) for f in manager["managerFilePatterns"]]
    assert any(f.search(".github/workflows/ci.yml") for f in files)
    assert not any(f.search("docs/ci.yml") for f in files)

    sha = "3" * 40
    for line in (f"      pack_ref: {sha} # v1.25.1 — keep identical to the uses: pin",
                 f"      pack_ref: '{sha}'  #v1.25.1"):
        m = pattern.search(line)
        assert m, line
        assert (m.group("currentDigest"), m.group("currentValue")) == (sha, "v1.25.1")
    # Tag-form or unversioned pack_ref is not a digest pin and is not matched.
    assert not pattern.search("      pack_ref: v1.25.1")
    assert not pattern.search(f"      pack_ref: {sha}")

    # Every SHA pack_ref example in the reusable docs is trackable.
    doc = (ROOT / ".github/workflows/REUSABLE-WORKFLOWS.md").read_text()
    examples = [l for l in doc.splitlines()
                if re.match(r"\s*pack_ref:\s*['\"]?[0-9a-f]{40}", l)]
    assert examples and all(pattern.search(l) for l in examples)


def test_pack_workflow_pins_are_converted_to_sha_pins() -> None:
    """Tag pins of pack reusables run whatever the tag points at today; the
    preset must make Renovate pin them to commit SHAs (github-actions only —
    the regex managers have no digest capture)."""
    rules = json.loads((ROOT / "default.json").read_text())["packageRules"]
    pin_rules = [r for r in rules if r.get("pinDigests") is True]
    assert len(pin_rules) == 1
    rule = pin_rules[0]
    assert rule["matchManagers"] == ["github-actions"]
    body, flags = rule["matchPackageNames"][0][1:].rsplit("/", 1)
    name = re.compile(body, re.IGNORECASE if "i" in flags else 0)
    assert name.search("Manolii-org/ai-starter-pack")
    assert not name.search("actions/checkout")
    # Must not re-enable automerge for pack pins.
    assert "automerge" not in rule


def test_pack_pins_are_grouped_into_one_reviewed_pr() -> None:
    rules = json.loads((ROOT / "default.json").read_text())["packageRules"]
    index, rule = next(
        (i, r) for i, r in enumerate(rules) if r.get("groupName") == "manolii-ai-starter-pack"
    )
    # Later packageRules win: the group must come after the digest automerge
    # rule (the feature-control SDK rule stays last; see its own test).
    automerge_index = next(i for i, r in enumerate(rules) if r.get("automerge") is True)
    assert index > automerge_index
    assert rule["automerge"] is False and rule["platformAutomerge"] is False

    # Renovate regex form "/<re>/i" — exercise the shipped expression.
    body, flags = rule["matchPackageNames"][0][1:].rsplit("/", 1)
    name = re.compile(body, re.IGNORECASE if "i" in flags else 0)
    for dep in ("Manolii-org/ai-starter-pack", "manolii-org/ai-starter-pack"):
        assert name.search(dep)
    assert not name.search("example/ai-starter-pack")

    allowed = re.compile(rule["allowedVersions"].strip("/"))
    for tag in ("v1.23.0", "1.23.0", "v2.0.0"):
        assert allowed.search(tag), tag
    for tag in ("v1", "email-capture-v0.2.6", "feature-controls-v0.1.0", "v0.2.6"):
        assert not allowed.search(tag), tag
