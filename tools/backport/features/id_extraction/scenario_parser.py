import re
from clients.gitlab import fetch_raw_file_content
from features.id_extraction.diff_scanner import parse_diff_modified_lines, _extract_tc_ids, _extract_xray_ids


def extract_scenario_block_ids(file_content, modified_line_nums):
    """Isolate only the Scenario/Scenario Outline blocks touched by changed lines."""
    lines = file_content.splitlines()
    total_lines = len(lines)
    tc_ids, xray_ids = set(), set()
    matched_blocks = []
    processed_ranges = set()

    for mod_line in modified_line_nums:
        idx = mod_line - 1
        if idx < 0 or idx >= total_lines:
            continue

        # Walk up to find the enclosing Scenario header (including its @tags).
        # BUG FIX: use >= 0 so line index 0 (first line of file) is not skipped.
        start_idx = idx
        while start_idx >= 0:
            if re.match(r"^\s*(Scenario|Scenario Outline):", lines[start_idx], re.IGNORECASE):
                while start_idx > 0 and lines[start_idx - 1].strip().startswith("@"):
                    start_idx -= 1
                break
            start_idx -= 1

        # Walk down to end of block (stop before next Scenario header or its @tags)
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

        block_text = "\n".join(lines[start_idx: end_idx + 1])

        # BUG FIX: use shared helpers — both annotation and bare patterns,
        # additive (not mutually exclusive), multi-ID aware.
        found_tcs = _extract_tc_ids(block_text)
        found_xrays = _extract_xray_ids(block_text)

        if found_tcs or found_xrays:
            tc_ids.update(found_tcs)
            xray_ids.update(found_xrays)
            matched_blocks.append({
                "start_line": start_idx + 1,
                "end_line": end_idx + 1,
                "tc_ids": found_tcs,
                "xray_ids": found_xrays,
            })

    return sorted(tc_ids), sorted(xray_ids), matched_blocks


def scan_scenario_blocks(project_id_encoded, changes, head_sha):
    """Phase 2: fetch full file content and parse scenario blocks for each modified test file."""
    tc_ids = []
    xray_ids = []
    matches_detail = []

    for change in changes:
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
        tc_ids.extend(sc_tcs)
        xray_ids.extend(sc_xrays)

    return tc_ids, xray_ids, matches_detail
