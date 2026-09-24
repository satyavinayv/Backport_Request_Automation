import re
from utils.log import err, info, warn
from clients.gitlab import gitlab_get
from features.id_extraction.diff_scanner import scan_added_lines_for_ids
from features.id_extraction.scenario_parser import scan_scenario_blocks


def extract_jira_id(mr_title):
    match = re.search(r"[A-Z]+-\d+", mr_title)
    if not match:
        err(
            f"No Jira ID found in MR title: '{mr_title}'\n"
            "  Expected pattern like QA-545535 in the title."
        )
    return match.group(0)


def extract_all_jira_ids(mr_title, mr_description=None):
    """
    Return every distinct Jira project ID found in the MR title and description,
    primary (title) first.

    Only extracts IDs from description lines that are NOT test-ID declaration lines
    (i.e. lines starting with 'Test Cases:' or 'Xray IDs:' are skipped so that
    DEV-XXXXXX Xray identifiers are not treated as Jira project issues).
    """
    seen = set()
    ids = []

    def _add_from(text):
        for m in re.finditer(r"\b[A-Z]{2,}-\d+\b", text or ""):
            key = m.group(0)
            if key not in seen:
                seen.add(key)
                ids.append(key)

    _add_from(mr_title)

    for line in (mr_description or "").splitlines():
        if re.match(r"^\s*(Test Cases|Xray IDs)\s*:", line, re.IGNORECASE):
            continue  # skip test-ID declaration lines
        _add_from(line)

    return ids


def extract_test_case_ids(description):
    """Parse TC / Xray IDs from MR description text. Returns ([], []) when not found."""
    tc_ids = []
    xray_ids = []

    if description:
        tc_match = re.search(r"Test Cases:\s*(.+)", description, re.IGNORECASE)
        if tc_match:
            tc_ids = [t.strip() for t in tc_match.group(1).split(",") if t.strip()]

        xr_match = re.search(r"Xray IDs:\s*(.+)", description, re.IGNORECASE)
        if xr_match:
            xray_ids = [t.strip() for t in xr_match.group(1).split(",") if t.strip()]

    return tc_ids, xray_ids


def extract_ids_from_diff(project_id_encoded, mr_iid, head_sha=None):
    """
    Phase 1: scan +lines in the git patch directly.
    Phase 2: fallback to scenario-block isolation on modified test files.
    """
    info(f"Scanning MR !{mr_iid} code diffs for Test IDs...")
    try:
        mr_changes = gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/changes")
    except Exception as e:
        warn(f"Could not fetch MR changes from GitLab: {e}")
        return [], [], []

    changes = mr_changes.get("changes", [])
    tc_ids, xray_ids, matches_detail = scan_added_lines_for_ids(changes)

    if not tc_ids and not xray_ids and head_sha:
        info("No test IDs found on modified (+) lines. Isolating modified scenario blocks...")
        sc_tcs, sc_xrays, sc_details = scan_scenario_blocks(project_id_encoded, changes, head_sha)
        tc_ids.extend(sc_tcs)
        xray_ids.extend(sc_xrays)
        matches_detail.extend(sc_details)

    return sorted(set(tc_ids)), sorted(set(xray_ids)), matches_detail


def resolve_mr_test_case_ids(project_id_encoded, mr_iid, description, head_sha=None):
    """Primary resolver: code diffs first, MR description as fallback."""
    tc_ids, xray_ids, details = extract_ids_from_diff(project_id_encoded, mr_iid, head_sha)

    if tc_ids or xray_ids:
        info("Extracted Test IDs directly from MR code files.")
        return tc_ids, xray_ids, "diff", details

    info("No test IDs matched in code diffs or scenario blocks. Falling back to MR description...")
    desc_tc_ids, desc_xray_ids = extract_test_case_ids(description)
    if desc_tc_ids or desc_xray_ids:
        return desc_tc_ids, desc_xray_ids, "description", []

    return [], [], "none", []


def get_deleted_feature_ids(changes):
    """
    Extract TC/Xray IDs from .feature files that were DELETED (not modified) in this MR.
    Used to skip GM2 checks for tests that no longer exist, while still recording them.
    Returns (deleted_tc_ids, deleted_xray_ids) — deduplicated sorted lists.
    """
    _TC_RE = re.compile(r"\bTC-\d+\b")
    _XRAY_RE = re.compile(r"\bDEV-\d+\b")
    tc_ids: set = set()
    xray_ids: set = set()
    for change in changes:
        if not change.get("deleted_file"):
            continue
        old_path = change.get("old_path", "")
        if not old_path.endswith(".feature"):
            continue
        for line in change.get("diff", "").splitlines():
            if line.startswith("-") and not line.startswith("---"):
                tc_ids.update(m.group(0) for m in _TC_RE.finditer(line))
                xray_ids.update(m.group(0) for m in _XRAY_RE.finditer(line))
    return sorted(tc_ids), sorted(xray_ids)


def get_non_feature_changed_files(changes):
    """
    Return new_path of all changed, non-deleted, non-.feature files.
    These are candidates for Phase 4 repo-file search.
    """
    result = []
    for change in changes:
        if change.get("deleted_file"):
            continue
        path = change.get("new_path") or change.get("old_path", "")
        if path and not path.endswith(".feature"):
            result.append(path)
    return result
