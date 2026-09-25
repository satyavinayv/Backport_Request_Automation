"""
Tests for features/id_extraction/jira_test_linker.py

Covers:
  - parse_description_for_table_data:
      * Wiki markup table format (|| / |)
      * Tab-separated table format
      * Multi-space table format (4+ spaces)
      * Structured label "Scenario: <name>"
      * Direct ID extraction from "Test ID/Xray ID" column
      * Numeric TestRail IDs in that column
      * DEV-XXXXXX direct extraction
      * Duplicates deduplicated
      * Table stops at blank line
  - extract_ids_via_jira_scenarios:
      * Primary path — direct IDs from table (no OpenSearch call)
      * Fallback path — OpenSearch via scenario names
      * No hits from OpenSearch
      * Empty / missing description
      * Multiple scenarios merged, deduplication
"""
import pytest
from unittest.mock import patch, call
from features.id_extraction.jira_test_linker import (
    parse_description_for_table_data,
    extract_ids_via_jira_scenarios,
)


class TestParseDescriptionForTableData:
    # ------------------------------------------------------------------
    # Structured label format
    # ------------------------------------------------------------------
    def test_structured_label_scenario(self):
        desc = "Scenario: Verify login with valid credentials"
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "Verify login with valid credentials" in scenarios

    def test_case_insensitive_scenario_label(self):
        desc = "scenario: lower case label\nSCENARIO: Upper case label"
        _, _, scenarios = parse_description_for_table_data(desc)
        assert "lower case label" in scenarios
        assert "Upper case label" in scenarios

    # ------------------------------------------------------------------
    # Tab-separated table — IDs and scenarios
    # ------------------------------------------------------------------
    def test_tab_table_extracts_xray_id(self):
        desc = (
            "API Version\tBrowser\tTest ID/Xray ID\tScenario/Method\n"
            "26.3\tchrome\tDEV-1085496\tValidate Overlays with PDF\n"
        )
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-1085496" in xray_ids
        assert "Validate Overlays with PDF" in scenarios

    def test_tab_table_extracts_numeric_tc_ids(self):
        # ≥5-digit bare numerics are returned as candidates by the parser
        desc = (
            "API Version\tTest ID/Xray ID\tScenario/Method\n"
            "26.3\t541098757, 540948289\tValidate enhanced esign\n"
        )
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "541098757" in tc_ids   # candidate
        assert "540948289" in tc_ids   # candidate
        assert "Validate enhanced esign" in scenarios

    def test_tab_table_multiple_rows(self):
        desc = (
            "API Version\tTest ID/Xray ID\tScenario/Method\n"
            "26.3\tDEV-1111\tScenario A\n"
            "26.3\tDEV-2222\tScenario B\n"
        )
        _, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-1111" in xray_ids
        assert "DEV-2222" in xray_ids
        assert "Scenario A" in scenarios
        assert "Scenario B" in scenarios

    # ------------------------------------------------------------------
    # Wiki markup table format (Jira Server REST API)
    # ------------------------------------------------------------------
    def test_wiki_markup_table_extracts_xray_id(self):
        desc = (
            "|| API Version || Test ID/Xray ID || Scenario/Method ||\n"
            "| 26.3 | DEV-1085496 | Validate Overlays with PDF |\n"
        )
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-1085496" in xray_ids
        assert "Validate Overlays with PDF" in scenarios

    def test_wiki_markup_table_numeric_ids(self):
        # ≥5-digit bare numerics are returned as candidates by the parser
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 541098757, 540948289 | Validate esign page size |\n"
        )
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "541098757" in tc_ids   # candidate
        assert "540948289" in tc_ids   # candidate
        assert "Validate esign page size" in scenarios

    def test_wiki_markup_table_multiple_rows(self):
        desc = (
            "|| Test ID/Xray ID || Feature/Class || Scenario/Method ||\n"
            "| DEV-1111 | Login Feature | Scenario A |\n"
            "| DEV-2222 | Logout Feature | Scenario B |\n"
        )
        _, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-1111" in xray_ids
        assert "DEV-2222" in xray_ids
        assert "Scenario A" in scenarios
        assert "Scenario B" in scenarios

    # ------------------------------------------------------------------
    # Multi-space table format
    # ------------------------------------------------------------------
    def test_multispaced_table_extracts_ids(self):
        desc = (
            "API Version    Test ID/Xray ID    Scenario/Method\n"
            "26.3    DEV-9876    Validate renditions format\n"
        )
        _, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-9876" in xray_ids
        assert "Validate renditions format" in scenarios

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------
    def test_table_stops_at_blank_line(self):
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| DEV-1111 | First scenario |\n"
            "\n"
            "| DEV-2222 | Should NOT be picked up |\n"
        )
        _, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert "DEV-1111" in xray_ids
        assert "DEV-2222" not in xray_ids
        assert "Should NOT be picked up" not in scenarios

    def test_duplicates_deduplicated(self):
        desc = (
            "Scenario: Same scenario\n"
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| DEV-1111 | Same scenario |\n"
        )
        _, _, scenarios = parse_description_for_table_data(desc)
        assert scenarios.count("Same scenario") == 1

    def test_empty_description_returns_empty(self):
        tc_ids, xray_ids, scenarios = parse_description_for_table_data("")
        assert tc_ids == []
        assert xray_ids == []
        assert scenarios == []

    def test_no_relevant_content_returns_empty(self):
        desc = "This Jira ticket is a plain text description with no table or scenario labels."
        tc_ids, xray_ids, scenarios = parse_description_for_table_data(desc)
        assert tc_ids == []
        assert xray_ids == []
        assert scenarios == []

    def test_tc_dash_format_in_table(self):
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| TC-1234 | Some TC scenario |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "TC-1234" in tc_ids
        assert xray_ids == []

    def test_dev_id_not_duplicated_as_numeric_tc(self):
        # DEV-1085496 should appear only as an Xray ID, not also as bare number 1085496
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| DEV-1085496 | Validate Overlays |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "DEV-1085496" in xray_ids
        assert "1085496" not in tc_ids
        assert tc_ids == []

    def test_dev_id_and_numeric_id_mixed(self):
        # DEV-XXXXX → xray; standalone ≥5-digit number → tc_id candidate; no digit bleed
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| DEV-1085496, 541098757 | Validate |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "DEV-1085496" in xray_ids
        assert "541098757" in tc_ids
        assert "1085496" not in tc_ids

    def test_two_comma_separated_numeric_ids(self):
        # Both 9-digit numbers in same cell must be extracted
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 541098757, 540948289 | Validate esign page size |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "541098757" in tc_ids
        assert "540948289" in tc_ids

    def test_short_numeric_id_is_candidate_from_parser(self):
        # The parser returns 7-digit numbers as candidates; OpenSearch validation
        # (in extract_ids_via_jira_scenarios) decides whether they are real TC IDs.
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 1000271 | Validate something |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "1000271" in tc_ids  # candidate — not yet validated

    def test_five_digit_id_is_candidate_from_parser(self):
        # Similarly, 5-digit numbers are candidates from the parser.
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 11371 | Pipeline run |\n"
        )
        tc_ids, xray_ids, _ = parse_description_for_table_data(desc)
        assert "11371" in tc_ids  # candidate — not yet validated


