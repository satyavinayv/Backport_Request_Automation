"""
Phase 3 ID extraction: parse test IDs and scenario names from the Jira description,
then optionally query OpenSearch by scenario name to resolve any remaining IDs.

Triggered only when Phase 1 (diff scan) and Phase 2 (scenario block scan) both return
no IDs — typically when the MR only modifies non-test helper files.

Handles three Jira description table formats:
  1. Jira wiki markup  — "|| Test ID/Xray ID || Scenario/Method ||" / "| value | value |"
  2. Tab-separated     — columns separated by \t
  3. Multi-space       — columns separated by 4+ consecutive spaces

Also handles structured label lines:
  "Scenario: <name>", "Feature: <name>", "TC ID: <id>"

Primary path: extract IDs directly from the "Test ID/Xray ID" column.
Fallback path: collect scenario names and query OpenSearch match_phrase on 'scenario'.
"""
import re
from clients.opensearch import opensearch_query
from utils.log import info, warn


# ---------------------------------------------------------------------------
# Table row splitting — handles wiki markup, tabs, and multi-space
# ---------------------------------------------------------------------------

def _split_row(line):
    """Split a table row into cells regardless of separator style."""
    stripped = line.strip()
    # Jira wiki markup: ||header|| or |data|
    if stripped.startswith("||") or (stripped.startswith("|") and stripped.endswith("|")):
        parts = re.split(r"\|{1,2}", stripped)
        return [p.strip() for p in parts if p.strip()]
    # Tab-separated
    if "\t" in line:
        return [p.strip() for p in line.split("\t") if p.strip()]
    # Multi-space (4+ spaces between tokens)
    parts = re.split(r" {4,}", stripped)
    return [p.strip() for p in parts if p.strip()]


def _looks_like_table_header(line):
    """True when a line appears to be a multi-column header in any of the three formats."""
    stripped = line.strip()
    if stripped.startswith("||"):
        return True
    if "\t" in line and line.count("\t") >= 2:
        return True
    # Multi-space: at least 3 tokens after splitting on 4+ spaces
    return len(re.split(r" {4,}", stripped)) >= 3


# ---------------------------------------------------------------------------
# ID extraction helpers for table cells
# ---------------------------------------------------------------------------

def _extract_ids_from_cell(cell_text):
    """Extract all recognisable test IDs from a single table cell value."""
    tc_ids = set()
    xray_ids = set()
    # DEV-XXXXXX / XR-XXXXXX → Xray
    for m in re.findall(r"\b(?:DEV|XR)[_-]\d+\b", cell_text, re.IGNORECASE):
        xray_ids.add(m.replace("_", "-").upper())
    # TC-XXXX / TC_XXXX → TC
    for m in re.findall(r"\bTC[_-]\d+\b", cell_text, re.IGNORECASE):
        tc_ids.add(m.replace("_", "-").upper())
    # Bare numeric IDs ≥5 digits (TestRail).  (?<!-) prevents matching digits inside DEV-XXXXX.
    # These are candidates only — callers should validate against OpenSearch before trusting them.
    for m in re.findall(r"(?<!-)\b\d{5,}\b", cell_text):
        tc_ids.add(m)
    return sorted(tc_ids), sorted(xray_ids)


# ---------------------------------------------------------------------------
# Main description parser
# ---------------------------------------------------------------------------

def _detect_table_format(line):
    """Return the row format: 'wiki', 'tab', or 'space'."""
    stripped = line.strip()
    if stripped.startswith("||"):
        return "wiki"
    if "\t" in line:
        return "tab"
    return "space"


def _is_valid_body_row(line, table_format, header_col_count):
    """
    Return True when a line qualifies as a body row for the detected table format.

    Wiki   — must start with a single '|' (not '||')
    Tab    — must contain at least one tab character
    Space  — multi-space token count must be within ±1 of the header's column count
             (eliminates free-text / stack-trace lines that happen to have large numbers)
    """
    stripped = line.strip()
    if not stripped:
        return False
    if table_format == "wiki":
        return stripped.startswith("|") and not stripped.startswith("||")
    if table_format == "tab":
        return "\t" in line
    # space format — column count must be within ±1 of the header
    # (eliminates free-text lines with incidentally large numbers that have wrong column counts)
    tokens = [p for p in re.split(r" {4,}", stripped) if p.strip()]
    return header_col_count - 1 <= len(tokens) <= header_col_count + 1


