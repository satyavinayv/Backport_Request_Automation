"""
Tests for features/backport/mr_ops.py

Covers:
  - build_label_set: always includes "backport", merges all sources, deduplicates
  - check_existing_backport_mr: found / not found cases
  - create_mr: minimal payload, with assignee, with reviewer_ids, with labels,
    labels as list vs string
"""
import pytest
from unittest.mock import patch, MagicMock
from features.backport.mr_ops import build_label_set, check_existing_backport_mr, create_mr


class TestBuildLabelSet:
    def test_backport_always_added(self):
        result = build_label_set([], "", "")
        assert "backport" in result

    def test_original_labels_included(self):
        result = build_label_set(["team-qa", "automation"], "", "")
        assert "team-qa" in result
        assert "automation" in result

    def test_env_labels_included(self):
        result = build_label_set([], "env-label1,env-label2", "")
        assert "env-label1" in result
        assert "env-label2" in result

    def test_cli_labels_included(self):
        result = build_label_set([], "", "cli-label")
        assert "cli-label" in result

    def test_all_sources_merged(self):
        result = build_label_set(["orig"], "env-lbl", "cli-lbl")
        assert "orig" in result
        assert "env-lbl" in result
        assert "cli-lbl" in result
        assert "backport" in result

    def test_duplicates_deduplicated(self):
        result = build_label_set(["backport", "team-qa"], "team-qa", "backport")
        labels = result.split(",")
        assert labels.count("backport") == 1
        assert labels.count("team-qa") == 1

    def test_whitespace_in_env_labels_trimmed(self):
        result = build_label_set([], " spaced , labels ", "")
        assert "spaced" in result
        assert "labels" in result

    def test_empty_strings_not_included(self):
        result = build_label_set([], ",,", "")
        labels = [l for l in result.split(",") if l]
        assert all(l.strip() for l in labels)

    def test_scba_approved_excluded(self):
        result = build_label_set(["SCBA Approved 🤿", "team-qa"], "", "")
        assert "SCBA Approved 🤿" not in result
        assert "team-qa" in result

    def test_peer_reviewed_excluded(self):
        result = build_label_set(["PeerReviewed", "team-qa"], "", "")
        assert "PeerReviewed" not in result
        assert "team-qa" in result

    def test_gm2_fixes_excluded(self):
        result = build_label_set(["GM2 Fixes", "backport"], "", "")
        assert "GM2 Fixes" not in result

    def test_excluded_labels_from_all_sources(self):
        result = build_label_set([], "SCBA Approved 🤿,env-lbl", "GM2 Fixes,cli-lbl")
        assert "SCBA Approved 🤿" not in result
        assert "GM2 Fixes" not in result
        assert "env-lbl" in result
        assert "cli-lbl" in result


class TestCheckExistingBackportMr:
    def test_found(self):
        fake_mr = {"source_branch": "r26.2.3_gm/user/QA-1_SU", "web_url": "https://gitlab/mr/1"}
        with patch("features.backport.mr_ops.gitlab_get", return_value=[fake_mr]):
            mr, state = check_existing_backport_mr("proj", "r26.2.3_gm/user/QA-1_SU", "release/26.2.3")
        assert mr == fake_mr
        assert state == "opened"

    def test_not_found_returns_none(self):
        fake_mr = {"source_branch": "some-other-branch", "web_url": "https://gitlab/mr/2"}
        with patch("features.backport.mr_ops.gitlab_get", return_value=[fake_mr]):
            mr, state = check_existing_backport_mr("proj", "r26.2.3_gm/user/QA-1_SU", "release/26.2.3")
        assert mr is None
        assert state is None

    def test_empty_mr_list_returns_none(self):
        with patch("features.backport.mr_ops.gitlab_get", return_value=[]):
            mr, state = check_existing_backport_mr("proj", "branch", "target")
        assert mr is None
        assert state is None

    def test_closed_mr_found(self):
        fake_mr = {"source_branch": "r26.2.3_gm/user/QA-1_SU", "web_url": "https://gitlab/mr/1"}

        def fake_get(path, params=None):
            # Return empty for 'opened', the MR for 'closed'
            if params and params.get("state") == "closed":
                return [fake_mr]
            return []

        with patch("features.backport.mr_ops.gitlab_get", side_effect=fake_get):
            mr, state = check_existing_backport_mr("proj", "r26.2.3_gm/user/QA-1_SU", "release/26.2.3")
        assert mr == fake_mr
        assert state == "closed"

    def test_pagination_match_on_second_page(self):
        """MR not in first 100 results must be found on page 2."""
        target_branch = "r26.2.3_gm/user/QA-1_SU"
        fake_mr = {"source_branch": target_branch, "web_url": "https://gitlab/mr/99"}
        page_1 = [{"source_branch": f"other-branch-{i}"} for i in range(100)]
        page_2 = [fake_mr]

        call_count = {"n": 0}

        def fake_get(path, params=None):
            call_count["n"] += 1
            state = params.get("state", "")
            page = params.get("page", 1)
            if state == "opened":
                if page == 1:
                    return page_1
                if page == 2:
                    return page_2
            return []

        with patch("features.backport.mr_ops.gitlab_get", side_effect=fake_get):
            mr, state = check_existing_backport_mr("proj", target_branch, "release/26.2.3")

        assert mr == fake_mr
        assert state == "opened"
        # Must have made at least 2 calls for the 'opened' state (page 1 and page 2)
        assert call_count["n"] >= 2


class TestCreateMr:
    def _call(self, **kwargs):
        defaults = dict(
            project_id_encoded="proj",
            source_branch="backport-branch",
            target_branch="release/26.2.3",
            title="Backport: QA-1 fix",
            description="Automated backport.",
        )
        defaults.update(kwargs)
        with patch("features.backport.mr_ops.gitlab_post", return_value={"web_url": "https://gl/mr/1"}) as mock_post:
            result = create_mr(**defaults)
        return result, mock_post

    def test_minimal_payload(self):
        _, mock_post = self._call()
        payload = mock_post.call_args[0][1]
        assert payload["source_branch"] == "backport-branch"
        assert payload["target_branch"] == "release/26.2.3"
        assert "assignee_id" not in payload
        assert "reviewer_ids" not in payload
        assert "labels" not in payload

    def test_assignee_included_when_provided(self):
        _, mock_post = self._call(assignee_id=42)
        payload = mock_post.call_args[0][1]
        assert payload["assignee_id"] == 42

    def test_reviewer_ids_included_when_provided(self):
        _, mock_post = self._call(reviewer_ids=[10, 20])
        payload = mock_post.call_args[0][1]
        assert payload["reviewer_ids"] == [10, 20]

    def test_labels_string_passed_through(self):
        _, mock_post = self._call(labels="backport,team-qa")
        payload = mock_post.call_args[0][1]
        assert payload["labels"] == "backport,team-qa"

    def test_labels_list_joined(self):
        _, mock_post = self._call(labels=["backport", "team-qa"])
        payload = mock_post.call_args[0][1]
        assert payload["labels"] == "backport,team-qa"

    def test_none_optional_params_not_in_payload(self):
        _, mock_post = self._call(assignee_id=None, reviewer_ids=None, labels=None)
        payload = mock_post.call_args[0][1]
        assert "assignee_id" not in payload
        assert "reviewer_ids" not in payload
        assert "labels" not in payload