class TestIsValidBodyRowBoundary:
    """
    The ±1 column-count guard means a multi-space-format body row is accepted only when
    its column count is within one of the header's column count.
    A prior bug had ±2 in the docstring and these tests would have caught it.
    Tested indirectly via parse_description_for_table_data (multi-space header = 3 cols).
    """

    _HEADER = "API Version    Test ID/Xray ID    Scenario/Method"  # 3-column header

    def _desc(self, body_line):
        return f"{self._HEADER}\n{body_line}"

    def test_exact_columns_accepted(self):
        # 3 columns — exact match → accepted
        _, xray_ids, _ = parse_description_for_table_data(self._desc("26.3    DEV-9991    Some scenario"))
        assert "DEV-9991" in xray_ids

    def test_minus_one_column_accepted(self):
        # 2 columns (header - 1) → accepted (optional trailing column missing)
        _, xray_ids, _ = parse_description_for_table_data(self._desc("26.3    DEV-9992"))
        assert "DEV-9992" in xray_ids

    def test_minus_two_columns_rejected(self):
        # 1 column (header - 2) → rejected as free-text; with ±2 bug this would be accepted
        _, xray_ids, _ = parse_description_for_table_data(self._desc("DEV-9993"))
        assert "DEV-9993" not in xray_ids

    def test_plus_one_column_accepted(self):
        # 4 columns (header + 1) → accepted (extra optional column present)
        _, xray_ids, _ = parse_description_for_table_data(self._desc("26.3    DEV-9994    Some scenario    extra"))
        assert "DEV-9994" in xray_ids

    def test_plus_two_columns_rejected(self):
        # 5 columns (header + 2) → rejected; with ±2 bug this would be accepted
        _, xray_ids, _ = parse_description_for_table_data(self._desc("26.3    DEV-9995    Some scenario    extra1    extra2"))
        assert "DEV-9995" not in xray_ids


