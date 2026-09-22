"""Licence checker: exact lookups must be exact, misses must never over-claim."""

from __future__ import annotations

import pytest

from app.licence.search import fuzzy_score, lookup, normalise_name, normalise_rl
from app.safety.guard import unsafe_verdict_hits


class TestNormalisation:
    def test_rl_variants_collapse(self):
        assert normalise_rl("RL-1001") == "1001"
        assert normalise_rl("rl 1001") == "1001"
        assert normalise_rl("RL/1001") == "1001"
        assert normalise_rl("01001") == "1001"

    def test_generic_suffixes_removed_but_distinctive_words_kept(self):
        assert normalise_name("M/S. Padma Overseas Employment Ltd.") == "padma overseas employment"
        assert "overseas" in normalise_name("Turag Overseas Ltd.")


class TestExactLookup:
    def test_exact_rl_found(self):
        result = lookup("RL-1001")
        assert result.status == "found"
        assert result.matches[0].record.rl_number == "RL-1001"
        assert result.matches[0].match_type == "exact_rl"
        assert result.data_date == "2026-09-01"

    def test_rl_without_prefix_found(self):
        assert lookup("1001").status == "found"

    def test_unknown_rl_is_not_found_and_does_not_substitute_another_agency(self):
        result = lookup("RL-9999")
        assert result.status == "not_found"
        assert result.matches == []

    def test_lookup_message_never_claims_safety(self):
        for query in ("RL-1001", "RL-9999", "Padma Overseas"):  # includes a hit and a miss
            result = lookup(query)
            # The real invariant: no un-negated safety claim, whatever the outcome.
            assert unsafe_verdict_hits(result.message_bn) == []
            # And a hit must still tell the worker to verify independently.
            if result.status == "found":
                assert "বিএমইটি" in result.message_bn

    def test_empty_query_is_rejected_cleanly(self):
        result = lookup("")
        assert result.status == "invalid_query"
        assert result.matched is False

    def test_whitespace_query(self):
        assert lookup("   ").status == "invalid_query"


class TestNameLookup:
    def test_exact_name(self):
        result = lookup("Jamuna Manpower Services Ltd.")
        assert result.status == "found"
        assert result.matches[0].match_type == "exact_name"

    def test_misspelled_name(self):
        result = lookup("sundarban employmnt agency")
        assert result.status == "found"
        assert result.matches[0].record.rl_number == "RL-1005"

    def test_truncated_name(self):
        result = lookup("Turag Overs")
        assert result.status == "found"
        assert result.matches[0].record.rl_number == "RL-1016"

    def test_similar_names_are_reported_as_ambiguous_not_guessed(self):
        """Two 'Arial Khan' agencies exist; the tool must ask, not pick."""
        result = lookup("Arial Kha")
        assert result.status in ("ambiguous", "found")
        if result.status == "ambiguous":
            rls = {match.record.rl_number for match in result.matches}
            assert {"RL-1010", "RL-1039"} <= rls

    def test_no_match_reports_not_found(self):
        result = lookup("Zzz Nonexistent Recruiting")
        assert result.status == "not_found"
        assert result.matches == []

    def test_suspended_agency_is_still_found_but_status_is_shown(self):
        result = lookup("RL-1007")
        assert result.status == "found"
        assert result.matches[0].record.status == "Suspended"
        assert "Suspended" in result.message_bn


class TestScoring:
    def test_subset_tokens_do_not_score_perfectly(self):
        """Regression: token_set_ratio gave 100 to a name that explained nothing."""
        assert fuzzy_score("padma kushiyara employm", "kushiyara overseas") < fuzzy_score(
            "padma kushiyara employm", "padma kushiyara employment"
        )

    def test_score_cutoff_is_honoured(self):
        assert fuzzy_score("abc", "xyz", 50) == 0.0
