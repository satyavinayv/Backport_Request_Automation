#!/usr/bin/env python3
"""
backport.py - GitLab/Jira/GM2 Backport Automation

Usage:
    python backport.py --mr <MR_IID>           # Phase 1: read-only validation
    python backport.py --mr <MR_IID> --execute  # Phase 2: full execution

Environment Variables Required:
    GITLAB_TOKEN      - GitLab personal access token
    GITLAB_URL        - e.g. https://gitlab.veevadev.com
    JIRA_URL          - e.g. https://jira.veevadev.com
    JIRA_USERNAME     - Jira username/email
    JIRA_PAT          - Jira personal access token
    OPENSEARCH_URL    - e.g. https://autoinfra-es.vaultdev.com:9200
"""
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import argparse
import os
import re
import sys
import json
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.veevadev.com")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
JIRA_URL = os.environ.get("JIRA_URL", "https://jira.veevadev.com")
JIRA_PAT = os.environ.get("JIRA_PAT", "")
JIRA_HEADERS = {
    "Authorization": f"Bearer {JIRA_PAT}",
    "Content-Type": "application/json",
}
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "https://autoinfra-es.vaultdev.com:9200")

MANAGER_NAME = "Vinil Pokala"
JIRA_TRANSITION_PASSED_GM2 = "751"   # Running on GM2 → GM Data Creation
JIRA_TRANSITION_DATA_CREATED = "761"  # GM Data Creation → MR to GM

GITLAB_HEADERS = {
    "PRIVATE-TOKEN": GITLAB_TOKEN,
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def err(msg):
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"[WARN]  {msg}")


def info(msg):
    print(f"[INFO]  {msg}")


