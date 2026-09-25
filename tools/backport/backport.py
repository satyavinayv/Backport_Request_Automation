#!/usr/bin/env python3
"""
backport.py - GitLab/Jira/GM2 Backport Automation

Usage:
    python backport.py --mr <MR_IID>                              # Phase 1: read-only validation
    python backport.py --mr <MR_IID> --inspect-ids               # Inspect modified test IDs
    python backport.py --mr <MR_IID> --execute                   # Phase 2: full execution
    python backport.py --mr <MR_IID> --execute --non-interactive # CI/CD headless execution
    python backport.py --mr <MR_IID> --execute \
        --reviewers john.doe jane.smith \
        --labels urgent,team-qa                                   # Custom reviewers and labels
    python backport.py --mr <MR_IID> --execute --bypass-gm2      # Skip GM2 check (pipeline/smoke issue)
    python backport.py --mr <MR_IID> --execute --bypass-gm2 \
        --bypass-gm2-reason "Smoke failure blocking release"      # Bypass with custom reason
    python backport.py --mr <MR_IID> --execute \
        --bypass-gm2-for TC-1234 DEV-5678                        # Bypass specific failing test IDs only
    python backport.py --mr <MR_IID> --execute \
        --bypass-gm2-for TC-1234 \
        --bypass-gm2-for-reason "Known flaky test, unrelated to fix"  # Bypass with reason
    python backport.py --mr 93015 93011 --execute                 # Combine two MRs into one backport

Environment Variables Required:
    GITLAB_TOKEN       - GitLab personal access token
    GITLAB_URL         - e.g. https://gitlab.veevadev.com
    JIRA_URL           - e.g. https://jira.veevadev.com
    JIRA_PAT           - Jira personal access token
    OPENSEARCH_URL     - e.g. https://autoinfra-es.vaultdev.com:9200

Optional Environment Variables:
    BACKPORT_REVIEWERS - Comma-separated GitLab usernames added as reviewers on every run
    BACKPORT_LABELS    - Comma-separated extra labels added on every run
"""
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import argparse
import os
import sys

from config import PROJECT_ID_ENCODED, JIRA_URL, BACKPORT_REVIEWERS, BACKPORT_LABELS
from utils.log import err, warn, info
from clients.gitlab import fetch_mr, fetch_mr_commits, get_current_user, resolve_usernames_to_ids
from clients.jira import jira_post_comment
from features.id_extraction import (
    extract_jira_id, extract_all_jira_ids, resolve_mr_test_case_ids,
    get_deleted_feature_ids, get_non_feature_changed_files,
)
from features.id_extraction.jira_test_linker import extract_ids_via_jira_scenarios
from features.id_extraction.repo_file_searcher import search_repo_for_file_usages
from features.gm2_eligibility.runner import fetch_gm2_runs, classify_gm2_absence, fetch_gm2_runs_cbb
from features.gm2_eligibility.evaluator import evaluate_eligibility, evaluate_eligibility_cbb
from clients.gitlab import fetch_mr_file_changes
from features.backport.branch_ops import create_backport_branch, cherry_pick_commit, delete_branch
from features.backport.mr_ops import check_existing_backport_mr, create_mr, build_label_set
from features.jira_workflow.issue import fetch_jira_issue, fetch_jira_issue_safe, get_fix_versions, get_caused_by_jira, parse_fix_version
from features.jira_workflow.transitions import get_jira_status, transition_jira_issue, ensure_transition_to_mr_to_gm
from features.jira_workflow.comment_builder import build_jira_comment


