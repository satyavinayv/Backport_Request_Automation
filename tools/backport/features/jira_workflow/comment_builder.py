from config import MANAGER_NAME
from utils.dashboard_url import build_dashboard_url, shorten_dashboard_url


def build_jira_comment(
    original_mr_url,
    backport_mr_url,
    dev_checkin_jira,
    gm2_run_count,
    test_case_count,
    fix_version,
    tc_ids,
    bypass_gm2=False,
    bypass_reason=None,
    per_tc_bypasses=None,
    deleted_tc_ids=None,
    cbb_eligible_tcs=None,
    skip_gm2_no_ids=False,
):
    risk = "Low" if test_case_count <= 2 else ("Medium" if test_case_count <= 5 else "High")
    dev_checkin_answer = f"Yes - {dev_checkin_jira}" if dev_checkin_jira else "No"
    per_tc_bypasses = per_tc_bypasses or {}
    deleted_tc_ids = deleted_tc_ids or []
    cbb_eligible_tcs = cbb_eligible_tcs or {}

    if bypass_gm2:
        reason_text = bypass_reason or "Smoke failure / pipeline issue requires immediate backport fix"
        executed_answer = f"BYPASSED — {reason_text}"
        gm2_runs_section = (
            f"NOTE: GM2 eligibility check was bypassed for this backport.\n"
            f"Reason: {reason_text}\n"
            f"Affected test IDs: {', '.join(tc_ids) if tc_ids else 'N/A'}"
        )
    elif skip_gm2_no_ids:
        executed_answer = "SKIPPED — no feature file references found for changed files"
        gm2_runs_section = (
            "NOTE: GM2 check skipped. No .feature files referencing the changed files "
            "were found in the repository scope."
        )
    else:
        executed_answer = f"{gm2_run_count} times"
        special_count = sum(
            1 for tc in tc_ids
            if tc in per_tc_bypasses or tc in cbb_eligible_tcs
        )
        if special_count:
            executed_answer += f" ({special_count} test(s) with special eligibility — see GM2 Runs below)"

        gm2_links = []
        for tc_id in tc_ids:
            if tc_id in per_tc_bypasses:
                gm2_links.append(f"{tc_id}: [BYPASSED] — {per_tc_bypasses[tc_id]}")
            elif tc_id in cbb_eligible_tcs:
                gm2_links.append(f"{tc_id}: [ELIGIBLE via CBB] — Branch: {cbb_eligible_tcs[tc_id]}")
            else:
                path = build_dashboard_url(tc_id)
                short_url = shorten_dashboard_url(path)
                gm2_links.append(f"{tc_id}: {short_url}")

        if deleted_tc_ids:
            gm2_links.append("")
            gm2_links.append("Deleted tests (GM2 check skipped — test removed in this MR):")
            for dtc in deleted_tc_ids:
                gm2_links.append(f"  {dtc}: [SKIPPED — deleted]")

        gm2_runs_section = "\n".join(gm2_links)

    return f"""Original MR: {original_mr_url}
Backport MR: {backport_mr_url}

Q: Why check in this late? Why not before?
A: NA

Q: Is there a dev check-in that is driving this test change? If yes, what is the Jira?
A: {dev_checkin_answer}

Q: Have these tests been merged into develop?
A: Yes

Q: How many times have they been executed in develop?
A: {executed_answer}

Q: What is the risk of regression (Low/ High)?
A: {risk}

Q: How many tests are impacted?
A: {test_case_count}

Q: What release versions are you requesting this approval for (e.g.24R3.0, 24R3.1)?
A: {fix_version}

Q: Have the requesting release version(s) updated in Fix version/s field in Jira?
A: Yes

[~{MANAGER_NAME}] Kindly approve the backport request
GM2 Runs:
{gm2_runs_section}"""