def parse_description_for_table_data(description):
    """
    Scan a Jira description for failure-report tables and structured labels.

    Returns (tc_ids, xray_ids, scenario_names):
      - tc_ids / xray_ids  — extracted directly from the "Test ID/Xray ID" column
      - scenario_names     — from the "Scenario/Method" column (for OpenSearch fallback)

    Format locking: once the header's row style is detected (wiki / tab / space) only
    rows of the same style are accepted as body rows. A mismatch ends the current table
    rather than letting free-text lines (stack traces, manifest IDs, etc.) be parsed as
    data rows and produce false-positive test IDs.
    """
    tc_ids: set = set()
    xray_ids: set = set()
    scenario_names = []
    seen_scenarios: set = set()

    lines = description.splitlines()
    col_test_id = -1
    col_scenario = -1
    in_table = False
    table_format = "space"
    header_col_count = 0

    def _reset_table():
        nonlocal in_table, col_test_id, col_scenario, table_format, header_col_count
        in_table = False
        col_test_id = -1
        col_scenario = -1
        table_format = "space"
        header_col_count = 0

    def _add_scenario(s):
        s = s.strip()
        if s and s not in seen_scenarios:
            seen_scenarios.add(s)
            scenario_names.append(s)

    for line in lines:
        stripped = line.strip()

        # --- Structured label: "Scenario: <name>" ---
        m = re.match(r"^Scenario:\s*(.+)$", stripped, re.IGNORECASE)
        if m:
            _add_scenario(m.group(1))

        # --- Table header detection ---
        if not in_table:
            has_test_id_col = bool(re.search(r"test\s*id|xray\s*id", stripped, re.IGNORECASE))
            has_scenario_col = "scenario" in stripped.lower()

            if (has_test_id_col or has_scenario_col) and _looks_like_table_header(line):
                cols = _split_row(line)
                for j, col in enumerate(cols):
                    col_l = col.lower()
                    if re.search(r"test\s*id|xray\s*id", col_l) and col_test_id < 0:
                        col_test_id = j
                    if "scenario" in col_l and col_scenario < 0:
                        col_scenario = j
                table_format = _detect_table_format(line)
                header_col_count = len(cols)
                in_table = True
            continue

        # --- Inside table body ---
        if not stripped:
            _reset_table()
            continue

        # Format-locked validation — a non-matching row ends the table
        if not _is_valid_body_row(line, table_format, header_col_count):
            _reset_table()
            continue

        cells = _split_row(line)
        if not cells:
            continue

        if col_test_id >= 0 and col_test_id < len(cells):
            t, x = _extract_ids_from_cell(cells[col_test_id])
            tc_ids.update(t)
            xray_ids.update(x)

        if col_scenario >= 0 and col_scenario < len(cells):
            _add_scenario(cells[col_scenario])

    return sorted(tc_ids), sorted(xray_ids), scenario_names


# ---------------------------------------------------------------------------
# OpenSearch validation — filters bare numeric candidates from table cells
# ---------------------------------------------------------------------------

def _validate_numeric_ids(raw_tc_ids):
    """
    Validate bare numeric TC IDs against OpenSearch.

    Formatted IDs (starting with a letter, e.g. TC-1234) are kept as-is.
    Bare numerics are only included when OpenSearch has at least one record
    where test_case_id matches — confirming they are real TestRail IDs and
    not pipeline/manifest/build numbers that happen to appear in Jira tables.

    On OpenSearch error the ID is kept (fail-open: do not silently drop a
    potentially valid ID because of a transient network issue).
    """
    validated = []
    for tc in raw_tc_ids:
        if re.match(r"^[A-Za-z]", tc):
            validated.append(tc)  # TC-XXXX / DEV-XXXX style — always keep
            continue
        payload = {"size": 1, "query": {"term": {"test_case_id": tc}}}
        try:
            data = opensearch_query(payload)
            if data.get("hits", {}).get("hits", []):
                validated.append(tc)
            else:
                warn(f"Phase 3: Numeric ID '{tc}' has no OpenSearch records "
                     "— likely a build/manifest number, not a TC ID. Skipping.")
        except Exception:
            warn(f"Phase 3: OpenSearch unreachable — dropping numeric ID '{tc}' to avoid false positives. "
                 "Verify this ID manually if it is a real TC ID.")
            # fail-closed: never accept an unvalidated bare numeric ID
    return validated


# ---------------------------------------------------------------------------
# OpenSearch lookup by scenario name (fallback when table has no direct IDs)
# ---------------------------------------------------------------------------

