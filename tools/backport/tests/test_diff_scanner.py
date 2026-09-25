"""
Tests for features/id_extraction/diff_scanner.py

Covers:
  - _extract_tc_ids: single, multi-value annotation, bare pattern, both together,
    underscore normalisation, case insensitivity, no-match
  - _extract_xray_ids: same dimensions for Xray patterns
  - scan_added_lines_for_ids: only +lines scanned, +++ skipped, - lines skipped,
    context lines skipped, table-row format, empty diff, multi-file
  - parse_diff_modified_lines: hunk header parsing, added/deleted/context line counting
"""
import pytest
from features.id_extraction.diff_scanner import (
    _extract_tc_ids,
    _extract_xray_ids,
    scan_added_lines_for_ids,
    parse_diff_modified_lines,
)


# ---------------------------------------------------------------------------
# _extract_tc_ids
# ---------------------------------------------------------------------------

class TestExtractTcIds:
    def test_single_annotation(self):
        assert _extract_tc_ids("@TestCase: TC-1234") == ["TC-1234"]

    def test_multi_value_annotation(self):
        result = _extract_tc_ids("@TestCase: TC-1234, TC-5678")
        assert "TC-1234" in result
        assert "TC-5678" in result

    def test_bare_pattern_dash(self):
        assert _extract_tc_ids("some text TC-9999 more") == ["TC-9999"]

    def test_bare_pattern_underscore_normalised(self):
        assert _extract_tc_ids("TC_4321") == ["TC-4321"]

    def test_annotation_and_bare_both_collected(self):
        # Bug fix: both patterns must run, not mutually exclusive
        result = _extract_tc_ids("@TestCase: TC-1111 and also TC-2222")
        assert "TC-1111" in result
        assert "TC-2222" in result

    def test_case_insensitive_annotation(self):
        assert _extract_tc_ids("@testcase: TC-0001") == ["TC-0001"]

    def test_case_insensitive_bare(self):
        assert _extract_tc_ids("tc-0002") == ["TC-0002"]

    def test_no_match_returns_empty(self):
        assert _extract_tc_ids("no test ids here") == []

    def test_deduplication(self):
        result = _extract_tc_ids("TC-1234 TC-1234 @TestCase: TC-1234")
        assert result.count("TC-1234") == 1

    def test_table_row_format(self):
        # Cucumber Examples table: | TC-9876 | some value |
        result = _extract_tc_ids("| TC-9876 | some_param |")
        assert "TC-9876" in result


# ---------------------------------------------------------------------------
# _extract_xray_ids
# ---------------------------------------------------------------------------

class TestExtractXrayIds:
    def test_xray_annotation(self):
        assert _extract_xray_ids("@Xray: DEV-1234") == ["DEV-1234"]

    def test_xrayid_annotation(self):
        assert _extract_xray_ids("@XrayID: XR-5678") == ["XR-5678"]

    def test_multi_value_annotation(self):
        result = _extract_xray_ids("@Xray: DEV-1234, DEV-5678")
        assert "DEV-1234" in result
        assert "DEV-5678" in result

    def test_bare_dev_pattern(self):
        assert _extract_xray_ids("DEV-123456") == ["DEV-123456"]

    def test_bare_xr_pattern(self):
        assert _extract_xray_ids("XR-9999") == ["XR-9999"]

    def test_underscore_normalised(self):
        assert _extract_xray_ids("DEV_111222") == ["DEV-111222"]

    def test_annotation_and_bare_both_collected(self):
        result = _extract_xray_ids("@Xray: DEV-0001 and bare DEV-0002")
        assert "DEV-0001" in result
        assert "DEV-0002" in result

    def test_no_match(self):
        assert _extract_xray_ids("nothing here") == []

    def test_deduplication(self):
        result = _extract_xray_ids("DEV-9999 DEV-9999 @Xray: DEV-9999")
        assert result.count("DEV-9999") == 1


# ---------------------------------------------------------------------------
# scan_added_lines_for_ids
# ---------------------------------------------------------------------------

