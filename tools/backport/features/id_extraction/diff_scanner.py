import re


# ---------------------------------------------------------------------------
# Shared ID extraction helpers (used by both diff_scanner and scenario_parser)
# ---------------------------------------------------------------------------

def _extract_tc_ids(text):
    """Extract all TC IDs from any text — annotation and bare patterns, multi-ID aware."""
    ids = set()
    # @TestCase: TC-1234, TC-5678  (multi-value annotation — [ \t] avoids cross-line greed)
    for match in re.finditer(r"@TestCase:\s*([A-Za-z0-9_, \t-]+)", text, re.IGNORECASE):
        for raw in match.group(1).split(","):
            raw = raw.strip()
            if re.match(r"^[A-Za-z0-9_-]+$", raw):
                ids.add(raw.upper())
    # Bare TC-1234 or TC_1234 (including inside Cucumber table rows)
    for raw in re.findall(r"\bTC[_-]\d+\b", text, re.IGNORECASE):
        ids.add(raw.replace("_", "-").upper())
    return sorted(ids)


def _extract_xray_ids(text):
    """Extract all Xray IDs from any text — annotation and bare patterns, multi-ID aware."""
    ids = set()
    # @Xray: XR-1234, DEV-5678  (multi-value annotation — [ \t] avoids cross-line greed)
    for match in re.finditer(r"@Xray(?:ID)?:\s*([A-Za-z0-9_, \t-]+)", text, re.IGNORECASE):
        for raw in match.group(1).split(","):
            raw = raw.strip()
            if re.match(r"^[A-Za-z0-9_-]+$", raw):
                ids.add(raw.replace("_", "-").upper())
    # Bare XR-1234 or DEV-123456 — skip Gherkin/diff comment lines to avoid treating
    # DEV-XXXXXX Jira references in # TODO comments as Xray test IDs.
    non_comment_text = "\n".join(
        line for line in text.splitlines()
        if not re.match(r"^[+\- ]?\s*#", line)
    )
    for raw in re.findall(r"\b(?:XR|DEV)[_-]\d+\b", non_comment_text, re.IGNORECASE):
        ids.add(raw.replace("_", "-").upper())
    return sorted(ids)


# ---------------------------------------------------------------------------
# Phase 1: scan only +lines in the git patch
# ---------------------------------------------------------------------------

def scan_added_lines_for_ids(changes):
    """Scan newly added (+) lines in each file diff for TC / Xray IDs."""
    tc_ids = []
    xray_ids = []
    matches_detail = []

    for change in changes:
        file_path = change.get("new_path", "unknown")
        diff_text = change.get("diff", "")
        if not diff_text:
            continue

        for line in diff_text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue

            found_tcs = _extract_tc_ids(line)
            found_xrays = _extract_xray_ids(line)

            if found_tcs or found_xrays:
                matches_detail.append({
                    "file": file_path,
                    "line": line.strip(),
                    "tc_ids": found_tcs,
                    "xray_ids": found_xrays,
                })
                tc_ids.extend(found_tcs)
                xray_ids.extend(found_xrays)

    return tc_ids, xray_ids, matches_detail


def parse_diff_modified_lines(diff_text):
    """Return 1-indexed line numbers in the new file that were added/modified (+)."""
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
            pass  # deleted lines don't advance the new-file line counter
        else:
            current_new_line += 1

    return modified_lines