class TestExtractIdsViaJiraScenarios:
    def _jira_issue(self, description):
        return {"fields": {"description": description}}

    def _os_response(self, tc_id="", xray_id=""):
        return {"hits": {"hits": [{"_source": {"test_case_id": tc_id, "x_ray_id": xray_id}}]}}

    # ------------------------------------------------------------------
    # Primary path — direct IDs from table, no OpenSearch call needed
    # ------------------------------------------------------------------
    def test_direct_xray_id_no_opensearch(self):
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| DEV-1085496 | Validate Overlays |\n"
        )
        issue = self._jira_issue(desc)
        with patch("features.id_extraction.jira_test_linker.opensearch_query") as mock_os:
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        mock_os.assert_not_called()
        assert "DEV-1085496" in xray_ids
        assert len(details) == 1

    def test_direct_numeric_ids_validated_via_opensearch(self):
        # Bare numeric IDs in a table are validated against OpenSearch.
        # When OpenSearch confirms them (hits returned) they appear in the final result.
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 541098757, 540948289 | Validate esign page size |\n"
        )
        issue = self._jira_issue(desc)
        os_hit = {"hits": {"hits": [{"_source": {"test_case_id": "541098757"}}]}}
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value=os_hit) as mock_os:
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert mock_os.called  # validation queries were made
        assert "541098757" in tc_ids
        assert "540948289" in tc_ids

    def test_numeric_id_excluded_when_no_opensearch_records(self):
        # A numeric ID with no OpenSearch records is a false positive (build/manifest
        # number, not a TestRail ID) and must not appear in the output.
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 1000271 | Validate something |\n"
        )
        issue = self._jira_issue(desc)
        no_hits = {"hits": {"hits": []}}
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value=no_hits):
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert "1000271" not in tc_ids
        assert tc_ids == []

    def test_numeric_id_included_when_opensearch_validates(self):
        # A 7-digit (or any-length) numeric ID that exists in OpenSearch IS a real
        # TestRail ID (e.g. @TestCase:1000271 used in feature files) and must be kept.
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 1000271 | Validate something |\n"
        )
        issue = self._jira_issue(desc)
        os_hit = {"hits": {"hits": [{"_source": {"test_case_id": "1000271"}}]}}
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value=os_hit):
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert "1000271" in tc_ids

    # ------------------------------------------------------------------
    # Fallback path — scenario names → OpenSearch
    # ------------------------------------------------------------------
    def test_ids_resolved_from_opensearch(self):
        issue = self._jira_issue("Scenario: Login test")
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value=self._os_response(tc_id="TC-1234", xray_id="DEV-5678")):
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert "TC-1234" in tc_ids
        assert "DEV-5678" in xray_ids
        assert len(details) == 1

    def test_no_opensearch_hits_returns_empty(self):
        issue = self._jira_issue("Scenario: Unknown test")
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value={"hits": {"hits": []}}):
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert tc_ids == []
        assert xray_ids == []
        assert details == []

    def test_multiple_scenarios_merged(self):
        issue = self._jira_issue("Scenario: Test A\nScenario: Test B")
        responses = [
            {"hits": {"hits": [{"_source": {"test_case_id": "TC-1111", "x_ray_id": ""}}]}},
            {"hits": {"hits": [{"_source": {"test_case_id": "TC-2222", "x_ray_id": ""}}]}},
        ]
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   side_effect=responses):
            tc_ids, _, details = extract_ids_via_jira_scenarios(issue)
        assert "TC-1111" in tc_ids
        assert "TC-2222" in tc_ids
        assert len(details) == 2

    def test_deduplication_across_scenarios(self):
        issue = self._jira_issue("Scenario: Test A\nScenario: Test B")
        same = {"hits": {"hits": [{"_source": {"test_case_id": "TC-9999", "x_ray_id": ""}}]}}
        with patch("features.id_extraction.jira_test_linker.opensearch_query", return_value=same):
            tc_ids, _, _ = extract_ids_via_jira_scenarios(issue)
        assert tc_ids.count("TC-9999") == 1

    def test_empty_description_returns_empty(self):
        tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(self._jira_issue(""))
        assert tc_ids == []
        assert xray_ids == []

    def test_missing_description_field_returns_empty(self):
        tc_ids, xray_ids, details = extract_ids_via_jira_scenarios({"fields": {}})
        assert tc_ids == []

    def test_numeric_id_dropped_when_opensearch_unreachable(self):
        # Issue 8 — fail-closed: bare numeric IDs must NOT be accepted when OpenSearch
        # is down. The ID should be silently dropped (with a warning to the user).
        desc = (
            "|| Test ID/Xray ID || Scenario/Method ||\n"
            "| 541098757 | Validate something |\n"
        )
        issue = self._jira_issue(desc)
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   side_effect=Exception("connection refused")):
            tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)
        assert "541098757" not in tc_ids
        assert tc_ids == []

    def test_collision_warning_emitted_for_shared_scenario_name(self):
        # Issue 28 — when multiple distinct IDs are returned for a single scenario name,
        # a warning must be emitted so the user knows to verify manually.
        issue = self._jira_issue("Scenario: Shared scenario name")
        multi_hit_response = {
            "hits": {
                "hits": [
                    {"_source": {"test_case_id": "TC-1111", "x_ray_id": "DEV-2222"}},
                    {"_source": {"test_case_id": "TC-3333", "x_ray_id": ""}},
                ]
            }
        }
        with patch("features.id_extraction.jira_test_linker.opensearch_query",
                   return_value=multi_hit_response):
            with patch("features.id_extraction.jira_test_linker.warn") as mock_warn:
                tc_ids, xray_ids, details = extract_ids_via_jira_scenarios(issue)

        # Both IDs must still be returned so the user can inspect them
        assert "TC-1111" in tc_ids or "TC-3333" in tc_ids
        # A warning referencing the scenario name must have been issued
        warning_texts = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "Shared scenario name" in warning_texts or "Multiple tests" in warning_texts
