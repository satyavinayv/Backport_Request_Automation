"""
Tests for features/gm2_eligibility/evaluator.py

Covers:
  - 0 runs → not eligible
  - 1 run → not eligible
  - 2 runs: pass+pass → eligible
  - 2 runs: fail+pass → not eligible
  - 2 runs: pass+fail → not eligible
  - 3 runs: last two pass → eligible (first can be fail)
  - 3 runs: last two not both pass → not eligible
  - Case insensitivity of "passed" string
  - pattern_str format
"""
import pytest
from features.gm2_eligibility.evaluator import evaluate_eligibility


def _run(result):
    return {"test_result": result}


class TestEvaluateEligibility:
    def test_no_runs_not_eligible(self):
        eligible, reason, pattern = evaluate_eligibility("TC-1", [])
        assert eligible is False
        assert "Insufficient" in reason
        assert pattern == "(no runs)"

    def test_one_run_not_eligible(self):
        eligible, reason, pattern = evaluate_eligibility("TC-1", [_run("Passed")])
        assert eligible is False
        assert "Insufficient" in reason

    def test_two_pass_eligible(self):
        eligible, reason, pattern = evaluate_eligibility("TC-1", [_run("Passed"), _run("Passed")])
        assert eligible is True
        assert "PASS" in reason

    def test_fail_pass_not_eligible(self):
        eligible, _, _ = evaluate_eligibility("TC-1", [_run("Failed"), _run("Passed")])
        assert eligible is False

    def test_pass_fail_not_eligible(self):
        eligible, _, _ = evaluate_eligibility("TC-1", [_run("Passed"), _run("Failed")])
        assert eligible is False

    def test_fail_fail_not_eligible(self):
        eligible, _, _ = evaluate_eligibility("TC-1", [_run("Failed"), _run("Failed")])
        assert eligible is False

    def test_three_runs_last_two_pass(self):
        runs = [_run("Failed"), _run("Passed"), _run("Passed")]
        eligible, _, _ = evaluate_eligibility("TC-1", runs)
        assert eligible is True

    def test_three_runs_last_two_not_both_pass(self):
        runs = [_run("Passed"), _run("Passed"), _run("Failed")]
        eligible, _, _ = evaluate_eligibility("TC-1", runs)
        assert eligible is False

    def test_many_runs_only_last_two_matter(self):
        runs = [_run("Failed")] * 8 + [_run("Passed"), _run("Passed")]
        eligible, _, _ = evaluate_eligibility("TC-1", runs)
        assert eligible is True

    def test_case_insensitive_passed(self):
        eligible, _, _ = evaluate_eligibility("TC-1", [_run("PASSED"), _run("passed")])
        assert eligible is True

    def test_pattern_str_format(self):
        runs = [_run("Failed"), _run("Passed"), _run("Passed")]
        _, _, pattern = evaluate_eligibility("TC-1", runs)
        assert pattern == "Failed -> Passed -> Passed"

    def test_one_run_pattern_shows_result(self):
        _, _, pattern = evaluate_eligibility("TC-1", [_run("Passed")])
        assert pattern == "Passed"
