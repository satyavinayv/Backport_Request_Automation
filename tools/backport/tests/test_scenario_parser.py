"""
Tests for features/id_extraction/scenario_parser.py

Covers:
  - extract_scenario_block_ids: normal block, multiple scenarios (only modified
    block returned), scenario at line index 0 (bug fix), no scenario header,
    Examples table IDs, duplicate block deduplication,
    Background modification → all scenarios returned
  - scan_scenario_blocks: file type filtering, no modified lines skipped
"""
import pytest
from unittest.mock import patch
from features.id_extraction.scenario_parser import (
    extract_scenario_block_ids,
    scan_scenario_blocks,
    _find_all_scenario_blocks,
)


FEATURE_CONTENT = """\
Feature: Login

  @TestCase: TC-0001
  Scenario: Successful login
    Given I am on login page
    When I enter valid credentials
    Then I am logged in

  @TestCase: TC-0002
  Scenario: Failed login
    Given I am on login page
    When I enter invalid credentials
    Then I see an error
"""


class TestExtractScenarioBlockIds:
    def test_ids_from_tag_on_modified_line(self):
        # Modifying line 6 (inside Scenario 1) should extract TC-0001
        tc_ids, xray_ids, blocks = extract_scenario_block_ids(FEATURE_CONTENT, [6])
        assert "TC-0001" in tc_ids
        assert "TC-0002" not in tc_ids

    def test_second_scenario_block(self):
        # Modifying line 12 (inside Scenario 2) should extract TC-0002
        tc_ids, xray_ids, blocks = extract_scenario_block_ids(FEATURE_CONTENT, [12])
        assert "TC-0002" in tc_ids
        assert "TC-0001" not in tc_ids

    def test_no_ids_returns_empty(self):
        content = "Feature: Empty\n\n  Scenario: No IDs here\n    Given nothing\n"
        tc_ids, xray_ids, blocks = extract_scenario_block_ids(content, [3])
        assert tc_ids == []
        assert xray_ids == []
        assert blocks == []

    def test_scenario_at_line_index_zero(self):
        # Bug fix: loop was `> 0` — first line of file was never checked
        content = "@TestCase: TC-FIRST\nScenario: At top\n  Given something\n"
        tc_ids, _, _ = extract_scenario_block_ids(content, [3])
        assert "TC-FIRST" in tc_ids

    def test_xray_tag_extracted(self):
        content = (
            "Feature: X\n\n"
            "  @Xray: DEV-9999\n"
            "  Scenario: Has xray\n"
            "    Given step\n"
        )
        _, xray_ids, _ = extract_scenario_block_ids(content, [5])
        assert "DEV-9999" in xray_ids

    def test_duplicate_modified_lines_same_block_not_double_counted(self):
        # Lines 5 and 6 are both in Scenario 1
        tc_ids, _, blocks = extract_scenario_block_ids(FEATURE_CONTENT, [5, 6])
        assert tc_ids.count("TC-0001") == 1
        assert len(blocks) == 1

    def test_multi_id_annotation(self):
        content = (
            "  @TestCase: TC-1111, TC-2222\n"
            "  Scenario: Multi ID\n"
            "    Given step\n"
        )
        tc_ids, _, _ = extract_scenario_block_ids(content, [3])
        assert "TC-1111" in tc_ids
        assert "TC-2222" in tc_ids

    def test_examples_table_id_in_block(self):
        content = (
            "  @Xray: DEV-5555\n"
            "  Scenario Outline: Parameterised\n"
            "    Given <param>\n"
            "  Examples:\n"
            "    | param   |\n"
            "    | value1  |\n"
        )
        _, xray_ids, _ = extract_scenario_block_ids(content, [3])
        assert "DEV-5555" in xray_ids

    def test_out_of_range_line_ignored(self):
        tc_ids, _, _ = extract_scenario_block_ids(FEATURE_CONTENT, [9999])
        assert tc_ids == []