def _fetch_ids_by_scenario(scenario_name):
    """Query OpenSearch for a scenario name and return (tc_ids, xray_ids) from hits."""
    payload = {
        "size": 5,
        "_source": ["test_case_id", "x_ray_id"],
        "query": {"match_phrase": {"scenario": scenario_name}},
    }
    try:
        data = opensearch_query(payload)
    except Exception:
        return [], []

    hits = data.get("hits", {}).get("hits", [])
    tc_ids, xray_ids = set(), set()

    for hit in hits:
        src = hit.get("_source", {})
        tc = src.get("test_case_id", "").strip()
        xr = src.get("x_ray_id", "").strip()
        if tc:
            tc_ids.add(tc.replace("_", "-").upper())
        if xr:
            xray_ids.add(xr.replace("_", "-").upper())

    if len(tc_ids) + len(xray_ids) > 1:
        label = scenario_name if len(scenario_name) <= 70 else scenario_name[:67] + "..."
        all_ids = ", ".join(sorted(tc_ids | xray_ids))
        warn(
            f"Phase 3: Scenario '{label}' matched {len(tc_ids) + len(xray_ids)} test ID(s): {all_ids}\n"
            "  Multiple tests share this scenario name — verify the correct IDs manually.\n"
            "  Add the correct IDs to the MR description (Xray IDs: DEV-XXXXX) to avoid ambiguity."
        )

    return sorted(tc_ids), sorted(xray_ids)


# ---------------------------------------------------------------------------
# Phase 3 entry point
# ---------------------------------------------------------------------------

def extract_ids_via_jira_scenarios(jira_issue):
    """
    Phase 3 entry point.

    1. Parse the Jira description for direct IDs (Test ID/Xray ID column) and
       scenario names.
    2. If direct IDs were found, return them immediately — no OpenSearch needed.
    3. Otherwise fall back to querying OpenSearch by scenario name.

    Returns (tc_ids, xray_ids, matches_detail).
    """
    fields = (jira_issue.get("fields") if isinstance(jira_issue, dict) else {}) or {}
    description = fields.get("description") or ""

    if not description:
        warn("Phase 3: Jira description is empty — cannot extract test IDs.")
        return [], [], []

    direct_tcs, direct_xrays, scenarios = parse_description_for_table_data(description)

    # Bare numeric TC IDs from the table are candidates only — validate against
    # OpenSearch to filter out pipeline/build/manifest numbers that are not real
    # TestRail IDs (e.g. 1000271 appearing in a Jira pipeline-report table).
    # Formatted IDs (TC-XXXX) and Xray IDs (DEV-/XR-) need no such check.
    if direct_tcs:
        direct_tcs = _validate_numeric_ids(direct_tcs)

    # Primary path: IDs were in the table directly (and survived validation)
    if direct_tcs or direct_xrays:
        info(f"Phase 3: Extracted {len(direct_tcs)} TC ID(s) and {len(direct_xrays)} Xray ID(s) "
             "directly from Jira description table.")
        matches_detail = [{
            "file": "[JIRA DESCRIPTION — DIRECT TABLE EXTRACTION]",
            "line": "Test ID/Xray ID column",
            "tc_ids": direct_tcs,
            "xray_ids": direct_xrays,
        }]
        return direct_tcs, direct_xrays, matches_detail

    # Fallback: no direct IDs — try OpenSearch via scenario names
    if not scenarios:
        warn("Phase 3: No test IDs or scenario names found in Jira description.")
        return [], [], []

    info(f"Phase 3: No direct IDs found. Querying OpenSearch for {len(scenarios)} scenario(s)...")

    tc_ids: set = set()
    xray_ids: set = set()
    matches_detail = []

    for scenario in scenarios:
        label = scenario if len(scenario) <= 70 else scenario[:67] + "..."
        info(f"  Scenario: '{label}'")
        found_tcs, found_xrays = _fetch_ids_by_scenario(scenario)

        if found_tcs or found_xrays:
            tc_ids.update(found_tcs)
            xray_ids.update(found_xrays)
            matches_detail.append({
                "file": "[JIRA DESCRIPTION — SCENARIO LOOKUP]",
                "line": scenario,
                "tc_ids": found_tcs,
                "xray_ids": found_xrays,
            })
        else:
            warn(f"  No OpenSearch hits for this scenario.")

    return sorted(tc_ids), sorted(xray_ids), matches_detail
