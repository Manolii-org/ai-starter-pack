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


def test_pack_pins_are_grouped_into_one_reviewed_pr() -> None:
    rules = json.loads((ROOT / "default.json").read_text())["packageRules"]
    index, rule = next(
        (i, r) for i, r in enumerate(rules) if r.get("groupName") == "manolii-ai-starter-pack"
    )
    # Later packageRules win: the group must come after the digest automerge rule.
    automerge_index = next(i for i, r in enumerate(rules) if r.get("automerge") is True)
    assert index > automerge_index
    assert rule["automerge"] is False and rule["platformAutomerge"] is False

    # Renovate regex form "/<re>/i" — exercise the shipped expression.
    body, flags = rule["matchPackageNames"][0][1:].rsplit("/", 1)
    name = re.compile(body, re.IGNORECASE if "i" in flags else 0)
    for dep in ("Manolii-org/ai-starter-pack", "manolii-org/ai-starter-pack"):
        assert name.search(dep)
    assert not name.search("Manolii-org/ai-starter-pack-fork")

    allowed = re.compile(rule["allowedVersions"].strip("/"))
    for tag in ("v1.23.0", "1.23.0", "v2.0.0"):
        assert allowed.search(tag), tag
    for tag in ("v1", "email-capture-v0.2.6", "feature-controls-v0.1.0", "v0.2.6"):
        assert not allowed.search(tag), tag
