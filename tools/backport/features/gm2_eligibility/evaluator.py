def evaluate_eligibility(tc_id, runs):
    """Eligible only when the latest two executions are both Passed."""
    if len(runs) < 2:
        pattern = " -> ".join(r["test_result"] for r in runs) or "(no runs)"
        return False, "Insufficient GM2 runs (need at least 2 post-merge)", pattern

    results = [r["test_result"].strip().lower() for r in runs]
    pattern_str = " -> ".join(r["test_result"] for r in runs)

    last_two = results[-2:]
    if last_two[0] == "passed" and last_two[1] == "passed":
        return True, "Latest two runs are PASS", pattern_str
    else:
        return False, f"Latest two runs are not both PASS: {' -> '.join(last_two)}", pattern_str
