"""Tests for scripts/memory-decay.py — Memory Evolution Phase 7 (decay + consolidation)."""
import importlib.util
import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("memory_decay", _SCRIPTS / "memory-decay.py")
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)

NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


class TestDecayMath:
    def test_formula_matches_prune_doc(self):
        # prune.md:88 — adjusted = base - (days/365)*rate
        assert abs(md.decay_confidence(0.9, 365, 0.1, 0.1) - 0.8) < 1e-9

    def test_floored(self):
        assert md.decay_confidence(0.2, 365 * 100, 0.1, 0.1) == 0.1

    def test_recent_entry_barely_decays(self):
        assert md.decay_confidence(0.8, 1, 0.1, 0.1) > 0.79

    def test_jaccard(self):
        assert md.jaccard({"a", "b"}, {"a", "b"}) == 1.0
        assert md.jaccard({"a"}, {"b"}) == 0.0
        assert md.jaccard(set(), set()) == 1.0


def _facts(tmp):
    f = Path(tmp) / "facts.jsonl"
    rows = [
        {"id": "a", "type": "fact",
         "content": "The Knowledge Layer MCP runs on Vercel knowledge-layer-cron endpoint",
         "tags": ["kl"], "confidence": 0.8, "entity_scope": "manolii",
         "created": (NOW - timedelta(days=365)).isoformat()},
        {"id": "b", "type": "fact",
         "content": "Knowledge Layer MCP runs on the Vercel knowledge-layer-cron endpoint today",
         "tags": ["mcp"], "confidence": 0.7, "entity_scope": "manolii",
         "created": (NOW - timedelta(days=10)).isoformat()},
        {"id": "c", "type": "fact",
         "content": "Completely unrelated fact about astroengine ephemeris caching strategy",
         "tags": ["astro"], "confidence": 0.6, "entity_scope": "manolii",
         "created": NOW.isoformat()},
        {"id": "d", "type": "fact",
         "content": "entry with no created field at all here",
         "tags": [], "confidence": 0.5, "entity_scope": "personal"},
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


class TestConsolidationAndDecay:
    def test_dry_run_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _facts(tmp)
            before = f.read_text()
            rep = md.run(str(f), apply=False, now=NOW)
            assert f.read_text() == before, "dry-run must not mutate the file"
            assert rep["entries"] == 4 and rep["dropped"] == 1

    def test_apply_merges_near_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _facts(tmp)
            md.run(str(f), apply=True, now=NOW)
            out = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
            assert len(out) == 3, "a+b merge -> 3 rows"
            kept = [r for r in out if r.get("reinforced")][0]
            assert kept["confidence"] == min(0.95, 0.8 + 0.05)
            assert set(kept["tags"]) == {"kl", "mcp"}
            assert kept["reinforced"] == 1
            assert "adjusted_confidence" in kept and "last_seen" in kept

    def test_missing_created_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _facts(tmp)
            md.run(str(f), apply=True, now=NOW)
            out = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
            d = [r for r in out if r["id"] == "d"][0]
            assert d["adjusted_confidence"] == 0.5  # days_since defaults to 0

    def test_decay_is_not_compounding(self):
        # adjusted is computed from the immutable base each run, so repeated
        # runs converge to a stable value rather than decaying further.
        with tempfile.TemporaryDirectory() as tmp:
            f = _facts(tmp)
            md.run(str(f), apply=True, now=NOW)
            first = {r["id"]: r.get("adjusted_confidence")
                     for r in (json.loads(x) for x in f.read_text().splitlines() if x.strip())}
            md.run(str(f), apply=True, now=NOW)
            second = {r["id"]: r.get("adjusted_confidence")
                      for r in (json.loads(x) for x in f.read_text().splitlines() if x.strip())}
            assert first == second, "re-run must be stable (no compounding decay)"

    def test_consolidate_only_skips_decay(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _facts(tmp)
            rep = md.run(str(f), apply=True, do_decay=False, now=NOW)
            out = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
            assert rep["dropped"] == 1
            assert all("adjusted_confidence" not in r for r in out)


class TestConsolidationValueVeto:
    def test_numeric_value_differs_never_merges(self):
        # "port 3000" vs "port 4000" — identical vocabulary, different value.
        a = {"content": "service uses port 3000 in staging"}
        b = {"content": "service uses port 4000 in staging"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_same_numeric_value_can_merge(self):
        a = {"content": "service uses port 3000 in staging"}
        b = {"content": "service uses port 3000 in staging environment"}
        assert md._rows_mergeable(a, b, 0.6)

    def test_negation_still_vetoes(self):
        a = {"content": "the flag is enabled"}
        b = {"content": "the flag is not enabled"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_short_negation_vetoes(self):
        # "no" is 2 chars — tokenize() drops it; the raw-text negation pass must catch it
        a = {"content": "email is enabled for production"}
        b = {"content": "email is no longer enabled for production"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_reversed_relationship_never_merges(self):
        # same tokens, swapped subject/object — a different claim, not a dup
        a = {"content": "staging uses production database"}
        b = {"content": "production uses staging database"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_value_substitution_never_merges(self):
        # one swapped non-numeric token — jaccard is high but the claims differ
        a = {"content": "the production service must use the primary database for query processing"}
        b = {"content": "the production service must use the replica database for query processing"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_subset_merge_keeps_more_complete_claim(self):
        # A high-confidence short fact must not win canonical selection over
        # its lower-confidence superset — the longer row carries qualifiers
        # (--apply) would otherwise drop silently.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "facts.jsonl"
            rows = [
                {"id": "short", "type": "fact",
                 "content": "deployment requires approval and security review",
                 "confidence": 0.95, "created": NOW.isoformat()},
                {"id": "long", "type": "fact",
                 "content": "deployment requires approval and security review for production",
                 "confidence": 0.5, "created": NOW.isoformat()},
            ]
            f.write_text("".join(json.dumps(r) + "\n" for r in rows))
            md.run(str(f), apply=True, now=NOW)
            out = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
            assert len(out) == 1
            assert out[0]["id"] == "long", "canonical must be the content-complete claim"

    def test_apply_decay_prefers_merged_confidence(self):
        from datetime import datetime, timezone
        row = {"content": "fact", "confidence": "medium", "merged_confidence": 0.65,
               "last_seen": "2026-09-27"}
        out = md.apply_decay([row], datetime(2026, 9, 27, tzinfo=timezone.utc))
        # base is the merged 0.65, not the categorical medium (0.6)
        assert out[0]["adjusted_confidence"] == 0.65


class TestKeeperPatternSchema:
    def test_keeper_patterns_consolidate(self):
        a = {"pattern": "verify retries", "context": "network calls", "example": "curl"}
        b = {"pattern": "verify retries on failure", "context": "network calls", "example": "curl -s"}
        assert md._rows_mergeable(a, b, 0.6)

    def test_keeper_distinct_contexts_do_not_merge(self):
        a = {"pattern": "verify retries", "context": "network calls"}
        b = {"pattern": "verify retries", "context": "database writes"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_keeper_context_substitution_never_merges(self):
        # High-overlap contexts differing in one content token (production vs
        # staging) describe different situations — the shared answer must not
        # merge them.
        a = {"pattern": "retry failed connections", "context": "production database connection failures"}
        b = {"pattern": "retry failed connections", "context": "staging database connection failures"}
        assert not md._rows_mergeable(a, b, 0.6)

    def test_keeper_context_subset_still_merges(self):
        a = {"pattern": "retry failed connections", "context": "database failures"}
        b = {"pattern": "retry failed connections", "context": "database failures under"}
        assert md._rows_mergeable(a, b, 0.6)

    def test_cross_schema_patterns_do_not_merge(self):
        learn = {"problem": "network calls", "solution": "verify retries", "rule": "always"}
        keeper = {"pattern": "verify retries", "context": "network calls"}
        assert not md._rows_mergeable(learn, keeper, 0.6)