def _resolve_test_ids_for_mr(mr_iid, mr_title, mr_description, head_sha, jira_id, jira_issue):
    """
    Extract TC/Xray IDs for a single MR through all fallback phases (1-4).
    Also detects and returns TC IDs from deleted .feature files.

    Returns:
        tc_ids, xray_ids, id_source, diff_details, all_tc_ids,
        deleted_tc_ids, deleted_xray_ids, phase4_files_searched, skip_gm2_no_ids
    """
    # Fetch raw file changes once — used for both deleted-file detection and Phase 4
    changes = fetch_mr_file_changes(PROJECT_ID_ENCODED, mr_iid)
    deleted_tc_ids, deleted_xray_ids = get_deleted_feature_ids(changes)

    if deleted_tc_ids or deleted_xray_ids:
        info(
            f"  Detected deleted .feature file(s) in !{mr_iid}: "
            f"TC IDs {deleted_tc_ids or []}, Xray IDs {deleted_xray_ids or []} — GM2 check will be skipped for these."
        )

    tc_ids, xray_ids, id_source, diff_details = resolve_mr_test_case_ids(
        PROJECT_ID_ENCODED, mr_iid, mr_description, head_sha=head_sha, changes=changes
    )
    all_tc_ids = tc_ids + xray_ids
    phase4_files_searched = []
    skip_gm2_no_ids = False

    if not all_tc_ids:
        # Phase 3: Jira description scenario lookup
        all_jira_ids = extract_all_jira_ids(mr_title, mr_description)
        info(
            f"Phase 3: No IDs found via diffs or MR description for !{mr_iid}. "
            f"Searching {len(all_jira_ids)} Jira issue(s): {', '.join(all_jira_ids)}"
        )
        combined_tcs: set = set()
        combined_xrays: set = set()
        combined_details = []

        for jid in all_jira_ids:
            issue = jira_issue if jid == jira_id else fetch_jira_issue_safe(jid)
            if issue is None:
                warn(f"  {jid}: could not fetch — skipping.")
                continue
            tcs, xrays, detail = extract_ids_via_jira_scenarios(issue)
            if tcs or xrays:
                info(f"  {jid}: found {len(tcs)} TC ID(s) and {len(xrays)} Xray ID(s)")
            combined_tcs.update(tcs)
            combined_xrays.update(xrays)
            combined_details.extend(detail)

        tc_ids = sorted(combined_tcs)
        xray_ids = sorted(combined_xrays)
        diff_details = combined_details
        id_source = "jira-scenarios"
        all_tc_ids = tc_ids + xray_ids

        if all_tc_ids:
            warn(
                "Phase 3 IDs were sourced from Jira description table(s) only.\n"
                "  This covers failures explicitly listed in the linked Jira(s): "
                + ", ".join(all_jira_ids) + "\n"
                "  If the changed file is also used by test cases NOT mentioned in those Jira(s),\n"
                "  those tests are NOT included in this GM2 eligibility check — verify manually.\n"
                "  To ensure complete coverage, add all known IDs to the MR description:\n"
                "    Xray IDs: DEV-XXXXX, DEV-YYYYY"
            )

    if not all_tc_ids:
        # Phase 4: search repo for .feature files referencing the changed filenames
        non_feature_files = get_non_feature_changed_files(changes)
        if non_feature_files:
            info(f"Phase 4: Searching repo for feature file references to {len(non_feature_files)} changed file(s) ...")
            combined_tcs = set()
            combined_xrays = set()
            combined_details = []
            for fpath in non_feature_files:
                p4_tcs, p4_xrays, p4_details, p4_hits = search_repo_for_file_usages(
                    PROJECT_ID_ENCODED, fpath, ref="develop"
                )
                if p4_tcs or p4_xrays:
                    phase4_files_searched.append(
                        {"file": fpath, "hits": p4_hits, "tc_ids": p4_tcs, "xray_ids": p4_xrays}
                    )
                    combined_tcs.update(p4_tcs)
                    combined_xrays.update(p4_xrays)
                    combined_details.extend(p4_details)

            if combined_tcs or combined_xrays:
                tc_ids = sorted(combined_tcs)
                xray_ids = sorted(combined_xrays)
                diff_details = combined_details
                id_source = "repo-file-search"
                all_tc_ids = tc_ids + xray_ids
                warn(
                    "Phase 4 IDs sourced from feature files referencing the changed file(s).\n"
                    "  Coverage may be partial if the file is referenced outside the inferred scope.\n"
                    "  To ensure complete coverage, add all known IDs to the MR description:\n"
                    "    Xray IDs: DEV-XXXXX, DEV-YYYYY"
                )

    return (
        tc_ids, xray_ids, id_source, diff_details, all_tc_ids,
        deleted_tc_ids, deleted_xray_ids, phase4_files_searched, skip_gm2_no_ids,
    )


