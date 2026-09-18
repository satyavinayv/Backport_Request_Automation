"""
Tests for features/jira_workflow/comment_builder.py

Covers:
  - Risk level thresholds (Low/Medium/High based on test_case_count)
  - dev_checkin_jira present / absent
  - GM2 run count shown correctly
  - Fix version shown correctly
  - Manager name appears in comment
  - GM2 links section contains each TC ID
  - Dashboard URL shortening called per TC ID
"""
import pytest
from unittest.mock import patch
from features.jira_workflow.comment_builder import build_jira_comment


FAKE_SHORT_URL = "https://autoinfra-es.vaultdev.com/_dashboards/goto/abc123"


def _build(**kwargs):
    defaults = dict(
        original_mr_url="https://gitlab/mr/1",
        backport_mr_url="https://gitlab/mr/2",
        dev_checkin_jira=None,
        gm2_run_count=5,
        test_case_count=1,
        fix_version="26R2.3",
        tc_ids=["TC-1234"],
    )
    defaults.update(kwargs)
    with patch("features.jira_workflow.comment_builder.build_dashboard_url", return_value="/some/path"), \
         patch("features.jira_workflow.comment_builder.shorten_dashboard_url", return_value=FAKE_SHORT_URL):
        return build_jira_comment(**defaults)


class TestBuildJiraComment:
    def test_low_risk_one_test(self):
        comment = _build(test_case_count=1)
        assert "Low" in comment

    def test_low_risk_two_tests(self):
        comment = _build(test_case_count=2)
        assert "Low" in comment

    def test_medium_risk_three_tests(self):
        comment = _build(test_case_count=3)
        assert "Medium" in comment

    def test_medium_risk_five_tests(self):
        comment = _build(test_case_count=5)
        assert "Medium" in comment

    def test_high_risk_six_tests(self):
        comment = _build(test_case_count=6)
        assert "High" in comment

    def test_dev_checkin_present(self):
        comment = _build(dev_checkin_jira="DEV-9876")
        assert "Yes - DEV-9876" in comment

    def test_dev_checkin_absent(self):
        comment = _build(dev_checkin_jira=None)
        assert "A: No" in comment

    def test_gm2_run_count_shown(self):
        comment = _build(gm2_run_count=12)
        assert "12 times" in comment

    def test_fix_version_shown(self):
        comment = _build(fix_version="26R2.2")
        assert "26R2.2" in comment

    def test_original_mr_url_shown(self):
        comment = _build(original_mr_url="https://gitlab/mr/original")
        assert "https://gitlab/mr/original" in comment

    def test_backport_mr_url_shown(self):
        comment = _build(backport_mr_url="https://gitlab/mr/backport")
        assert "https://gitlab/mr/backport" in comment

    def test_manager_name_in_comment(self):
        comment = _build()
        from config import MANAGER_NAME
        assert f"[~{MANAGER_NAME}]" in comment

    def test_each_tc_id_in_gm2_section(self):
        comment = _build(tc_ids=["TC-1111", "TC-2222"])
        assert "TC-1111" in comment
        assert "TC-2222" in comment

    def test_short_url_in_gm2_section(self):
        comment = _build(tc_ids=["TC-1234"])
        assert FAKE_SHORT_URL in comment

    def test_test_case_count_shown(self):
        comment = _build(test_case_count=3)
        assert "3" in comment

    def test_bypass_gm2_skips_run_count(self):
        comment = _build(bypass_gm2=True, gm2_run_count=7)
        assert "7 times" not in comment
        assert "BYPASSED" in comment

    def test_bypass_gm2_includes_reason(self):
        comment = _build(bypass_gm2=True, bypass_reason="Smoke failure blocking release pipeline")
        assert "Smoke failure blocking release pipeline" in comment

    def test_bypass_gm2_default_reason(self):
        comment = _build(bypass_gm2=True)
        assert "BYPASSED" in comment
        assert "Smoke failure" in comment or "pipeline" in comment

    def test_bypass_gm2_includes_tc_ids(self):
        comment = _build(bypass_gm2=True, tc_ids=["TC-9001", "DEV-1234"])
        assert "TC-9001" in comment
        assert "DEV-1234" in comment

    def test_bypass_gm2_no_dashboard_url_fetched(self):
        # When bypassing, shorten_dashboard_url should not be called
        with patch("features.jira_workflow.comment_builder.build_dashboard_url") as mock_build, \
             patch("features.jira_workflow.comment_builder.shorten_dashboard_url") as mock_shorten:
            build_jira_comment(
                original_mr_url="https://gitlab/mr/1",
                backport_mr_url="https://gitlab/mr/2",
                dev_checkin_jira=None,
                gm2_run_count=5,
                test_case_count=1,
                fix_version="26R2.3",
                tc_ids=["TC-1234"],
                bypass_gm2=True,
            )
            mock_build.assert_not_called()
            mock_shorten.assert_not_called()


