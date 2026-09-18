import re
import requests
from clients.jira import jira_get
from utils.log import err, info


def fetch_jira_issue(jira_id):
    info(f"Fetching Jira issue {jira_id} ...")
    try:
        return jira_get(f"/rest/api/2/issue/{jira_id}")
    except requests.exceptions.HTTPError as e:
        err(f"Failed to fetch Jira issue {jira_id}: {e}")


def fetch_jira_issue_safe(jira_id):
    """Fetch a Jira issue without exiting on error — returns None if not found or inaccessible."""
    info(f"Fetching Jira issue {jira_id} (secondary) ...")
    try:
        return jira_get(f"/rest/api/2/issue/{jira_id}")
    except Exception:
        return None


def get_fix_versions(jira_issue):
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


def parse_fix_version(version_str):
    """Convert '26R2.3' -> ('26.2.3', 'release/26.2.3')"""
    match = re.match(r"(\d+)R(\d+)\.(\d+)", version_str)
    if not match:
        err(f"Fix Version '{version_str}' does not match expected pattern (e.g. 26R2.3).")
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{patch}", f"release/{major}.{minor}.{patch}"
