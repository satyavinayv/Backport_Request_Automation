"""
Tests for features/id_extraction/__init__.py

Covers:
  - extract_jira_id: normal title, no ID, multiple patterns (first match)
  - extract_all_jira_ids: title only, description, multiple IDs, skip test-ID lines
  - extract_test_case_ids: TC only, Xray only, both, neither → returns empty
  - extract_ids_from_diff: Phase 1 finds IDs, Phase 2 triggered, both empty
  - resolve_mr_test_case_ids: diff path, description fallback path
"""
import pytest
from unittest.mock import patch, MagicMock
from features.id_extraction import (
    extract_jira_id,
    extract_all_jira_ids,
    extract_test_case_ids,
    extract_ids_from_diff,
    resolve_mr_test_case_ids,
)


class TestExtractAllJiraIds:
    def test_title_only(self):
        ids = extract_all_jira_ids("QA-559720 Fix renditions")
        assert ids == ["QA-559720"]

    def test_title_primary_first(self):
        ids = extract_all_jira_ids("QA-1111 Fix", "Also fixes QA-2222")
        assert ids[0] == "QA-1111"
        assert "QA-2222" in ids

    def test_multiple_ids_in_description(self):
        ids = extract_all_jira_ids("QA-1 Fix", "Related: QA-2, QA-3")
        assert "QA-1" in ids
        assert "QA-2" in ids
        assert "QA-3" in ids

    def test_xray_ids_line_skipped(self):
        ids = extract_all_jira_ids("QA-1111 Fix", "Xray IDs: DEV-9999, DEV-8888")
        assert "DEV-9999" not in ids
        assert "DEV-8888" not in ids
        assert "QA-1111" in ids

    def test_test_cases_line_skipped(self):
        ids = extract_all_jira_ids("QA-1111 Fix", "Test Cases: TC-1234")
        assert "TC-1234" not in ids
        assert "QA-1111" in ids

    def test_deduplication_across_title_and_description(self):
        ids = extract_all_jira_ids("QA-1111 Fix", "Related to QA-1111 and QA-2222")
        assert ids.count("QA-1111") == 1
        assert "QA-2222" in ids

    def test_no_ids_returns_empty(self):
        assert extract_all_jira_ids("Fix renditions", "No jira mentioned") == []

    def test_none_description_handled(self):
        ids = extract_all_jira_ids("QA-9999 Fix", None)
        assert ids == ["QA-9999"]


class TestExtractJiraId:
    def test_standard_title(self):
        assert extract_jira_id("QA-558547 Fix something") == "QA-558547"

    def test_id_at_end(self):
        assert extract_jira_id("Fix something QA-12345") == "QA-12345"

    def test_no_id_raises_system_exit(self):
        with pytest.raises(SystemExit):
            extract_jira_id("No jira id in this title")

    def test_first_id_returned_when_multiple(self):
        # Only the first match should be returned
        result = extract_jira_id("QA-1111 and QA-2222")
        assert result == "QA-1111"

    def test_different_project_prefix(self):
        assert extract_jira_id("DEV-9999 some fix") == "DEV-9999"


class TestExtractTestCaseIds:
    def test_tc_ids_only(self):
        desc = "Test Cases: TC-1234, TC-5678"
        tc_ids, xray_ids = extract_test_case_ids(desc)
        assert tc_ids == ["TC-1234", "TC-5678"]
        assert xray_ids == []

    def test_xray_ids_only(self):
        desc = "Xray IDs: DEV-1085503"
        tc_ids, xray_ids = extract_test_case_ids(desc)
        assert tc_ids == []
        assert xray_ids == ["DEV-1085503"]

    def test_both_present(self):
        desc = "Test Cases: TC-1\nXray IDs: DEV-2"
        tc_ids, xray_ids = extract_test_case_ids(desc)
        assert "TC-1" in tc_ids
        assert "DEV-2" in xray_ids

    def test_neither_returns_empty(self):
        tc_ids, xray_ids = extract_test_case_ids("No IDs here.")
        assert tc_ids == []
        assert xray_ids == []

    def test_empty_description_returns_empty(self):
        tc_ids, xray_ids = extract_test_case_ids("")
        assert tc_ids == []
        assert xray_ids == []

    def test_none_description_returns_empty(self):
        tc_ids, xray_ids = extract_test_case_ids(None)
        assert tc_ids == []
        assert xray_ids == []

    def test_case_insensitive_labels(self):
        desc = "test cases: TC-9\nxray ids: DEV-8"
        tc_ids, xray_ids = extract_test_case_ids(desc)
        assert "TC-9" in tc_ids
        assert "DEV-8" in xray_ids


class TestExtractIdsFromDiff:
    def _changes_response(self, diff_line):
        return {"changes": [{"new_path": "a.feature", "diff": diff_line}]}

    def test_phase1_finds_ids(self):
        changes = self._changes_response("+@TestCase: TC-7777\n")
        with patch("features.id_extraction.gitlab_get", return_value=changes):
            tc_ids, xray_ids, details = extract_ids_from_diff("proj", 1, head_sha="abc")
        assert "TC-7777" in tc_ids

    def test_phase2_triggered_when_phase1_empty(self):
        changes = self._changes_response("@@ -1,1 +1,1 @@\n context line\n")
        phase2_result = (["TC-8888"], [], [{"file": "a.feature", "line": "...", "tc_ids": ["TC-8888"], "xray_ids": []}])
        with patch("features.id_extraction.gitlab_get", return_value=changes), \
             patch("features.id_extraction.scan_scenario_blocks", return_value=phase2_result):
            tc_ids, _, _ = extract_ids_from_diff("proj", 1, head_sha="abc")
        assert "TC-8888" in tc_ids

    def test_phase2_not_triggered_without_head_sha(self):
        changes = self._changes_response("context line\n")
        with patch("features.id_extraction.gitlab_get", return_value=changes), \
             patch("features.id_extraction.scan_scenario_blocks") as mock_phase2:
            extract_ids_from_diff("proj", 1, head_sha=None)
        mock_phase2.assert_not_called()

    def test_gitlab_error_returns_empty(self):
        with patch("features.id_extraction.gitlab_get", side_effect=Exception("API error")):
            tc_ids, xray_ids, details = extract_ids_from_diff("proj", 1)
        assert tc_ids == []
        assert xray_ids == []


class TestResolveMrTestCaseIds:
    def test_diff_path_used_when_ids_found(self):
        with patch("features.id_extraction.extract_ids_from_diff",
                   return_value=(["TC-1"], ["DEV-2"], [{"file": "f", "line": "l", "tc_ids": [], "xray_ids": []}])):
            tc_ids, xray_ids, source, details = resolve_mr_test_case_ids("proj", 1, "desc", "sha")
        assert source == "diff"
        assert "TC-1" in tc_ids

    def test_description_fallback_when_diff_empty(self):
        with patch("features.id_extraction.extract_ids_from_diff", return_value=([], [], [])), \
             patch("features.id_extraction.extract_test_case_ids", return_value=(["TC-9"], ["DEV-9"])):
            tc_ids, xray_ids, source, details = resolve_mr_test_case_ids("proj", 1, "desc")
        assert source == "description"
        assert "TC-9" in tc_ids
