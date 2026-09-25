import re
from clients.gitlab import fetch_raw_file_content
from features.id_extraction.diff_scanner import parse_diff_modified_lines, _extract_tc_ids, _extract_xray_ids


def _extract_scenario_name(line):
    """Extract title text from a Scenario: or Scenario Outline: header line."""
    m = re.match(r"^\s*(?:Scenario Outline|Scenario):\s*(.*)$", line, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _is_scenario_outline(line):
    return bool(re.match(r"^\s*Scenario Outline:", line, re.IGNORECASE))


def _parse_examples_sections(lines, block_start_idx, block_end_idx):
    """
    Locate all Examples: blocks within a Scenario Outline and return per-section metadata.

    Each section dict contains:
      tag_line_idxs  — 0-based indices of @tag lines immediately before the Examples: keyword.
                       These are per-Examples-section tags (e.g. @TestCase:537475183 placed
                       directly above an Examples block rather than above the Scenario Outline).
      rows           — [(0-based_line_idx, 1-based_row_num), ...] for every data row.
                       Row numbering is continuous across all sections.
    """
    sections = []
    row_num = 0
    i = block_start_idx
    while i <= block_end_idx:
        if re.match(r"^\s*Examples\s*:", lines[i], re.IGNORECASE):
            # Collect @tag lines immediately before this Examples: keyword.
            # Blank lines and comment lines (#...) between the tags and the
            # Examples: keyword are skipped — they don't break the tag chain.
            tag_idxs = []
            j = i - 1
            while j >= block_start_idx:
                stripped = lines[j].strip()
                if stripped.startswith("@"):
                    tag_idxs.insert(0, j)
                    j -= 1
                elif not stripped or stripped.startswith("#"):
                    j -= 1  # blank or comment — keep looking for @ tags
                else:
                    break

            section = {"tag_line_idxs": tag_idxs, "rows": []}
            header_skipped = False
            i += 1
            while i <= block_end_idx:
                stripped = lines[i].strip()
                if stripped.startswith("|"):
                    if not header_skipped:
                        header_skipped = True
                    else:
                        row_num += 1
                        section["rows"].append((i, row_num))
                    i += 1
                elif not stripped or stripped.startswith("#"):
                    i += 1  # blank/comment — stay inside table
                else:
                    break  # non-table content ends this section
            sections.append(section)
            continue
        i += 1
    return sections


def _find_scenario_header_idx(lines, start_idx, end_idx):
    """Return the line index of the Scenario/Scenario Outline keyword within a block."""
    for i in range(start_idx, end_idx + 1):
        if re.match(r"^\s*(Scenario|Scenario Outline):", lines[i], re.IGNORECASE):
            return i
    return start_idx


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
    """
    Isolate only the Scenario/Scenario Outline blocks touched by changed lines.

    For Scenario Outlines, when the modified lines fall inside Examples table body rows:
    - Only TC/Xray IDs from those specific rows (and their parent Examples-section @tags)
      are returned, preventing unrelated rows from polluting GM2 eligibility.
    - The matched_block carries eval_contexts [{tc_id, row}] so backport.py can issue
      one targeted GM2 query per (ID, row) rather than all rows for the ID.
    - Per-Examples @tags (e.g. @TestCase:537475183 placed directly above an Examples:
      block) are captured correctly, which is the standard Gherkin pattern when each
      Examples section within one Scenario Outline has a distinct TC ID.

    If a modified line sits inside a Background block, all scenarios in the file are
    returned — a Background change affects every scenario that uses it.

    Each matched_block includes:
      scenario_name  — text of the Scenario/Scenario Outline title
      is_outline     — True for Scenario Outline blocks
      affected_rows  — list of 1-based row numbers, or None (all rows / not an outline)
      eval_contexts  — [{tc_id, row}] for targeted GM2 queries; row=None means all rows
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

        sc_header_idx = _find_scenario_header_idx(lines, start_idx, end_idx)
        scenario_name = _extract_scenario_name(lines[sc_header_idx])
        is_outline = _is_scenario_outline(lines[sc_header_idx])

        if is_outline:
            sections = _parse_examples_sections(lines, start_idx, end_idx)
            mod_line_set = {m - 1 for m in modified_line_nums}  # 0-based

            # Determine which Examples sections contain modified rows
            affected_row_nums = []
            for sec in sections:
                for (line_idx, row_num) in sec["rows"]:
                    if line_idx in mod_line_set:
                        affected_row_nums.append(row_num)

            if affected_row_nums:
                # Build eval_contexts: one (tc_id, row) per affected section.
                # Each section may have its own @TestCase tag (common Gherkin pattern
                # where one Scenario Outline has multiple Examples blocks, each tagged
                # with a different TC ID — e.g. @TestCase:537475183 before Examples block 1,
                # @TestCase:537475184 before Examples block 2).
                affected_row_set = set(affected_row_nums)
                found_tcs: set = set()
                found_xrays: set = set()
                eval_contexts = []

                for sec in sections:
                    sec_affected = [rn for (_, rn) in sec["rows"] if rn in affected_row_set]
                    if not sec_affected:
                        continue

                    # IDs from this section's own @tags (placed directly before Examples:)
                    sec_tcs: set = set()
                    sec_xrays: set = set()
                    for tag_idx in sec["tag_line_idxs"]:
                        sec_tcs.update(_extract_tc_ids(lines[tag_idx]))
                        sec_xrays.update(_extract_xray_ids(lines[tag_idx]))
                    # IDs from the affected data rows themselves
                    for (line_idx, rn) in sec["rows"]:
                        if rn in affected_row_set:
                            sec_tcs.update(_extract_tc_ids(lines[line_idx]))
                            sec_xrays.update(_extract_xray_ids(lines[line_idx]))

                    for tid in sorted(sec_tcs):
                        for rn in sec_affected:
                            eval_contexts.append({"tc_id": tid, "row": rn})
                    for xid in sorted(sec_xrays):
                        for rn in sec_affected:
                            eval_contexts.append({"tc_id": xid, "row": rn})

                    found_tcs.update(sec_tcs)
                    found_xrays.update(sec_xrays)

                # Shared IDs from the Scenario Outline header/tags (apply to ALL rows)
                tags_and_header = "\n".join(lines[start_idx:sc_header_idx + 1])
                shared_tcs = set(_extract_tc_ids(tags_and_header))
                shared_xrays = set(_extract_xray_ids(tags_and_header))
                for tid in sorted(shared_tcs):
                    for rn in sorted(affected_row_set):
                        eval_contexts.append({"tc_id": tid, "row": rn})
                for xid in sorted(shared_xrays):
                    for rn in sorted(affected_row_set):
                        eval_contexts.append({"tc_id": xid, "row": rn})
                found_tcs.update(shared_tcs)
                found_xrays.update(shared_xrays)

                # Deduplicate eval_contexts while preserving order
                seen_ctx: set = set()
                deduped_ctx = []
                for ctx in eval_contexts:
                    key = (ctx["tc_id"], ctx["row"])
                    if key not in seen_ctx:
                        seen_ctx.add(key)
                        deduped_ctx.append(ctx)

                found_tcs_list = sorted(found_tcs)
                found_xrays_list = sorted(found_xrays)
                affected_rows = sorted(affected_row_nums)
                eval_contexts = deduped_ctx
            else:
                # Modified lines are in steps or tags — all rows are affected.
                block_text = "\n".join(lines[start_idx: end_idx + 1])
                found_tcs_list = _extract_tc_ids(block_text)
                found_xrays_list = _extract_xray_ids(block_text)
                affected_rows = None
                eval_contexts = [
                    {"tc_id": tid, "row": None} for tid in found_tcs_list
                ] + [
                    {"tc_id": xid, "row": None} for xid in found_xrays_list
                ]
        else:
            block_text = "\n".join(lines[start_idx: end_idx + 1])
            found_tcs_list = _extract_tc_ids(block_text)
            found_xrays_list = _extract_xray_ids(block_text)
            affected_rows = None
            eval_contexts = [
                {"tc_id": tid, "row": None} for tid in found_tcs_list
            ] + [
                {"tc_id": xid, "row": None} for xid in found_xrays_list
            ]

        if found_tcs_list or found_xrays_list:
            tc_ids.update(found_tcs_list)
            xray_ids.update(found_xrays_list)
            matched_blocks.append({
                "start_line": start_idx + 1,
                "end_line": end_idx + 1,
                "tc_ids": found_tcs_list,
                "xray_ids": found_xrays_list,
                "scenario_name": scenario_name,
                "is_outline": is_outline,
                "affected_rows": affected_rows,
                "eval_contexts": eval_contexts,
            })

    if background_triggered:
        for sc_start, sc_end in _find_all_scenario_blocks(lines):
            block_key = (sc_start, sc_end)
            if block_key in processed_ranges:
                continue
            processed_ranges.add(block_key)
            block_text = "\n".join(lines[sc_start: sc_end + 1])
            found_tcs_list = _extract_tc_ids(block_text)
            found_xrays_list = _extract_xray_ids(block_text)
            if found_tcs_list or found_xrays_list:
                sc_header_idx = _find_scenario_header_idx(lines, sc_start, sc_end)
                scenario_name = _extract_scenario_name(lines[sc_header_idx])
                is_outline = _is_scenario_outline(lines[sc_header_idx])
                tc_ids.update(found_tcs_list)
                xray_ids.update(found_xrays_list)
                matched_blocks.append({
                    "start_line": sc_start + 1,
                    "end_line": sc_end + 1,
                    "tc_ids": found_tcs_list,
                    "xray_ids": found_xrays_list,
                    "scenario_name": scenario_name,
                    "is_outline": is_outline,
                    "affected_rows": None,  # Background affects all rows
                    "eval_contexts": [
                        {"tc_id": tid, "row": None} for tid in found_tcs_list
                    ] + [
                        {"tc_id": xid, "row": None} for xid in found_xrays_list
                    ],
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
                "scenario_name": b.get("scenario_name"),
                "is_outline": b.get("is_outline", False),
                "affected_rows": b.get("affected_rows"),
                "eval_contexts": b.get("eval_contexts", []),
            })
        tc_ids.extend(sc_tcs)
        xray_ids.extend(sc_xrays)

    return tc_ids, xray_ids, matches_detail
