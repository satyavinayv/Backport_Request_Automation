import re
from clients.gitlab import fetch_raw_file_content
from features.id_extraction.diff_scanner import parse_diff_modified_lines, _extract_tc_ids, _extract_xray_ids


def _find_all_scenario_blocks(lines):
    """Return (start_idx, end_idx) for every Scenario/Scenario Outline block in the file."""
    total_lines = len(lines)
    blocks = []
    i = 0
    while i < total_lines:
        if re.match(r"^\s*(Scenario|Scenario Outline):", lines[i], re.IGNORECASE):
            sc_start = i
            while sc_start > 0 and lines[sc_start - 1].strip().startswith("@"):
                sc_start -= 1
            sc_end = i
            while sc_end < total_lines - 1:
                next_line = lines[sc_end + 1]
                if re.match(r"^\s*(Scenario|Scenario Outline):", next_line, re.IGNORECASE) or \
                   (next_line.strip().startswith("@") and any(
                       re.match(r"^\s*(Scenario|Scenario Outline):", lines[k], re.IGNORECASE)
                       for k in range(sc_end + 1, min(sc_end + 10, total_lines))
                   )):
                    break
                sc_end += 1
            blocks.append((sc_start, sc_end))
            i = sc_end + 1
        else:
            i += 1
    return blocks


def extract_scenario_block_ids(file_content, modified_line_nums):
    """Isolate only the Scenario/Scenario Outline blocks touched by changed lines.

    If a modified line sits inside a Background block (no enclosing Scenario header found
    above it), all scenarios in the file are returned — a Background change affects every
    scenario that uses it.
    """
    lines = file_content.splitlines()
    total_lines = len(lines)
    tc_ids, xray_ids = set(), set()
    matched_blocks = []
    processed_ranges = set()
    background_triggered = False

    for mod_line in modified_line_nums:
        idx = mod_line - 1
        if idx < 0 or idx >= total_lines:
            continue

        # Walk up to find the enclosing Scenario header (including its @tags).
        start_idx = idx
        while start_idx >= 0:
            if re.match(r"^\s*(Scenario|Scenario Outline):", lines[start_idx], re.IGNORECASE):
                while start_idx > 0 and lines[start_idx - 1].strip().startswith("@"):
                    start_idx -= 1
                break
            start_idx -= 1

        if start_idx < 0:
            # No Scenario header found above — modified line is in a Background block.
            # Background steps run before every scenario, so all scenarios are affected.
            background_triggered = True
            continue

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

    if background_triggered:
        for sc_start, sc_end in _find_all_scenario_blocks(lines):
            block_key = (sc_start, sc_end)
            if block_key in processed_ranges:
                continue
            processed_ranges.add(block_key)
            block_text = "\n".join(lines[sc_start: sc_end + 1])
            found_tcs = _extract_tc_ids(block_text)
            found_xrays = _extract_xray_ids(block_text)
            if found_tcs or found_xrays:
                tc_ids.update(found_tcs)
                xray_ids.update(found_xrays)
                matched_blocks.append({
                    "start_line": sc_start + 1,
                    "end_line": sc_end + 1,
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
        if not file_path.endswith((".feature", ".java", ".py", ".groovy")) or not diff_text:
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