def gitlab_get(path, params=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.get(url, headers=GITLAB_HEADERS, params=params, timeout=30)
    if resp.status_code == 404:
        err(f"GitLab 404: {url}")
    resp.raise_for_status()
    return resp.json()


def gitlab_post(path, payload):
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.post(url, headers=GITLAB_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def jira_get(path):
    url = f"{JIRA_URL}{path}"
    resp = requests.get(url, headers=JIRA_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def jira_post_comment(issue_key, body):
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/comment"
    resp = requests.post(url, headers=JIRA_HEADERS, json={"body": body}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def opensearch_query(payload):
    url = f"{OPENSEARCH_URL}/autoresult-*/_search"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,  # internal cert; suppress with VPN
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        err(
            "Cannot reach OpenSearch. Ensure you are connected to the company VPN.\n"
            f"  Endpoint: {url}"
        )


# ---------------------------------------------------------------------------
# Step 1: Fetch MR Metadata
# ---------------------------------------------------------------------------

def fetch_mr(project_id_encoded, mr_iid):
    info(f"Fetching MR !{mr_iid} ...")
    return gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}")


def extract_jira_id(mr_title):
    match = re.search(r"[A-Z]+-\d+", mr_title)
    if not match:
        err(
            f"No Jira ID found in MR title: '{mr_title}'\n"
            "  Expected pattern like QA-545535 in the title."
        )
    return match.group(0)


def extract_test_case_ids(description):
    tc_ids = []
    xray_ids = []

    if description:
        tc_match = re.search(r"Test Cases:\s*(.+)", description, re.IGNORECASE)
        if tc_match:
            tc_ids = [t.strip() for t in tc_match.group(1).split(",") if t.strip()]

        xr_match = re.search(r"Xray IDs:\s*(.+)", description, re.IGNORECASE)
        if xr_match:
            xray_ids = [t.strip() for t in xr_match.group(1).split(",") if t.strip()]

    all_ids = tc_ids + xray_ids
    if not all_ids:
        err(
            "No Test Cases or Xray IDs found in MR description.\n"
            "  Add lines like:\n"
            "    Test Cases: TC-1234, TC-5678\n"
            "    Xray IDs: XR-9012"
        )
    return tc_ids, xray_ids


def parse_fix_version(version_str):
    """Convert '26R2.3' -> 'release/26.2.3'"""
    match = re.match(r"(\d+)R(\d+)\.(\d+)", version_str)
    if not match:
        err(
            f"Fix Version '{version_str}' does not match expected pattern (e.g. 26R2.3)."
        )
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{patch}", f"release/{major}.{minor}.{patch}"


# ---------------------------------------------------------------------------
# Step 2: Fetch Jira Metadata
# ---------------------------------------------------------------------------

def fetch_jira_issue(jira_id):
    info(f"Fetching Jira issue {jira_id} ...")
    try:
        return jira_get(f"/rest/api/2/issue/{jira_id}")
    except requests.exceptions.HTTPError as e:
        err(f"Failed to fetch Jira issue {jira_id}: {e}")


def get_fix_versions(jira_issue):
    """Return all fix versions from Jira issue."""
    fix_versions = jira_issue.get("fields", {}).get("fixVersions", [])
    if not fix_versions:
        err(
            f"No Fix Version set in Jira issue {jira_issue['key']}.\n"
            "  Set the Fix Version/s field in Jira before running backport."
        )
    return [v["name"] for v in fix_versions]


def get_caused_by_jira(jira_issue):
    """Return linked 'is caused by' issue key if present."""
    links = jira_issue.get("fields", {}).get("issuelinks", [])
    for link in links:
        link_type = link.get("type", {}).get("name", "").lower()
        if "caused by" in link_type or "is caused by" in link_type:
            inward = link.get("inwardIssue") or link.get("outwardIssue")
            if inward:
                return inward.get("key")
    return None

def get_jira_status(jira_id):
    """Get current workflow status of a Jira issue."""
    issue = jira_get(f"/rest/api/2/issue/{jira_id}?fields=status")
    return issue.get("fields", {}).get("status", {}).get("name", "")

# ---------------------------------------------------------------------------
# Step 3: OpenSearch GM2 Results
# ---------------------------------------------------------------------------

def fetch_gm2_runs(tc_id, merged_at_iso):
    info(f"  Querying GM2 runs for {tc_id} after {merged_at_iso} ...")
    payload = {
        "size": 10,
        "_source": {"excludes": []},
        "query": {
            "bool": {
                "filter": [
                    {"match_phrase": {"environment": "GM2"}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": merged_at_iso,
                                "lte": "now",
                            }
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"match_phrase": {"x_ray_id": tc_id}}
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "asc"}}],
    }

    data = opensearch_query(payload)
    hits = data.get("hits", {}).get("hits", [])

    runs = []
    for hit in hits:
        src = hit.get("_source", {})
        # rerun_count may be stored as string "0" or integer 0
        rerun_count = src.get("rerun_count", 0)
        try:
            rerun_count = int(rerun_count)
        except (ValueError, TypeError):
            rerun_count = 0

        # isCBB may be bool or string
        is_cbb = src.get("isCBB", False)
        if isinstance(is_cbb, str):
            is_cbb = is_cbb.lower() == "true"

        # Strict filter in Python
        if rerun_count != 0:
            continue
        if is_cbb:
            continue

        runs.append({
            "timestamp": src.get("@timestamp"),
            "test_result": src.get("test_result", ""),
            "vault_version": src.get("vault_version", ""),
            "test_case_id": src.get("test_case_id", ""),
            "x_ray_id": src.get("x_ray_id", ""),
            "rerun_count": rerun_count,
            "isCBB": is_cbb,
            "error_message": src.get("error_message", ""),
            "environment": src.get("environment", ""),
        })
    return runs



# ---------------------------------------------------------------------------
# Step 4: GM2 Eligibility
# ---------------------------------------------------------------------------

def evaluate_eligibility(tc_id, runs):
    """
    Returns (eligible: bool, reason: str, pattern_str: str)
    Eligible only when the latest two executions are both Passed.
    """
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


# ---------------------------------------------------------------------------
# Step 5: Cherry-pick Dry Run
# ---------------------------------------------------------------------------

def fetch_mr_commits(project_id_encoded, mr_iid):
    return gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/commits")


def check_existing_backport_mr(project_id_encoded, backport_branch, target_branch):
    """Check if a backport MR already exists for this branch."""
    mrs = gitlab_get(
        f"/projects/{project_id_encoded}/merge_requests",
        params={"state": "opened", "target_branch": target_branch, "per_page": 100},
    )
    for mr in mrs:
        if mr.get("source_branch") == backport_branch:
            return mr
    return None


# ---------------------------------------------------------------------------
# Step 6: Backport MR Creation
# ---------------------------------------------------------------------------

def create_backport_branch(project_id_encoded, backport_branch, target_branch):
    info(f"Creating branch '{backport_branch}' from '{target_branch}' ...")
    payload = {"branch": backport_branch, "ref": target_branch}
    try:
        return gitlab_post(f"/projects/{project_id_encoded}/repository/branches", payload)
    except requests.exceptions.HTTPError as e:
        err(f"Failed to create branch '{backport_branch}': {e}")


def cherry_pick_commit(project_id_encoded, commit_sha, backport_branch):
    info(f"  Cherry-picking {commit_sha[:8]} onto '{backport_branch}' ...")
    payload = {"branch": backport_branch}
    url = f"{GITLAB_URL}/api/v4/projects/{project_id_encoded}/repository/commits/{commit_sha}/cherry_pick"
    resp = requests.post(url, headers=GITLAB_HEADERS, json=payload, timeout=30)
    if resp.status_code == 400:
        data = resp.json()
        msg = data.get("message", str(data))
        if "conflict" in msg.lower() or "cherry-pick" in msg.lower():
            err(
                f"Cherry-pick conflict on commit {commit_sha[:8]}.\n"
                f"  Message: {msg}\n"
                "  Manual resolution required. Backport MR NOT created."
            )
        err(f"Cherry-pick failed for {commit_sha[:8]}: {msg}")
    resp.raise_for_status()
    return resp.json()


def create_mr(project_id_encoded, source_branch, target_branch, title, description):
    info(f"Creating Backport MR: '{title}' ...")
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
    }
    return gitlab_post(f"/projects/{project_id_encoded}/merge_requests", payload)


def build_dashboard_url(tc_id):
    """Build OpenSearch dashboard path filtered by a single test case ID."""
    kql_query = f'"{tc_id}"'
    base = (
        "/app/data-explorer/discover/#"
        "?_a=(discover:(columns:!(environment,vault_version,test_case_id,test_result,"
        "rerun_count,isCBB,error_message,feature,scenario,row),isDirty:!f,sort:!()),"
        "metadata:(indexPattern:'autoresult-*',view:discover))"
        "&_g=(filters:!(),refreshInterval:(pause:!t,value:0),time:(from:now-1y,to:now))"
        "&_q=(filters:!(('$state':(store:appState),meta:(alias:!n,disabled:!f,"
        "index:'autoresult-*',key:isCBB,negate:!f,params:(query:!f),type:phrase),"
        "query:(match_phrase:(isCBB:!f))),('$state':(store:appState),meta:(alias:!n,"
        "disabled:!f,index:'autoresult-*',key:rerun_count,negate:!f,params:(query:'0'),"
        "type:phrase),query:(match_phrase:(rerun_count:'0'))),('$state':(store:appState),"
        "meta:(alias:!n,disabled:!f,index:'autoresult-*',key:environment,negate:!f,"
        "params:(query:GM2),type:phrase),query:(match_phrase:(environment:GM2)))),"
        f"query:(language:kuery,query:{kql_query}))"
    )
    return base  # starts with /app/ — correct format for shorten API


def shorten_dashboard_url(path):
    """Shorten a dashboard path via OpenSearch short URL API."""
    try:
        resp = requests.post(
            "https://autoinfra-es.vaultdev.com/_dashboards/api/shorten_url",
            json={"url": path},
            headers={
                "Content-Type": "application/json",
                "osd-xsrf": "true",
            },
            timeout=10,
            verify=False,
        )
        info(f"  Shorten API status: {resp.status_code}")
        info(f"  Shorten API response: {resp.text}")
        resp.raise_for_status()
        url_id = resp.json().get("urlId")
        if url_id:
            return f"https://autoinfra-es.vaultdev.com/_dashboards/goto/{url_id}"
        warn(f"Shorten URL returned no urlId: {resp.text}")
    except Exception as e:
        warn(f"URL shortening failed ({type(e).__name__}): {e}")
    # Fallback: full long URL with domain
    return f"https://autoinfra-es.vaultdev.com/_dashboards{path}"

def transition_jira_issue(jira_id, transition_id, transition_name):
    """Transition a Jira issue to a new workflow state."""
    url = f"{JIRA_URL}/rest/api/2/issue/{jira_id}/transitions"
    payload = {"transition": {"id": str(transition_id)}}
    resp = requests.post(url, headers=JIRA_HEADERS, json=payload, timeout=30)
    if resp.status_code == 204:
        info(f"Jira {jira_id} transitioned to '{transition_name}'")
        return True
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Step 7: Jira Comment
# ---------------------------------------------------------------------------

def build_jira_comment(
    original_mr_url,
    backport_mr_url,
    dev_checkin_jira,
    gm2_run_count,
    test_case_count,
    fix_version,
    tc_ids,
):
    risk = "Low" if test_case_count <= 2 else ("Medium" if test_case_count <= 5 else "High")
    dev_checkin_answer = f"Yes - {dev_checkin_jira}" if dev_checkin_jira else "No"

    # Build per-TC GM2 run links
    gm2_links = []
    for tc_id in tc_ids:
        path = build_dashboard_url(tc_id)  # path only e.g. /_dashboards/app/...
        short_url = shorten_dashboard_url(path)  # returns short URL or full fallback
        gm2_links.append(f"{tc_id}: {short_url}")
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
A: {gm2_run_count} times

Q: What is the risk of regression (Low/ High)?
A: {risk}

Q: How many tests are impacted?
A: {test_case_count}

Q: What release versions are you requesting this approval for (e.g.24R3.0, 24R3.1)?
A: {fix_version}

Q: Have the requesting release version(s) updated in Fix version/s field in Jira?
A: Yes

{MANAGER_NAME} Kindly approve the backport request
GM2 Runs:
{gm2_runs_section}"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GitLab/Jira/GM2 Backport Automation")
    parser.add_argument("--mr", required=True, type=int, help="Develop MR IID")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Phase 2: actually create backport MR and post Jira comment",
    )
    parser.add_argument(
        "--target-branch",
        type=str,
        default=None,
        help="Override target release branch (e.g. release/26.2.2).",
    )
    args = parser.parse_args()

    # Validate env vars
    missing = [v for v in ["GITLAB_TOKEN", "JIRA_PAT"] if not os.environ.get(v)]
    if missing:
        err(f"Missing required environment variables: {', '.join(missing)}")

    print("\n" + "=" * 60)
    print("  BACKPORT ELIGIBILITY REPORT")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 1: MR Metadata
    # -----------------------------------------------------------------------
    project_path = "veevavault/vaultautomationtests"
    project_id_encoded = project_path.replace("/", "%2F")

    mr = fetch_mr(project_id_encoded, args.mr)
    mr_title = mr.get("title", "")
    mr_description = mr.get("description", "") or ""
    mr_author = mr.get("author", {}).get("username", "unknown")
    mr_web_url = mr.get("web_url", "")
    merged_at = mr.get("merged_at")

    if not merged_at:
        err(f"MR !{args.mr} has not been merged yet.")

    merged_at_iso = merged_at if merged_at.endswith("Z") else merged_at + "Z"
    jira_id = extract_jira_id(mr_title)
    tc_ids, xray_ids = extract_test_case_ids(mr_description)
    all_tc_ids = tc_ids + xray_ids

    print(f"\nMR:            !{args.mr}")
    print(f"Title:         {mr_title}")
    print(f"Author:        {mr_author}")
    print(f"Merged At:     {merged_at_iso}")
    print(f"Jira:          {jira_id}")
    print(f"Test Cases:    {', '.join(tc_ids) or 'none'}")
    print(f"Xray IDs:      {', '.join(xray_ids) or 'none'}")

    # -----------------------------------------------------------------------
    # Step 2: Jira Metadata
    # -----------------------------------------------------------------------
    jira_issue = fetch_jira_issue(jira_id)
    fix_versions = get_fix_versions(jira_issue)
    caused_by_jira = get_caused_by_jira(jira_issue)

    info(f"Fix Versions found: {', '.join(fix_versions)}")

    # Build list of (fix_version_raw, version_str, target_branch) to process
    if args.target_branch:
        target_branch = args.target_branch
        version_str = target_branch.replace("release/", "")
        # Find matching fix version label from Jira for use in comment
        matched_fix_version = None
        for fv in fix_versions:
            try:
                _, derived = parse_fix_version(fv)
                if derived == target_branch:
                    matched_fix_version = fv
                    break
            except SystemExit:
                pass
        fix_version_raw = matched_fix_version or version_str
        # Warn if override doesn't match any Jira fix version
        jira_branches = []
        for fv in fix_versions:
            try:
                _, b = parse_fix_version(fv)
                jira_branches.append(b)
            except SystemExit:
                pass
        if target_branch not in jira_branches:
            warn(
                f"Target branch override '{target_branch}' does not match "
                f"any Jira Fix Version branch: {', '.join(jira_branches)}"
            )
            confirm = input("Do you want to continue anyway? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
        warn(f"Target branch overridden to '{target_branch}'")
        versions_to_process = [(fix_version_raw, version_str, target_branch)]
    else:
        versions_to_process = []
        for fv in fix_versions:
            vs, tb = parse_fix_version(fv)
            versions_to_process.append((fv, vs, tb))

    # -----------------------------------------------------------------------
    # Step 3 & 4: GM2 Runs + Eligibility (shared across all versions)
    # -----------------------------------------------------------------------
    print("\nTest Case GM2 Results:")
    print("-" * 40)

    overall_eligible = True
    total_run_count = 0

    for tc_id in all_tc_ids:
        runs = fetch_gm2_runs(tc_id, merged_at_iso)
        eligible, reason, pattern_str = evaluate_eligibility(tc_id, runs)
        total_run_count += len(runs)
        status = "[ELIGIBLE]" if eligible else "[NOT ELIGIBLE]"
        print(f"  {tc_id}: {pattern_str} -> {status}")
        if not eligible:
            print(f"    Reason: {reason}")
            overall_eligible = False

    print("-" * 40)
    verdict = "BACKPORT" if overall_eligible else "DO NOT BACKPORT"
    print(f"\nOverall: {verdict}")

    # -----------------------------------------------------------------------
    # Step 5: Cherry-pick Dry Run (shared across all versions)
    # -----------------------------------------------------------------------
    commits = fetch_mr_commits(project_id_encoded, args.mr)
    commits_ordered = list(reversed(commits))

    print(f"\nCommits to cherry-pick ({len(commits_ordered)}):")
    for c in commits_ordered:
        print(f"  {c['id'][:8]} - {c['title']}")

    # Show dry run info per version
    print(f"\nVersions to process: {len(versions_to_process)}")
    for fix_version_raw, version_str, target_branch in versions_to_process:
        backport_branch = f"r{version_str}_gm/{mr_author}/{jira_id}_SU"
        existing_mr = check_existing_backport_mr(project_id_encoded, backport_branch, target_branch)
        print(f"\n  Fix Version:     {fix_version_raw}")
        print(f"  Target Branch:   {target_branch}")
        print(f"  Backport Branch: {backport_branch}")
        if existing_mr:
            warn(f"  Backport MR already exists: {existing_mr['web_url']}")

    if not args.execute:
        print(
            "\nDry run complete. Run with --execute to create the backport MR and post Jira comment."
        )
        print("=" * 60 + "\n")
        return

    # -----------------------------------------------------------------------
    # Phase 2: Execute — loop over each fix version
    # -----------------------------------------------------------------------
    if not overall_eligible:
        err("GM2 eligibility check failed. Verdict: DO NOT BACKPORT. Aborting execution.")

    for fix_version_raw, version_str, target_branch in versions_to_process:
        backport_branch = f"r{version_str}_gm/{mr_author}/{jira_id}_SU"

        print("\n" + "=" * 60)
        print(f"  EXECUTING BACKPORT — {fix_version_raw}")
        print("=" * 60)

        existing_mr = check_existing_backport_mr(project_id_encoded, backport_branch, target_branch)

        if existing_mr:
            backport_mr_url = existing_mr["web_url"]
            warn(f"Skipping MR creation — backport MR already exists: {backport_mr_url}")
        else:
            # Create branch
            create_backport_branch(project_id_encoded, backport_branch, target_branch)

            # Cherry-pick commits with conflict handling
            conflict_occurred = False
            for c in commits_ordered:
                try:
                    cherry_pick_commit(project_id_encoded, c["id"], backport_branch)
                except SystemExit:
                    conflict_occurred = True
                    info(f"Cleaning up branch '{backport_branch}' ...")
                    cleanup_url = (
                        f"{GITLAB_URL}/api/v4/projects/{project_id_encoded}"
                        f"/repository/branches/{requests.utils.quote(backport_branch, safe='')}"
                    )
                    requests.delete(cleanup_url, headers=GITLAB_HEADERS, timeout=30)
                    warn(
                        f"Cherry-pick conflict detected for {fix_version_raw}.\n"
                        f"  Branch '{backport_branch}' has been deleted.\n"
                        "  Please resolve conflicts manually and re-run."
                    )
                    break

            if conflict_occurred:
                print(f"  Skipping {fix_version_raw} due to conflict. Moving to next version.")
                continue

            # Create MR
            backport_title = f"Backport: {mr_title}"
            backport_description = (
                f"Automated backport of !{args.mr} to `{target_branch}`.\n\n"
                f"Jira: {jira_id}\n"
                f"Original MR: {mr_web_url}"
            )
            backport_mr = create_mr(
                project_id_encoded,
                backport_branch,
                target_branch,
                backport_title,
                backport_description,
            )
            backport_mr_url = backport_mr["web_url"]
            info(f"Backport MR created: {backport_mr_url}")

        # Post Jira comment
        info(f"Posting Jira comment to {jira_id} ...")
        comment_body = build_jira_comment(
            original_mr_url=mr_web_url,
            backport_mr_url=backport_mr_url,
            dev_checkin_jira=caused_by_jira,
            gm2_run_count=total_run_count,
            test_case_count=len(all_tc_ids),
            fix_version=fix_version_raw,
            tc_ids=all_tc_ids,
        )
        jira_post_comment(jira_id, comment_body)
        info("Jira comment posted successfully.")

        # Jira workflow transition
        current_status = get_jira_status(jira_id)
        info(f"Current Jira status: {current_status}")

        if current_status == "Running on GM2":
            info(f"Transitioning {jira_id}: Running on GM2 → GM Data Creation ...")
            transition_jira_issue(jira_id, JIRA_TRANSITION_PASSED_GM2, "GM Data Creation")
            info(f"Transitioning {jira_id}: GM Data Creation → MR to GM ...")
            transition_jira_issue(jira_id, JIRA_TRANSITION_DATA_CREATED, "MR to GM")
        elif current_status == "GM Data Creation":
            info(f"Transitioning {jira_id}: GM Data Creation → MR to GM ...")
            transition_jira_issue(jira_id, JIRA_TRANSITION_DATA_CREATED, "MR to GM")
        elif current_status == "MR to GM":
            warn(f"Jira {jira_id} already in 'MR to GM' — skipping transitions.")
        else:
            warn(f"Jira {jira_id} is in unexpected status '{current_status}' — skipping transitions.")

        print(f"\n  Backport MR:  {backport_mr_url}")
        print(f"  Jira Comment: Posted to {JIRA_URL}/browse/{jira_id}")
        print(f"  GM2 Verdict:  {verdict}")

    print("\n" + "=" * 60)
    print("  ALL VERSIONS PROCESSED")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()