#!/usr/bin/env python3
"""
backport.py - GitLab/Jira/GM2 Backport Automation

Usage:
    python backport.py --mr <MR_IID>                       # Phase 1: read-only validation
    python backport.py --mr <MR_IID> --inspect-ids         # Inspect modified test IDs from diffs/scenarios
    python backport.py --mr <MR_IID> --execute             # Phase 2: full execution
    python backport.py --mr <MR_IID> --execute --non-interactive # CI/CD headless execution

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
import urllib.parse
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


def gitlab_get_raw(path, params=None):
    """Fetch raw text content from GitLab API."""
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.get(url, headers=GITLAB_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


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
# Step 1: Scenario Isolation & ID Extraction Logic
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


def fetch_raw_file_content(project_id_encoded, file_path, ref_sha):
    """Fetch raw file content at a specific commit ref from GitLab."""
    encoded_path = urllib.parse.quote(file_path, safe="")
    path = f"/projects/{project_id_encoded}/repository/files/{encoded_path}/raw"
    try:
        return gitlab_get_raw(path, params={"ref": ref_sha})
    except Exception as e:
        warn(f"Could not fetch raw file content for {file_path}: {e}")
        return ""


def parse_diff_modified_lines(diff_text):
    """Extract 1-indexed line numbers in the NEW file that were added/modified (+)."""
    modified_lines = []
    current_new_line = 0

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                current_new_line = int(match.group(1)) - 1
        elif line.startswith("+") and not line.startswith("+++"):
            current_new_line += 1
            modified_lines.append(current_new_line)
        elif line.startswith("-") and not line.startswith("---"):
            pass  # Deleted line does not advance line count in the new file version
        else:
            current_new_line += 1

    return modified_lines


def extract_scenario_block_ids(file_content, modified_line_nums):
    """Isolate only the specific Scenario/Scenario Outline blocks touched by code changes."""
    lines = file_content.splitlines()
    total_lines = len(lines)
    tc_ids, xray_ids = set(), set()
    matched_blocks = []
    processed_ranges = set()

    for mod_line in modified_line_nums:
        idx = mod_line - 1
        if idx < 0 or idx >= total_lines:
            continue

        # Search upwards for scenario header
        start_idx = idx
        while start_idx > 0:
            if re.match(r"^\s*(Scenario|Scenario Outline):", lines[start_idx], re.IGNORECASE):
                # Include preceding @tags
                while start_idx > 0 and lines[start_idx - 1].strip().startswith("@"):
                    start_idx -= 1
                break
            start_idx -= 1

        # Search downwards for end of block or next scenario header
        end_idx = idx
        while end_idx < total_lines - 1:
            next_line = lines[end_idx + 1]
            if re.match(r"^\s*(Scenario|Scenario Outline):", next_line, re.IGNORECASE) or \
               (next_line.strip().startswith("@") and any(
                   re.match(r"^\s*(Scenario|Scenario Outline):", lines[k], re.IGNORECASE)
                   for k in range(end_idx + 1, min(end_idx + 10, total_lines))
               )):
                break
            end_idx += 1

        block_key = (start_idx, end_idx)
        if block_key in processed_ranges:
            continue
        processed_ranges.add(block_key)

        block_text = "\n".join(lines[start_idx : end_idx + 1])

        # Extract IDs strictly within this scenario block
        found_tcs = [tc.strip().upper() for tc in re.findall(r"@TestCase:\s*([A-Za-z0-9_-]+)", block_text, re.IGNORECASE)]
        found_tcs += [tc.replace("_", "-").upper() for tc in re.findall(r"\bTC[_-]\d+\b", block_text, re.IGNORECASE)]

        found_xrays = [xr.strip().upper() for xr in re.findall(r"@Xray(?:ID)?:\s*([A-Za-z0-9_-]+)", block_text, re.IGNORECASE)]
        found_xrays += [xr.replace("_", "-").upper() for xr in re.findall(r"\b(?:XR|DEV)[_-]\d+\b", block_text, re.IGNORECASE)]

        if found_tcs or found_xrays:
            unique_tcs = sorted(list(set(found_tcs)))
            unique_xrays = sorted(list(set(found_xrays)))
            tc_ids.update(unique_tcs)
            xray_ids.update(unique_xrays)
            matched_blocks.append({
                "start_line": start_idx + 1,
                "end_line": end_idx + 1,
                "tc_ids": unique_tcs,
                "xray_ids": unique_xrays,
            })

    return sorted(list(tc_ids)), sorted(list(xray_ids)), matched_blocks


def extract_ids_from_diff(project_id_encoded, mr_iid, head_sha=None):
    """
    Phase 1: Scans newly added/modified lines (+) directly in git patch.
    Phase 2: Fallback to scenario-bounded parsing of modified test files.
    """
    info(f"Scanning MR !{mr_iid} code diffs for Test IDs...")
    try:
        mr_changes = gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/changes")
    except Exception as e:
        warn(f"Could not fetch MR changes from GitLab: {e}")
        return [], [], []

    tc_ids = set()
    xray_ids = set()
    matches_detail = []

    # Phase 1: Direct scan on modified (+) lines
    for change in mr_changes.get("changes", []):
        file_path = change.get("new_path", "unknown")
        diff_text = change.get("diff", "")
        if not diff_text:
            continue

        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                found_tcs = [tc.strip().upper() for tc in re.findall(r"@TestCase:\s*([A-Za-z0-9_-]+)", line, re.IGNORECASE)]
                if not found_tcs:
                    found_tcs = [tc.replace("_", "-").upper() for tc in re.findall(r"\bTC[_-]\d+\b", line, re.IGNORECASE)]

                found_xrays = [xr.strip().upper() for xr in re.findall(r"@Xray(?:ID)?:\s*([A-Za-z0-9_-]+)", line, re.IGNORECASE)]
                if not found_xrays:
                    found_xrays = [xr.replace("_", "-").upper() for xr in re.findall(r"\b(?:XR|DEV)[_-]\d+\b", line, re.IGNORECASE)]

                if found_tcs or found_xrays:
                    matches_detail.append({
                        "file": file_path,
                        "line": line.strip(),
                        "tc_ids": found_tcs,
                        "xray_ids": found_xrays,
                    })
                    tc_ids.update(found_tcs)
                    xray_ids.update(found_xrays)

    # Phase 2: Isolated Scenario Block Fallback
    if not tc_ids and not xray_ids and head_sha:
        info("No test IDs found on modified (+) lines. Isolating modified scenario blocks...")
        for change in mr_changes.get("changes", []):
            file_path = change.get("new_path", "")
            diff_text = change.get("diff", "")
            if not file_path.endswith((".feature", ".java", ".py")) or not diff_text:
                continue

            mod_lines = parse_diff_modified_lines(diff_text)
            if not mod_lines:
                continue

            content = fetch_raw_file_content(project_id_encoded, file_path, head_sha)
            if not content:
                continue

            sc_tcs, sc_xrays, block_details = extract_scenario_block_ids(content, mod_lines)
            for b in block_details:
                matches_detail.append({
                    "file": file_path,
                    "line": f"[SCENARIO BLOCK Lines {b['start_line']}-{b['end_line']}]",
                    "tc_ids": b["tc_ids"],
                    "xray_ids": b["xray_ids"],
                })
            tc_ids.update(sc_tcs)
            xray_ids.update(sc_xrays)

    return sorted(list(tc_ids)), sorted(list(xray_ids)), matches_detail


def extract_test_case_ids(description):
    """Fallback extraction from MR description fields."""
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


def resolve_mr_test_case_ids(project_id_encoded, mr_iid, description, head_sha=None):
    """
    Primary: Extract IDs from added code diffs or isolated scenario blocks.
    Fallback: Extract IDs from MR description text block.
    """
    tc_ids, xray_ids, details = extract_ids_from_diff(project_id_encoded, mr_iid, head_sha)

    if tc_ids or xray_ids:
        info(f"Extracted Test IDs directly from MR code files.")
        return tc_ids, xray_ids, "diff", details

    info("No test IDs matched in code diffs or scenario blocks. Falling back to MR description...")
    try:
        desc_tc_ids, desc_xray_ids = extract_test_case_ids(description)
        return desc_tc_ids, desc_xray_ids, "description", []
    except SystemExit:
        err(
            f"No Test Case or Xray IDs found in MR !{mr_iid} code diffs, scenarios, or description.\n"
            "  Ensure test annotations (e.g. TC-1234, XR-5678, DEV-1085503) exist in code "
            "or are declared in the MR description."
        )


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
# Step 2: Fetch Jira Metadata & Dynamic Workflow Transitions
# ---------------------------------------------------------------------------

def fetch_jira_issue(jira_id):
    info(f"Fetching Jira issue {jira_id} ...")
    try:
        return jira_get(f"/rest/api/2/issue/{jira_id}")
    except requests.exceptions.HTTPError as e:
        err(f"Failed to fetch Jira issue {jira_id}: {e}")


def get_fix_versions(jira_issue):
    """Safely extract fix versions handling missing/null payload structures."""
    fields = (jira_issue.get("fields") if isinstance(jira_issue, dict) else {}) or {}
    fix_versions = fields.get("fixVersions") or []

    names = [v.get("name") for v in fix_versions if isinstance(v, dict) and v.get("name")]
    if not names:
        issue_key = jira_issue.get("key", "unknown") if isinstance(jira_issue, dict) else "unknown"
        err(
            f"No Fix Version set in Jira issue {issue_key}.\n"
            "  Set the Fix Version/s field in Jira before running backport."
        )
    return names


def get_caused_by_jira(jira_issue):
    """Return linked 'is caused by' issue key if present."""
    fields = (jira_issue.get("fields") if isinstance(jira_issue, dict) else {}) or {}
    links = fields.get("issuelinks") or []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type", {}).get("name", "").lower()
        if "caused by" in link_type or "is caused by" in link_type:
            inward = link.get("inwardIssue") or link.get("outwardIssue")
            if inward and isinstance(inward, dict):
                return inward.get("key")
    return None


def get_jira_status(jira_id):
    """Get current workflow status of a Jira issue."""
    issue = jira_get(f"/rest/api/2/issue/{jira_id}?fields=status")
    return issue.get("fields", {}).get("status", {}).get("name", "")


def get_jira_transition_id(jira_id, target_status):
    """Dynamically resolve transition ID by destination state or transition name."""
    data = jira_get(f"/rest/api/2/issue/{jira_id}/transitions")
    transitions = data.get("transitions", [])

    for t in transitions:
        to_name = t.get("to", {}).get("name", "").lower()
        trans_name = t.get("name", "").lower()
        target_lower = target_status.lower()

        if target_lower in (to_name, trans_name):
            return t.get("id")

    err(f"No available Jira transition matching '{target_status}' for issue {jira_id}.")


def transition_jira_issue(jira_id, target_status):
    """Transition Jira issue dynamically by state name."""
    transition_id = get_jira_transition_id(jira_id, target_status)
    url = f"{JIRA_URL}/rest/api/2/issue/{jira_id}/transitions"
    payload = {"transition": {"id": str(transition_id)}}

    resp = requests.post(url, headers=JIRA_HEADERS, json=payload, timeout=30)
    if resp.status_code == 204:
        info(f"Jira {jira_id} transitioned to '{target_status}'")
        return True
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Step 3: OpenSearch GM2 Results (Dual-Field Match)
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
                                {"match_phrase": {"x_ray_id": tc_id}},
                                {"match_phrase": {"test_case_id": tc_id}},
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
        try:
            rerun_count = int(src.get("rerun_count", 0))
        except (ValueError, TypeError):
            rerun_count = 0

        is_cbb = src.get("isCBB", False)
        if isinstance(is_cbb, str):
            is_cbb = is_cbb.lower() == "true"

        if rerun_count != 0 or is_cbb:
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


# ---------------------------------------------------------------------------
# Step 5: Cherry-pick Dry Run & Branch Operations
# ---------------------------------------------------------------------------

def fetch_mr_commits(project_id_encoded, mr_iid):
    return gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/commits")


def check_existing_backport_mr(project_id_encoded, backport_branch, target_branch):
    mrs = gitlab_get(
        f"/projects/{project_id_encoded}/merge_requests",
        params={"state": "opened", "target_branch": target_branch, "per_page": 100},
    )
    for mr in mrs:
        if mr.get("source_branch") == backport_branch:
            return mr
    return None


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
    return base


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
        resp.raise_for_status()
        url_id = resp.json().get("urlId")
        if url_id:
            return f"https://autoinfra-es.vaultdev.com/_dashboards/goto/{url_id}"
    except Exception as e:
        warn(f"URL shortening failed ({type(e).__name__}): {e}")
    return f"https://autoinfra-es.vaultdev.com/_dashboards{path}"


# ---------------------------------------------------------------------------
# Step 7: Jira Comment Payload Builder
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

    gm2_links = []
    for tc_id in tc_ids:
        path = build_dashboard_url(tc_id)
        short_url = shorten_dashboard_url(path)
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
# Main Execution Pipeline
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GitLab/Jira/GM2 Backport Automation")
    parser.add_argument("--mr", required=True, type=int, help="Develop MR IID")
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
    args = parser.parse_args()

    missing = [v for v in ["GITLAB_TOKEN", "JIRA_PAT"] if not os.environ.get(v)]
    if missing:
        err(f"Missing required environment variables: {', '.join(missing)}")

    project_path = "veevavault/vaultautomationtests"
    project_id_encoded = project_path.replace("/", "%2F")

    # Step 1: MR Metadata & ID Resolution
    mr = fetch_mr(project_id_encoded, args.mr)
    mr_title = mr.get("title", "")
    mr_description = mr.get("description", "") or ""
    mr_author = mr.get("author", {}).get("username", "unknown")
    mr_web_url = mr.get("web_url", "")
    merged_at = mr.get("merged_at")
    head_sha = mr.get("sha") or mr.get("diff_refs", {}).get("head_sha")

    jira_id = extract_jira_id(mr_title)
    tc_ids, xray_ids, id_source, diff_details = resolve_mr_test_case_ids(
        project_id_encoded, args.mr, mr_description, head_sha=head_sha
    )
    all_tc_ids = tc_ids + xray_ids

    # Handle --inspect-ids inspection mode
    if args.inspect_ids:
        print("\n" + "=" * 60)
        print(f"  TEST CASE INSPECTION REPORT FOR MR !{args.mr}")
        print("=" * 60)
        print(f"MR Title: {mr_title}")
        print(f"Jira ID:  {jira_id}")
        print(f"Source:   {id_source.upper()}")

        if diff_details:
            print("\nMatched Scenario Blocks / Lines:")
            print("-" * 60)
            for item in diff_details:
                print(f"File: {item['file']}")
                print(f"  Line:   {item['line']}")
                if item['tc_ids']:
                    print(f"  TCs:    {', '.join(item['tc_ids'])}")
                if item['xray_ids']:
                    print(f"  Xrays:  {', '.join(item['xray_ids'])}")
                print("-" * 60)

        print("\nExtracted Identifiers:")
        print(f"  Test Case IDs ({len(tc_ids)}): {', '.join(tc_ids) or 'None'}")
        print(f"  Xray IDs      ({len(xray_ids)}): {', '.join(xray_ids) or 'None'}")
        print("=" * 60 + "\n")
        sys.exit(0)

    # Continue Backport Workflow
    if not merged_at:
        err(f"MR !{args.mr} has not been merged yet.")
    merged_at_iso = merged_at if merged_at.endswith("Z") else merged_at + "Z"

    print("\n" + "=" * 60)
    print("  BACKPORT ELIGIBILITY REPORT")
    print("=" * 60)
    print(f"\nMR:            !{args.mr}")
    print(f"Title:         {mr_title}")
    print(f"Author:        {mr_author}")
    print(f"Merged At:     {merged_at_iso}")
    print(f"Jira:          {jira_id}")
    print(f"ID Source:     {id_source.upper()}")
    print(f"Test Cases:    {', '.join(tc_ids) or 'none'}")
    print(f"Xray IDs:      {', '.join(xray_ids) or 'none'}")

    # Step 2: Jira Metadata
    jira_issue = fetch_jira_issue(jira_id)
    fix_versions = get_fix_versions(jira_issue)
    caused_by_jira = get_caused_by_jira(jira_issue)

    info(f"Fix Versions found: {', '.join(fix_versions)}")

    jira_branches = []
    for fv in fix_versions:
        try:
            _, b = parse_fix_version(fv)
            jira_branches.append(b)
        except SystemExit:
            pass

    if args.target_branch:
        target_branch = args.target_branch
        version_str = target_branch.replace("release/", "")
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
        for fv in fix_versions:
            vs, tb = parse_fix_version(fv)
            versions_to_process.append((fv, vs, tb))

    # Step 3 & 4: GM2 Runs + Eligibility
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

    # Step 5: Cherry-pick Dry Run
    commits = fetch_mr_commits(project_id_encoded, args.mr)
    commits_ordered = list(reversed(commits))

    print(f"\nCommits to cherry-pick ({len(commits_ordered)}):")
    for c in commits_ordered:
        print(f"  {c['id'][:8]} - {c['title']}")

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
            "\nDry run complete. Run with --execute to create backport MR and post Jira comment."
        )
        print("=" * 60 + "\n")
        return

    # Phase 2 Execution Loop
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
            create_backport_branch(project_id_encoded, backport_branch, target_branch)

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

        # Post Jira Comment
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

        # Dynamic Jira Workflow Transition
        current_status = get_jira_status(jira_id)
        info(f"Current Jira status: {current_status}")

        if current_status == "Running on GM2":
            info(f"Transitioning {jira_id}: Running on GM2 → GM Data Creation ...")
            transition_jira_issue(jira_id, "GM Data Creation")
            info(f"Transitioning {jira_id}: GM Data Creation → MR to GM ...")
            transition_jira_issue(jira_id, "MR to GM")
        elif current_status == "GM Data Creation":
            info(f"Transitioning {jira_id}: GM Data Creation → MR to GM ...")
            transition_jira_issue(jira_id, "MR to GM")
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