class TestScanScenarioBlocks:
    def _make_change(self, path, diff="@@ -1,1 +1,1 @@\n+added line\n"):
        return {"new_path": path, "diff": diff}

    def test_non_test_files_skipped(self):
        changes = [self._make_change("config.xml"), self._make_change("data.json")]
        with patch("features.id_extraction.scenario_parser.fetch_raw_file_content") as mock_fetch:
            tc_ids, xray_ids, _ = scan_scenario_blocks("proj", changes, "abc123")
        mock_fetch.assert_not_called()
        assert tc_ids == []

    def test_feature_file_processed(self):
        # diff marks line 3 (the step inside the scenario body) as modified
        content = "@TestCase: TC-7777\nScenario: Test\n  Given step\n"
        diff = "@@ -1,3 +1,3 @@\n @TestCase: TC-7777\n Scenario: Test\n+  Given step modified\n"
        changes = [self._make_change("tests/Login.feature", diff)]
        with patch("features.id_extraction.scenario_parser.fetch_raw_file_content", return_value=content):
            tc_ids, _, _ = scan_scenario_blocks("proj", changes, "abc123")
        assert "TC-7777" in tc_ids

    def test_java_file_processed(self):
        # diff marks line 3 (the step inside the scenario body) as modified
        content = "@TestCase: TC-8888\nScenario: Java test\n  Given step\n"
        diff = "@@ -1,3 +1,3 @@\n @TestCase: TC-8888\n Scenario: Java test\n+  Given step modified\n"
        changes = [self._make_change("src/LoginTest.java", diff)]
        with patch("features.id_extraction.scenario_parser.fetch_raw_file_content", return_value=content):
            tc_ids, _, _ = scan_scenario_blocks("proj", changes, "abc123")
        assert "TC-8888" in tc_ids

    def test_empty_content_skipped(self):
        changes = [self._make_change("tests/Empty.feature")]
        with patch("features.id_extraction.scenario_parser.fetch_raw_file_content", return_value=""):
            tc_ids, xray_ids, _ = scan_scenario_blocks("proj", changes, "abc123")
        assert tc_ids == []

    def test_no_modified_lines_skipped(self):
        # diff with no +lines means parse_diff_modified_lines returns []
        changes = [{"new_path": "tests/A.feature", "diff": "@@ -1,1 +1,0 @@\n-removed\n"}]
        with patch("features.id_extraction.scenario_parser.fetch_raw_file_content") as mock_fetch:
            scan_scenario_blocks("proj", changes, "abc123")
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Background block modification → all scenarios affected
# ---------------------------------------------------------------------------

BACKGROUND_FEATURE = """\
Feature: Shared setup

  Background:
    Given I am logged in
    And the database is clean

  @TestCase: TC-AAA
  Scenario: First action
    When I do something
    Then result A

  @TestCase: TC-BBB
  Scenario: Second action
    When I do something else
    Then result B

  @Xray: DEV-9999
  Scenario Outline: Parameterised
    When I use <param>
    Then result C
  Examples:
    | param |
    | x     |
"""


class TestBackgroundModification:
    def test_background_change_returns_all_scenario_ids(self):
        # Line 4 is "Given I am logged in" inside Background
        tc_ids, xray_ids, blocks = extract_scenario_block_ids(BACKGROUND_FEATURE, [4])
        assert "TC-AAA" in tc_ids
        assert "TC-BBB" in tc_ids
        assert "DEV-9999" in xray_ids

    def test_background_change_all_blocks_returned(self):
        # All 3 scenario blocks should appear in matched_blocks
        _, _, blocks = extract_scenario_block_ids(BACKGROUND_FEATURE, [4])
        assert len(blocks) == 3

    def test_background_second_step_also_triggers_all(self):
        # Line 5 ("And the database is clean") is also in Background
        tc_ids, _, _ = extract_scenario_block_ids(BACKGROUND_FEATURE, [5])
        assert "TC-AAA" in tc_ids
        assert "TC-BBB" in tc_ids

    def test_background_plus_scenario_change_no_duplicates(self):
        # Modifying both a Background line and a line inside Scenario 1:
        # TC-AAA must appear exactly once even though it is found via both paths.
        tc_ids, _, blocks = extract_scenario_block_ids(BACKGROUND_FEATURE, [4, 9])
        assert tc_ids.count("TC-AAA") == 1
        assert "TC-BBB" in tc_ids

    def test_no_background_unmodified_scenarios_not_returned(self):
        # Modifying only a line inside Scenario 1 should NOT return TC-BBB
        tc_ids, _, _ = extract_scenario_block_ids(BACKGROUND_FEATURE, [9])
        assert "TC-AAA" in tc_ids
        assert "TC-BBB" not in tc_ids

    def test_background_no_scenarios_returns_empty(self):
        content = "Feature: Empty\n\n  Background:\n    Given nothing\n"
        tc_ids, xray_ids, blocks = extract_scenario_block_ids(content, [3])
        assert tc_ids == []
        assert xray_ids == []
        assert blocks == []


# ---------------------------------------------------------------------------
# _find_all_scenario_blocks helper
# ---------------------------------------------------------------------------

class TestFindAllScenarioBlocks:
    def test_returns_one_block_per_scenario(self):
        blocks = _find_all_scenario_blocks(BACKGROUND_FEATURE.splitlines())
        assert len(blocks) == 3

    def test_each_block_includes_tags(self):
        lines = BACKGROUND_FEATURE.splitlines()
        blocks = _find_all_scenario_blocks(lines)
        # First block start should be the @TestCase: TC-AAA tag line (index 6)
        first_start, _ = blocks[0]
        assert "@TestCase" in lines[first_start]

    def test_empty_file_returns_no_blocks(self):
        assert _find_all_scenario_blocks([]) == []

    def test_file_with_no_scenarios_returns_empty(self):
        lines = ["Feature: Nothing", "", "  Background:", "    Given step"]
        assert _find_all_scenario_blocks(lines) == []