def main():
    parser = argparse.ArgumentParser(description="GitLab/Jira/GM2 Backport Automation")
    parser.add_argument("--mr", required=True, type=int, nargs="+", metavar="MR_IID",
                        help="One or more develop MR IIDs to combine into a single backport.")
    parser.add_argument(
        "--inspect-ids",
        action="store_true",
        help="Inspect and display modified Test Case/Xray IDs from MR diffs and exit.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Phase 2: create backport MR, post comment, and transition Jira issue",
    )
    parser.add_argument(
        "--target-branch",
        type=str,
        default=None,
        help="Override target release branch (e.g. release/26.2.2).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail fast on warnings instead of prompting for stdin input",
    )
    parser.add_argument(
        "--reviewers",
        nargs="+",
        metavar="USERNAME",
        default=[],
        help="GitLab usernames to add as reviewers on the backport MR (space-separated).",
    )
    parser.add_argument(
        "--labels",
        type=str,
        default="",
        help="Extra comma-separated labels to add on the backport MR.",
    )
    parser.add_argument(
        "--bypass-gm2",
        action="store_true",
        dest="bypass_gm2",
        help="Skip GM2 eligibility check (use for smoke failures or broken pipelines).",
    )
    parser.add_argument(
        "--bypass-gm2-reason",
        type=str,
        dest="bypass_gm2_reason",
        default="Smoke failure / pipeline issue requires immediate backport fix",
        help="Reason for bypassing GM2 eligibility (included in terminal output and Jira comment).",
    )
    parser.add_argument(
        "--bypass-gm2-for",
        nargs="+",
        metavar="TC_ID",
        dest="bypass_gm2_for",
        default=[],
        help="Bypass GM2 check for specific test case ID(s) only (e.g. TC-1234 DEV-5678). "
             "Other IDs still go through the normal eligibility check.",
    )
    parser.add_argument(
        "--bypass-gm2-for-reason",
        type=str,
        dest="bypass_gm2_for_reason",
        default="Known failure unrelated to this backport — user-confirmed bypass",
        help="Reason applied to every ID listed in --bypass-gm2-for.",
    )
    args = parser.parse_args()

    missing = [v for v in ["GITLAB_TOKEN", "JIRA_PAT"] if not os.environ.get(v)]
    if missing:
        err(f"Missing required environment variables: {', '.join(missing)}")

    # -------------------------------------------------------------------------
    # Step 1 & 2 & 3: Per-MR data collection
    # -------------------------------------------------------------------------
    mr_records = []

    for mr_iid in args.mr:
        mr = fetch_mr(PROJECT_ID_ENCODED, mr_iid)
        mr_title = mr.get("title", "")
        mr_description = mr.get("description", "") or ""
        mr_author = mr.get("author", {}).get("username", "unknown")
        mr_web_url = mr.get("web_url", "")
        merged_at = mr.get("merged_at")
        head_sha = mr.get("sha") or mr.get("diff_refs", {}).get("head_sha")
        original_mr_labels = mr.get("labels", [])

        jira_id = extract_jira_id(mr_title)

        jira_issue = fetch_jira_issue(jira_id)
        fix_versions = get_fix_versions(jira_issue)
        caused_by_jira = get_caused_by_jira(jira_issue)
        info(f"!{mr_iid} ({jira_id}) Fix Versions: {', '.join(fix_versions)}")

        (
            tc_ids, xray_ids, id_source, diff_details, all_tc_ids,
            mr_deleted_tc_ids, mr_deleted_xray_ids, phase4_files, skip_gm2_no_ids,
        ) = _resolve_test_ids_for_mr(mr_iid, mr_title, mr_description, head_sha, jira_id, jira_issue)

        if not all_tc_ids and not skip_gm2_no_ids:
            if not args.non_interactive and sys.stdin.isatty():
                warn(
                    f"No Test Case or Xray IDs found via any method for MR !{mr_iid}.\n"
                    "  Checked: code diffs, scenario blocks, MR description, Jira description, repo file search."
                )
                confirm = input("  Continue without GM2 check for this MR? (y/n): ").strip().lower()
                if confirm == "y":
                    skip_gm2_no_ids = True
                    warn("Proceeding without GM2 check — no test IDs found.")
                else:
                    err(
                        f"Aborted: No test IDs found for MR !{mr_iid}.\n"
                        "  Add test annotations (TC-XXXX, DEV-XXXXX) to the code or MR description."
                    )
            else:
                err(
                    f"No Test Case or Xray IDs found via any method for MR !{mr_iid}.\n"
                    "  Checked: code diffs, scenario blocks, MR description, Jira description, repo file search.\n"
                    "  Add test annotations (TC-XXXX, DEV-XXXXX) to the code or MR description."
                )

        mr_records.append({
            "iid": mr_iid,
            "mr": mr,
            "title": mr_title,
            "description": mr_description,
            "author": mr_author,
            "web_url": mr_web_url,
            "merged_at": merged_at,
            "head_sha": head_sha,
            "original_mr_labels": original_mr_labels,
            "jira_id": jira_id,
            "jira_issue": jira_issue,
            "fix_versions": fix_versions,
            "caused_by_jira": caused_by_jira,
            "tc_ids": tc_ids,
            "xray_ids": xray_ids,
            "id_source": id_source,
            "diff_details": diff_details,
            "deleted_tc_ids": mr_deleted_tc_ids,
            "deleted_xray_ids": mr_deleted_xray_ids,
            "skip_gm2_no_ids": skip_gm2_no_ids,
        })

    # -------------------------------------------------------------------------
    # Aggregate across all MRs
    # -------------------------------------------------------------------------
    multi = len(mr_records) > 1

    # Combined & deduplicated test IDs (preserve insertion order)
    seen_ids: set = set()
    combined_tc_ids = []
    combined_xray_ids = []
    for rec in mr_records:
        for tid in rec["tc_ids"]:
            if tid not in seen_ids:
                combined_tc_ids.append(tid)
                seen_ids.add(tid)
        for xid in rec["xray_ids"]:
            if xid not in seen_ids:
                combined_xray_ids.append(xid)
                seen_ids.add(xid)
    all_tc_ids = combined_tc_ids + combined_xray_ids

    # Aggregated labels (union across all source MRs)
    all_original_labels = list({lbl for rec in mr_records for lbl in rec["original_mr_labels"]})

    # Primary author = first MR's author (used for branch naming in dry-run)
    mr_author = mr_records[0]["author"]

    # Jira IDs list (in MR order, deduplicated)
    jira_ids = list(dict.fromkeys(rec["jira_id"] for rec in mr_records))

    # Fix versions: union from all Jira issues; warn if they differ
    all_fix_versions_sets = [set(rec["fix_versions"]) for rec in mr_records]
    combined_fix_versions = sorted(set.union(*all_fix_versions_sets))
    if multi and len(set(frozenset(s) for s in all_fix_versions_sets)) > 1:
        warn(
            "Fix versions differ across MRs — using union.\n"
            + "\n".join(f"  !{rec['iid']}: {', '.join(rec['fix_versions'])}" for rec in mr_records)
        )

    # caused_by_jira: first non-None value
    caused_by_jira = next((rec["caused_by_jira"] for rec in mr_records if rec["caused_by_jira"]), None)

    # Aggregate deleted TC IDs across all MRs (deduplicated)
    seen_deleted: set = set()
    combined_deleted_tc_ids = []
    for rec in mr_records:
        for dtc in rec.get("deleted_tc_ids", []) + rec.get("deleted_xray_ids", []):
            if dtc not in seen_deleted:
                combined_deleted_tc_ids.append(dtc)
                seen_deleted.add(dtc)

    # skip_gm2_no_ids: true if ANY MR has no IDs and user agreed to skip
    skip_gm2_no_ids = any(rec.get("skip_gm2_no_ids") for rec in mr_records)

    # -------------------------------------------------------------------------
    # --inspect-ids mode
    # -------------------------------------------------------------------------
    if args.inspect_ids:
        for rec in mr_records:
            print("\n" + "=" * 60)
            print(f"  TEST CASE INSPECTION REPORT FOR MR !{rec['iid']}")
            print("=" * 60)
            print(f"MR Title: {rec['title']}")
            print(f"Jira ID:  {rec['jira_id']}")
            print(f"Source:   {rec['id_source'].upper()}")
            if rec["diff_details"]:
                print("\nMatched Scenario Blocks / Lines:")
                print("-" * 60)
                for item in rec["diff_details"]:
                    print(f"File: {item['file']}")
                    print(f"  Line:   {item['line']}")
                    if item["tc_ids"]:
                        print(f"  TCs:    {', '.join(item['tc_ids'])}")
                    if item["xray_ids"]:
                        print(f"  Xrays:  {', '.join(item['xray_ids'])}")
                    print("-" * 60)
            print("\nExtracted Identifiers:")
            print(f"  Test Case IDs ({len(rec['tc_ids'])}): {', '.join(rec['tc_ids']) or 'None'}")
            print(f"  Xray IDs      ({len(rec['xray_ids'])}): {', '.join(rec['xray_ids']) or 'None'}")
        print("=" * 60 + "\n")
        sys.exit(0)

    # Validate all MRs are merged; use earliest merged_at for GM2 window
    merged_at_isos = []
    for rec in mr_records:
        if not rec["merged_at"]:
            err(f"MR !{rec['iid']} has not been merged yet.")
        iso = rec["merged_at"] if rec["merged_at"].endswith("Z") else rec["merged_at"] + "Z"
        merged_at_isos.append(iso)
    merged_at_iso = min(merged_at_isos)

    # -------------------------------------------------------------------------
    # Eligibility report header
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  BACKPORT ELIGIBILITY REPORT")
    print("=" * 60)
    for rec in mr_records:
        print(f"\nMR:            !{rec['iid']}")
        print(f"Title:         {rec['title']}")
        print(f"Author:        {rec['author']}")
        print(f"Merged At:     {rec['merged_at']}")
        print(f"Jira:          {rec['jira_id']}")
        print(f"ID Source:     {rec['id_source'].upper()}")
        print(f"Test Cases:    {', '.join(rec['tc_ids']) or 'none'}")
        print(f"Xray IDs:      {', '.join(rec['xray_ids']) or 'none'}")
    if multi:
        print(f"\nCombined Test IDs ({len(all_tc_ids)}): {', '.join(all_tc_ids)}")

    if combined_deleted_tc_ids:
        print(f"\nDeleted Tests (GM2 skipped): {', '.join(combined_deleted_tc_ids)}")

    # -------------------------------------------------------------------------
    # Step 4: Fix Version → target branches
    # -------------------------------------------------------------------------
    jira_branches = []
    for fv in combined_fix_versions:
        try:
            _, b = parse_fix_version(fv)
            jira_branches.append(b)
        except SystemExit:
            pass

    if args.target_branch:
        target_branch = args.target_branch
        version_str = target_branch.replace("release/", "")
        matched_fix_version = None
        for fv in combined_fix_versions:
            try:
                _, derived = parse_fix_version(fv)
                if derived == target_branch:
                    matched_fix_version = fv
                    break
            except SystemExit:
                pass
        fix_version_raw = matched_fix_version or version_str

        if target_branch not in jira_branches:
            warn(
                f"Target branch override '{target_branch}' does not match "
                f"any Jira Fix Version branch: {', '.join(jira_branches)}"
            )
            if args.non_interactive or not sys.stdin.isatty():
                err("Aborting run in non-interactive environment due to target branch mismatch.")
            confirm = input("Do you want to continue anyway? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
        warn(f"Target branch overridden to '{target_branch}'")
        versions_to_process = [(fix_version_raw, version_str, target_branch)]
    else:
        versions_to_process = []
        for fv in combined_fix_versions:
            vs, tb = parse_fix_version(fv)
            versions_to_process.append((fv, vs, tb))

    # -------------------------------------------------------------------------
    # Step 5: GM2 Runs + Eligibility
    # -------------------------------------------------------------------------
    overall_eligible = True
    total_run_count = 0

    # per_tc_bypasses: tc_id -> reason for IDs that bypass the GM2 check individually.
    # Pre-populated from --bypass-gm2-for; extended interactively during the loop.
    per_tc_bypasses: dict = {}
    if args.bypass_gm2_for:
        for _tid in args.bypass_gm2_for:
            per_tc_bypasses[_tid] = args.bypass_gm2_for_reason

    # cbb_eligible_tcs: tc_id -> cbb_branch used for CBB-based eligibility
    cbb_eligible_tcs: dict = {}

    if args.bypass_gm2:
        print("\n" + "!" * 60)
        print("  !! GM2 ELIGIBILITY CHECK BYPASSED !!")
        print(f"  Reason: {args.bypass_gm2_reason}")
        print("  Proceeding with backport without GM2 eligibility check.")
        print("!" * 60)
        verdict = "BYPASSED"
    elif skip_gm2_no_ids:
        print("\n" + "!" * 60)
        print("  !! GM2 CHECK SKIPPED — NO FEATURE FILE REFERENCES FOUND !!")
        print("  No TC/Xray IDs could be resolved via any extraction phase.")
        print("!" * 60)
        verdict = "BACKPORT ELIGIBLE (GM2 skipped — no IDs found)"
    else:
        print("\nTest Case GM2 Results:")
        print("-" * 40)

        if combined_deleted_tc_ids:
            for dtc in combined_deleted_tc_ids:
                print(f"  {dtc}: [SKIPPED — test deleted in this MR]")

        for tc_id in all_tc_ids:
            if tc_id in per_tc_bypasses:
                print(f"  {tc_id}: [BYPASSED] — {per_tc_bypasses[tc_id]}")
                continue

            runs = fetch_gm2_runs(tc_id, merged_at_iso)
            eligible, reason, pattern_str = evaluate_eligibility(tc_id, runs)
            total_run_count += len(runs)

            if not eligible and not runs:
                # Distinguish new test (no history) from CBB/rerun-only
                absence = classify_gm2_absence(tc_id, merged_at_iso)

                if absence == "NO_HISTORY":
                    print(f"  {tc_id}: [NEW TEST — NO GM2 HISTORY] -> [NOT ELIGIBLE]")
                    print(f"    This test has no GM2 executions post-merge. Backport not recommended.")
                    print(f"    If urgent, use: --bypass-gm2-for {tc_id} --bypass-gm2-for-reason '<reason>'")
                    if not args.non_interactive and sys.stdin.isatty():
                        confirm = input(
                            f"    Force bypass for new test {tc_id}? (y/n): "
                        ).strip().lower()
                        if confirm == "y":
                            br = input(f"    Enter bypass reason: ").strip() or "New test, urgent backport"
                            per_tc_bypasses[tc_id] = br
                            print(f"    [BYPASSED] {tc_id}: {br}")
                        else:
                            overall_eligible = False
                    else:
                        overall_eligible = False

                else:  # CBB_RERUN_ONLY
                    print(f"  {tc_id}: [NO NORMAL RUNS — ONLY CBB/RERUN FOUND]")
                    if not args.non_interactive and sys.stdin.isatty():
                        cbb_branch = input(
                            f"    Enter build_branches value to search CBB runs for {tc_id}"
                            f" (e.g. r26.2.3/user/QA-XXXXX), or press Enter to skip: "
                        ).strip()
                        if cbb_branch:
                            cbb_runs = fetch_gm2_runs_cbb(tc_id, merged_at_iso, cbb_branch)
                            cbb_ok, cbb_reason, cbb_pattern = evaluate_eligibility_cbb(tc_id, cbb_runs)
                            if cbb_ok:
                                print(f"    {tc_id} (CBB): {cbb_pattern} -> [ELIGIBLE via CBB]")
                                cbb_eligible_tcs[tc_id] = cbb_branch
                            else:
                                print(f"    {tc_id} (CBB): {cbb_pattern} -> [NOT ELIGIBLE]")
                                print(f"    Reason: {cbb_reason}")
                                overall_eligible = False
                        else:
                            print(f"    CBB check skipped for {tc_id}. Marked NOT ELIGIBLE.")
                            overall_eligible = False
                    else:
                        overall_eligible = False

            elif not eligible:
                print(f"  {tc_id}: {pattern_str} -> [NOT ELIGIBLE]")
                print(f"    Reason: {reason}")
                if not args.non_interactive and sys.stdin.isatty():
                    confirm = input(
                        f"    Bypass GM2 check for {tc_id}? (y/n): "
                    ).strip().lower()
                    if confirm == "y":
                        br = input(
                            f"    Enter bypass reason for {tc_id}: "
                        ).strip() or "User-confirmed bypass — known failure reason"
                        per_tc_bypasses[tc_id] = br
                        print(f"    [BYPASSED] {tc_id}: {br}")
                    else:
                        overall_eligible = False
                else:
                    overall_eligible = False
            else:
                print(f"  {tc_id}: {pattern_str} -> [ELIGIBLE]")

        print("-" * 40)
        has_special = bool(per_tc_bypasses or cbb_eligible_tcs)
        if overall_eligible and has_special:
            verdict = "BACKPORT ELIGIBLE (with selective GM2 overrides)"
        elif overall_eligible:
            verdict = "BACKPORT ELIGIBLE"
        else:
            verdict = "DO NOT BACKPORT"
        print(f"\nOverall: {verdict}")

    # -------------------------------------------------------------------------
    # Step 6: Commits to cherry-pick (dry-run display)
    # -------------------------------------------------------------------------
    # Collect commits from all MRs in order, filtering out merge commits
    commits_ordered = []
    for rec in mr_records:
        raw = fetch_mr_commits(PROJECT_ID_ENCODED, rec["iid"])
        filtered = [c for c in reversed(raw) if len(c.get("parent_ids", [])) <= 1]
        skipped = len(raw) - len(filtered)
        if skipped:
            warn(f"!{rec['iid']}: Skipping {skipped} merge commit(s) — only cherry-picking regular commits.")
        commits_ordered.extend(filtered)

    print(f"\nCommits to cherry-pick ({len(commits_ordered)}):")
    for c in commits_ordered:
        print(f"  {c['id'][:8]} - {c['title']}")

    # Use only the primary (first) Jira ID in the branch name for conciseness.
    # Multi-MR backports still cover all tickets via the MR description's "Closes" lines.
    jira_id_str = jira_ids[0]

    print(f"\nVersions to process: {len(versions_to_process)}")
    for fix_version_raw, version_str, target_branch in versions_to_process:
        backport_branch = f"r{version_str}_gm/{mr_author}/{jira_id_str}_SU"
        existing_mr, existing_mr_state = check_existing_backport_mr(PROJECT_ID_ENCODED, backport_branch, target_branch)
        print(f"\n  Fix Version:     {fix_version_raw}")
        print(f"  Target Branch:   {target_branch}")
        print(f"  Backport Branch: {backport_branch}")
        if existing_mr:
            if existing_mr_state == "opened":
                warn(f"  Backport MR already open: {existing_mr['web_url']}")
            elif existing_mr_state == "merged":
                warn(f"  Backport MR already merged: {existing_mr['web_url']}")
            elif existing_mr_state == "closed":
                warn(f"  Previously closed backport MR found: {existing_mr['web_url']} — will re-create on --execute.")

    if not args.execute:
        print("\nDry run complete. Run with --execute to create backport MR and post Jira comment.")
        print("=" * 60 + "\n")
        return

    # -------------------------------------------------------------------------
    # Phase 2: Execution
    # -------------------------------------------------------------------------
    if not overall_eligible and not args.bypass_gm2:
        err("GM2 eligibility check failed. Verdict: DO NOT BACKPORT. Aborting execution.")

    current_user = get_current_user()
    assignee_id = current_user.get("id")
    author_name = current_user.get("name", mr_author)
    author_email = current_user.get("email", "")
    info(f"Backport MR will be assigned to: {current_user.get('username', '?')}")

    reviewer_usernames = []
    if BACKPORT_REVIEWERS:
        reviewer_usernames += [u.strip() for u in BACKPORT_REVIEWERS.split(",") if u.strip()]
    reviewer_usernames += [u.strip() for u in args.reviewers if u.strip()]

    reviewer_ids = []
    if reviewer_usernames:
        info(f"Resolving reviewers: {', '.join(reviewer_usernames)}")
        reviewer_ids = resolve_usernames_to_ids(reviewer_usernames)

    labels = build_label_set(all_original_labels, BACKPORT_LABELS, args.labels)
    if labels:
        info(f"MR labels: {labels}")

    for fix_version_raw, version_str, target_branch in versions_to_process:
        backport_branch = f"r{version_str}_gm/{mr_author}/{jira_id_str}_SU"

        print("\n" + "=" * 60)
        print(f"  EXECUTING BACKPORT — {fix_version_raw}")
        print("=" * 60)

        existing_mr_obj, existing_mr_state = check_existing_backport_mr(PROJECT_ID_ENCODED, backport_branch, target_branch)

        if existing_mr_obj and existing_mr_state in ("opened", "merged"):
            backport_mr_url = existing_mr_obj["web_url"]
            warn(f"Skipping MR creation — backport MR already {existing_mr_state}: {backport_mr_url}")
        else:
            if existing_mr_obj and existing_mr_state == "closed":
                warn(f"Re-creating backport MR — previous MR was closed: {existing_mr_obj['web_url']}")
            create_backport_branch(PROJECT_ID_ENCODED, backport_branch, target_branch)

            conflict_occurred = False
            for c in commits_ordered:
                try:
                    cherry_pick_commit(PROJECT_ID_ENCODED, c["id"], backport_branch)
                except SystemExit:
                    conflict_occurred = True
                    info(f"Cleaning up branch '{backport_branch}' ...")
                    delete_branch(PROJECT_ID_ENCODED, backport_branch)
                    warn(
                        f"Cherry-pick conflict detected for {fix_version_raw}.\n"
                        f"  Branch '{backport_branch}' has been deleted.\n"
                        "  Please resolve conflicts manually and re-run."
                    )
                    break

            if conflict_occurred:
                print(f"  Skipping {fix_version_raw} due to conflict. Moving to next version.")
                continue

            # Build title and description
            if multi:
                title_suffixes = [rec["title"][len(rec["jira_id"]):].strip() for rec in mr_records]
                combined_suffix = " | ".join(s for s in title_suffixes if s)
                backport_title = f"{' '.join(jira_ids)} [Backport to {version_str}] {combined_suffix}"
                mr_titles_block = "\n".join(rec["title"] for rec in mr_records)
                see_mrs_block = "\n".join(
                    f"See merge request !{rec['iid']} (merged)" for rec in mr_records
                )
                head_shas_str = ", ".join(rec["head_sha"][:8] for rec in mr_records)
                commits_block = "\n".join(
                    f"{c['id'][:8]} {c['title']}" for c in commits_ordered
                )
                closes_block = "\n".join(f"Closes {jid}" for jid in jira_ids)
                backport_description = (
                    f"{mr_titles_block}\n\n"
                    f"{see_mrs_block}\n\n"
                    f"(cherry picked from commits {head_shas_str})\n\n"
                    f"{commits_block}\n\n"
                    f"Co-authored-by: {author_name} {author_email}\n\n"
                    f"{closes_block}"
                )
            else:
                rec = mr_records[0]
                title_suffix = rec["title"][len(rec["jira_id"]):].strip()
                backport_title = f"{rec['jira_id']} [Backport to {version_str}] {title_suffix}"
                first_commit = commits_ordered[0]
                backport_description = (
                    f"{rec['title']}\n\n"
                    f"See merge request !{rec['iid']} (merged)\n\n"
                    f"(cherry picked from commit {rec['head_sha'][:8]})\n\n"
                    f"{first_commit['id'][:8]} {first_commit['title']}\n\n"
                    f"Co-authored-by: {author_name} {author_email}\n\n"
                    f"Closes {rec['jira_id']}"
                )

            if combined_deleted_tc_ids:
                backport_description += (
                    f"\n\nNote: The following tests were deleted in this MR and skipped from GM2 check:\n"
                    + "\n".join(f"  - {dtc}" for dtc in combined_deleted_tc_ids)
                )

            backport_mr = create_mr(
                PROJECT_ID_ENCODED,
                backport_branch,
                target_branch,
                backport_title,
                backport_description,
                assignee_id=assignee_id,
                reviewer_ids=reviewer_ids if reviewer_ids else None,
                labels=labels if labels else None,
            )
            backport_mr_url = backport_mr["web_url"]
            info(f"Backport MR created: {backport_mr_url}")

        # Post Jira comment to the primary (first) ticket only — the backport MR
        # description already closes all Jira IDs, so a single comment is enough.
        primary_rec = mr_records[0]
        primary_jira_id = primary_rec["jira_id"]
        info(f"Posting Jira comment to {primary_jira_id} ...")
        comment_body = build_jira_comment(
            original_mr_url=primary_rec["web_url"],
            backport_mr_url=backport_mr_url,
            dev_checkin_jira=primary_rec["caused_by_jira"],
            gm2_run_count=total_run_count,
            test_case_count=len(all_tc_ids),
            fix_version=fix_version_raw,
            tc_ids=all_tc_ids,
            bypass_gm2=args.bypass_gm2,
            bypass_reason=args.bypass_gm2_reason if args.bypass_gm2 else None,
            per_tc_bypasses=per_tc_bypasses if not args.bypass_gm2 else None,
            deleted_tc_ids=combined_deleted_tc_ids if not args.bypass_gm2 else None,
            cbb_eligible_tcs=cbb_eligible_tcs if not args.bypass_gm2 else None,
            skip_gm2_no_ids=skip_gm2_no_ids,
        )
        jira_post_comment(primary_jira_id, comment_body)
        info(f"Jira comment posted to {primary_jira_id}.")

        # Transition all Jira issues to 'MR to GM', regardless of current state.
        for rec in mr_records:
            ensure_transition_to_mr_to_gm(rec["jira_id"])

        print(f"  Jira Comment: Posted to {JIRA_URL}/browse/{primary_jira_id}")

        print(f"\n  Backport MR:  {backport_mr_url}")
        print(f"  GM2 Verdict:  {verdict}")

    print("\n" + "=" * 60)
    print("  ALL VERSIONS PROCESSED")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