class TestScanAddedLinesForIds:
    def _make_change(self, file_path, diff):
        return {"new_path": file_path, "diff": diff}

    def test_only_plus_lines_scanned(self):
        diff = (
            "@@  -1,3 +1,3 @@\n"
            "-@TestCase: TC-1111\n"    # removed line — must NOT be captured
            "+@TestCase: TC-2222\n"    # added line — must be captured
            " @TestCase: TC-3333\n"    # context line — must NOT be captured
        )
        tc_ids, xray_ids, details = scan_added_lines_for_ids([self._make_change("a.feature", diff)])
        assert "TC-2222" in tc_ids
        assert "TC-1111" not in tc_ids
        assert "TC-3333" not in tc_ids

    def test_triple_plus_header_skipped(self):
        diff = "+++ b/SomeFile.feature\n+@TestCase: TC-9999\n"
        tc_ids, _, _ = scan_added_lines_for_ids([self._make_change("b.feature", diff)])
        assert "TC-9999" in tc_ids  # only the +@TestCase line should match, not +++

    def test_empty_diff_returns_empty(self):
        tc_ids, xray_ids, details = scan_added_lines_for_ids([self._make_change("a.feature", "")])
        assert tc_ids == []
        assert xray_ids == []
        assert details == []

    def test_no_diff_key_returns_empty(self):
        tc_ids, xray_ids, _ = scan_added_lines_for_ids([{"new_path": "x.feature"}])
        assert tc_ids == []

    def test_multi_file(self):
        changes = [
            self._make_change("a.feature", "+@TestCase: TC-1111\n"),
            self._make_change("b.feature", "+@Xray: DEV-2222\n"),
        ]
        tc_ids, xray_ids, _ = scan_added_lines_for_ids(changes)
        assert "TC-1111" in tc_ids
        assert "DEV-2222" in xray_ids

    def test_table_row_id_captured(self):
        diff = "+| TC-7777 | value1 | value2 |\n"
        tc_ids, _, _ = scan_added_lines_for_ids([self._make_change("a.feature", diff)])
        assert "TC-7777" in tc_ids

    def test_match_detail_structure(self):
        diff = "+@TestCase: TC-4444\n"
        _, _, details = scan_added_lines_for_ids([self._make_change("some.feature", diff)])
        assert len(details) == 1
        assert details[0]["file"] == "some.feature"
        assert "TC-4444" in details[0]["tc_ids"]

    def test_non_test_file_skipped(self):
        # Files without a test extension (e.g. XML config) must never be scanned.
        diff = "+@TestCase: TC-9999\n"
        tc_ids, xray_ids, details = scan_added_lines_for_ids([self._make_change("config.xml", diff)])
        assert tc_ids == []
        assert xray_ids == []
        assert details == []

    def test_java_line_comment_filtered(self):
        # IDs on Java single-line comment lines must be ignored.
        diff = "+// @TestCase: TC-1111\n+@TestCase: TC-2222\n"
        tc_ids, _, _ = scan_added_lines_for_ids([self._make_change("LoginTest.java", diff)])
        assert "TC-1111" not in tc_ids
        assert "TC-2222" in tc_ids

    def test_groovy_block_comment_filtered(self):
        # IDs inside /* ... */ or * continuation lines must be ignored.
        diff = "+/* @Xray: DEV-3333\n+ * DEV-4444\n+@Xray: DEV-5555\n"
        _, xray_ids, _ = scan_added_lines_for_ids([self._make_change("Spec.groovy", diff)])
        assert "DEV-3333" not in xray_ids
        assert "DEV-4444" not in xray_ids
        assert "DEV-5555" in xray_ids


# ---------------------------------------------------------------------------
# parse_diff_modified_lines
# ---------------------------------------------------------------------------

class TestParseDiffModifiedLines:
    def test_basic_added_line(self):
        diff = "@@ -1,1 +1,2 @@\n context\n+added\n"
        lines = parse_diff_modified_lines(diff)
        assert 2 in lines  # context is line 1, added is line 2

    def test_deleted_line_does_not_advance_counter(self):
        diff = "@@ -1,2 +1,1 @@\n-removed\n context\n"
        lines = parse_diff_modified_lines(diff)
        assert lines == []  # nothing added

    def test_multiple_hunks(self):
        diff = (
            "@@ -1,1 +1,1 @@\n+first hunk add\n"
            "@@ -10,1 +10,1 @@\n+second hunk add\n"
        )
        lines = parse_diff_modified_lines(diff)
        assert 1 in lines
        assert 10 in lines

    def test_empty_diff(self):
        assert parse_diff_modified_lines("") == []