class TestPerTcBypass:
    """Tests for selective (per-TC) GM2 bypass via per_tc_bypasses dict."""

    def test_bypassed_tc_shows_bypass_label(self):
        comment = _build(
            tc_ids=["TC-1234", "TC-5678"],
            per_tc_bypasses={"TC-1234": "Known flaky test"},
        )
        assert "[BYPASSED]" in comment
        assert "Known flaky test" in comment

    def test_non_bypassed_tc_shows_dashboard_url(self):
        comment = _build(
            tc_ids=["TC-1234", "TC-5678"],
            per_tc_bypasses={"TC-1234": "Known flaky test"},
        )
        assert FAKE_SHORT_URL in comment

    def test_bypassed_tc_has_no_dashboard_url(self):
        with patch("features.jira_workflow.comment_builder.build_dashboard_url") as mock_build, \
             patch("features.jira_workflow.comment_builder.shorten_dashboard_url", return_value=FAKE_SHORT_URL):
            build_jira_comment(
                original_mr_url="https://gitlab/mr/1",
                backport_mr_url="https://gitlab/mr/2",
                dev_checkin_jira=None,
                gm2_run_count=3,
                test_case_count=2,
                fix_version="26R2.3",
                tc_ids=["TC-1234", "TC-5678"],
                per_tc_bypasses={"TC-1234": "Known flaky test"},
            )
            # build_dashboard_url should only be called for TC-5678, not TC-1234
            called_ids = [call.args[0] for call in mock_build.call_args_list]
            assert "TC-5678" in called_ids
            assert "TC-1234" not in called_ids

    def test_run_count_notes_bypassed_count(self):
        comment = _build(
            tc_ids=["TC-1234", "TC-5678"],
            gm2_run_count=4,
            per_tc_bypasses={"TC-5678": "Environment issue"},
        )
        assert "4 times" in comment
        assert "1 test(s) selectively bypassed" in comment

    def test_all_tcs_bypassed_shows_all_bypass_labels(self):
        comment = _build(
            tc_ids=["TC-1111", "TC-2222"],
            per_tc_bypasses={"TC-1111": "Reason A", "TC-2222": "Reason B"},
        )
        assert "TC-1111" in comment
        assert "TC-2222" in comment
        assert "Reason A" in comment
        assert "Reason B" in comment
        assert comment.count("[BYPASSED]") == 2

    def test_empty_per_tc_bypasses_behaves_normally(self):
        comment = _build(tc_ids=["TC-9999"], per_tc_bypasses={})
        assert "[BYPASSED]" not in comment
        assert FAKE_SHORT_URL in comment

    def test_none_per_tc_bypasses_behaves_normally(self):
        comment = _build(tc_ids=["TC-9999"], per_tc_bypasses=None)
        assert "[BYPASSED]" not in comment
        assert FAKE_SHORT_URL in comment

    def test_per_tc_bypass_and_global_bypass_mutually_exclusive(self):
        # When bypass_gm2=True, per_tc_bypasses is ignored entirely
        comment = _build(
            bypass_gm2=True,
            bypass_reason="Pipeline broken",
            tc_ids=["TC-1234"],
            per_tc_bypasses={"TC-1234": "Should be ignored"},
        )
        assert "BYPASSED" in comment
        assert "Pipeline broken" in comment
        # The per-TC reason should not appear alongside global bypass text
        assert "Should be ignored" not in comment
