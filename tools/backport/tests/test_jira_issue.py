"""
Tests for features/jira_workflow/issue.py

Covers:
  - parse_fix_version: valid formats, invalid format
  - get_fix_versions: normal, empty list, missing field, None
  - get_caused_by_jira: "is caused by" link, "caused by" link, no such link,
    empty links, inward vs outward issue
"""
import pytest
from unittest.mock import patch
from features.jira_workflow.issue import parse_fix_version, get_fix_versions, get_caused_by_jira


class TestParseFixVersion:
    def test_standard_format(self):
        version_str, branch = parse_fix_version("26R2.3")
        assert version_str == "26.2.3"
        assert branch == "release/26.2.3"

    def test_24_format(self):
        version_str, branch = parse_fix_version("24R3.0")
        assert version_str == "24.3.0"
        assert branch == "release/24.3.0"

    def test_single_digit_parts(self):
        version_str, branch = parse_fix_version("1R1.1")
        assert version_str == "1.1.1"
        assert branch == "release/1.1.1"

    def test_invalid_format_raises_system_exit(self):
        with pytest.raises(SystemExit):
            parse_fix_version("invalid-version")

    def test_missing_patch_raises_system_exit(self):
        with pytest.raises(SystemExit):
            parse_fix_version("26R2")

    def test_plain_semver_raises_system_exit(self):
        with pytest.raises(SystemExit):
            parse_fix_version("26.2.3")


class TestGetFixVersions:
    def _issue(self, fix_versions):
        return {"key": "QA-1234", "fields": {"fixVersions": fix_versions}}

    def test_normal(self):
        issue = self._issue([{"name": "26R2.3"}, {"name": "26R2.2"}])
        names = get_fix_versions(issue)
        assert "26R2.3" in names
        assert "26R2.2" in names

    def test_empty_list_raises_system_exit(self):
        with pytest.raises(SystemExit):
            get_fix_versions(self._issue([]))

    def test_none_fix_versions_raises_system_exit(self):
        with pytest.raises(SystemExit):
            get_fix_versions(self._issue(None))

    def test_missing_fields_key_raises_system_exit(self):
        with pytest.raises(SystemExit):
            get_fix_versions({"key": "QA-1"})

    def test_version_without_name_field_skipped(self):
        issue = self._issue([{"id": "123"}, {"name": "26R2.3"}])
        names = get_fix_versions(issue)
        assert names == ["26R2.3"]


class TestGetCausedByJira:
    def _link(self, link_type_name, issue_key, direction="inward"):
        link = {
            "type": {"name": link_type_name},
        }
        link_issue = {"key": issue_key}
        if direction == "inward":
            link["inwardIssue"] = link_issue
        else:
            link["outwardIssue"] = link_issue
        return link

    def _issue(self, links):
        return {"fields": {"issuelinks": links}}

    def test_caused_by_link_returns_key(self):
        issue = self._issue([self._link("is caused by", "DEV-1234")])
        result = get_caused_by_jira(issue)
        assert result == "DEV-1234"

    def test_caused_by_outward_returns_key(self):
        issue = self._issue([self._link("caused by", "QA-5678", direction="outward")])
        result = get_caused_by_jira(issue)
        assert result == "QA-5678"

    def test_unrelated_link_returns_none(self):
        issue = self._issue([self._link("blocks", "DEV-9999")])
        result = get_caused_by_jira(issue)
        assert result is None

    def test_empty_links_returns_none(self):
        result = get_caused_by_jira(self._issue([]))
        assert result is None

    def test_no_issuelinks_field_returns_none(self):
        result = get_caused_by_jira({"fields": {}})
        assert result is None

    def test_non_dict_link_skipped(self):
        issue = self._issue(["not-a-dict"])
        result = get_caused_by_jira(issue)
        assert result is None
