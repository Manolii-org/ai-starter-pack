#!/usr/bin/env python3
"""Test suite for classify-message.py's _build_dispatch_rule() — verifies that
OSS-routed agents land in the correct dispatch bucket, and in particular that
reasoning-tier aliases (tier-review) are NOT folded into the haiku bucket
(which would discard their large max_tokens / reasoning budget)."""
import unittest
import importlib.util
from pathlib import Path

# Load classify-message.py as a module (hyphenated filename).
_module_path = Path(__file__).resolve().parent.parent / "scripts" / "classify-message.py"
_spec = importlib.util.spec_from_file_location("classify_message", _module_path)
classify_message = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classify_message)

build = classify_message._build_dispatch_rule


def _cfg(agent_routing):
    """Minimal model-routing.json-shaped dict with the tier aliases under test."""
    return {
        "tier_aliases": {
            "tier-1-fast": {},
            "tier-review": {},
            "sonnet": {},
        },
        "proxy_intercepted_models": {"haiku": {}},
        "agent_routing": agent_routing,
    }


class TestReasoningTierBucket(unittest.TestCase):
    def test_reasoning_agent_not_in_haiku_bucket(self):
        """An agent routed to tier-review must NOT appear in the haiku bucket."""
        rule = build(_cfg({"editor": "tier-review"}))
        haiku_segment = rule.split("sonnet=")[0]
        self.assertNotIn("editor", haiku_segment,
                         "tier-review agent leaked into the haiku dispatch bucket")

    def test_reasoning_agent_gets_own_clause(self):
        """A tier-review agent is dispatched under its own reasoning clause."""
        rule = build(_cfg({"editor": "tier-review"}))
        self.assertIn("tier-review=reasoning/editorial (editor)", rule)

    def test_no_reasoning_clause_when_unused(self):
        """Instances with no tier-review agent pay zero extra dispatch tokens."""
        rule = build(_cfg({"searcher": "tier-1-fast"}))
        self.assertNotIn("tier-review=", rule)

    def test_haiku_tier_still_buckets_to_haiku(self):
        """Regression: ordinary OSS tiers stay in the haiku bucket."""
        rule = build(_cfg({"searcher": "tier-1-fast"}))
        haiku_segment = rule.split("sonnet=")[0]
        self.assertIn("searcher", haiku_segment)

    def test_sonnet_alias_still_buckets_to_sonnet(self):
        """Regression: the sonnet proxy alias stays in the sonnet bucket."""
        rule = build(_cfg({"reviewer": "sonnet"}))
        self.assertIn("sonnet=(reviewer)", rule)

    def test_anthropic_locked_agent_listed_restricted(self):
        """Regression: an agent on a non-OSS model is Anthropic-locked."""
        rule = build(_cfg({"secrets": "claude-sonnet-4-6"}))
        self.assertIn("claude-sonnet-4-6=restricted/client (secrets)", rule)


class TestRobustness(unittest.TestCase):
    def test_null_config_keys_do_not_crash(self):
        """Keys present but explicitly null must not raise AttributeError."""
        rule = build({"tier_aliases": None, "proxy_intercepted_models": None,
                      "agent_routing": None})
        self.assertIn("DISPATCH RULE", rule)

    def test_missing_config_keys_do_not_crash(self):
        """Entirely absent keys fall back to empty without error."""
        rule = build({})
        self.assertIn("DISPATCH RULE", rule)

    def test_explore_agent_not_duplicated(self):
        """An agent literally named 'Explore' on a haiku tier is listed once."""
        rule = build(_cfg({"Explore": "tier-1-fast"}))
        haiku_segment = rule.split("sonnet=")[0]
        self.assertEqual(haiku_segment.count("Explore"), 1)


if __name__ == "__main__":
    unittest.main()
