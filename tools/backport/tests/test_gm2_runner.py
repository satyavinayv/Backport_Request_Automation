"""
Tests for features/gm2_eligibility/runner.py

Covers:
  - rerun_count != 0 filtered out
  - isCBB = True filtered out (bool and string variants)
  - Valid runs included
  - Results returned oldest→newest (reversed from desc sort)
  - Empty hits returns empty list
  - rerun_count as non-numeric string handled gracefully
"""
import pytest
from unittest.mock import patch
from features.gm2_eligibility.runner import fetch_gm2_runs


def _hit(test_result, rerun_count=0, is_cbb=False, timestamp="2026-01-01T00:00:00Z"):
    return {
        "_source": {
            "@timestamp": timestamp,
            "test_result": test_result,
            "vault_version": "26.3",
            "test_case_id": "TC-1234",
            "x_ray_id": "DEV-5678",
            "rerun_count": rerun_count,
            "isCBB": is_cbb,
            "error_message": "",
            "environment": "GM2",
        }
    }


class TestFetchGm2Runs:
    def _mock_query(self, hits):
        return {"hits": {"hits": hits}}

    def test_valid_run_included(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([_hit("Passed")])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert len(runs) == 1
        assert runs[0]["test_result"] == "Passed"

    def test_rerun_nonzero_filtered(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([_hit("Passed", rerun_count=1)])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert runs == []

    def test_is_cbb_true_filtered(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([_hit("Passed", is_cbb=True)])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert runs == []

    def test_is_cbb_string_true_filtered(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([_hit("Passed", is_cbb="true")])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert runs == []

    def test_is_cbb_string_false_kept(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([_hit("Passed", is_cbb="false")])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert len(runs) == 1

    def test_rerun_count_string_zero_kept(self):
        hit = _hit("Passed")
        hit["_source"]["rerun_count"] = "0"
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([hit])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert len(runs) == 1

    def test_rerun_count_non_numeric_treated_as_zero(self):
        hit = _hit("Passed")
        hit["_source"]["rerun_count"] = "N/A"
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([hit])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert len(runs) == 1

    def test_empty_hits_returns_empty(self):
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query([])):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert runs == []

    def test_results_reversed_oldest_first(self):
        # API returns desc (newest first): t3, t2, t1 — after reverse: t1, t2, t3
        hits = [
            _hit("Failed", timestamp="2026-01-03T00:00:00Z"),
            _hit("Passed", timestamp="2026-01-02T00:00:00Z"),
            _hit("Passed", timestamp="2026-01-01T00:00:00Z"),
        ]
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query(hits)):
            runs = fetch_gm2_runs("TC-1234", "2025-12-31T00:00:00Z")
        # After reverse: oldest first → [t1=Passed, t2=Passed, t3=Failed]
        assert runs[0]["timestamp"] == "2026-01-01T00:00:00Z"
        assert runs[-1]["timestamp"] == "2026-01-03T00:00:00Z"

    def test_mixed_valid_and_filtered(self):
        hits = [
            _hit("Passed", rerun_count=0),
            _hit("Passed", rerun_count=2),   # filtered
            _hit("Failed", is_cbb=True),      # filtered
        ]
        with patch("features.gm2_eligibility.runner.opensearch_query",
                   return_value=self._mock_query(hits)):
            runs = fetch_gm2_runs("TC-1234", "2026-01-01T00:00:00Z")
        assert len(runs) == 1
