from clients.opensearch import opensearch_query
from utils.log import info

# Fetch enough runs to safely cover any realistic post-merge run count.
# Sort descending (newest first), then reverse so the list is oldest→newest —
# evaluator's results[-2:] then gives the 2 most recent qualifying runs.
_GM2_FETCH_SIZE = 50


def fetch_gm2_runs(tc_id, merged_at_iso, scenario_name=None, row=None):
    """
    Query OpenSearch for GM2 runs of a test case after the MR merge timestamp.

    scenario_name: when set, restricts results to runs whose 'scenario' field matches
        exactly — prevents runs from a different scenario sharing the same TC/Xray ID
        from polluting the eligibility evaluation.
    row: 1-based integer; when set, restricts results to that specific Scenario Outline
        row — prevents rows unrelated to the change from polluting the evaluation.
    """
    ctx_label = _ctx_label(scenario_name, row)
    info(f"  Querying GM2 runs for {tc_id}{ctx_label} after {merged_at_iso} ...")

    filters = [
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
    if scenario_name:
        filters.append({"match_phrase": {"scenario": scenario_name}})
    if row is not None:
        filters.append({"term": {"row": row}})

    payload = {
        "size": _GM2_FETCH_SIZE,
        "_source": {"excludes": []},
        "query": {"bool": {"filter": filters}},
        "sort": [{"@timestamp": {"order": "desc"}}],
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

    # Reverse to oldest→newest so evaluator's results[-2:] picks the 2 most recent.
    runs.reverse()
    return runs


def classify_gm2_absence(tc_id, merged_at_iso, scenario_name=None, row=None):
    """
    Determine why fetch_gm2_runs returned an empty list.
    Returns 'NO_HISTORY'     — zero GM2 runs exist post-merge (new test / new row).
    Returns 'CBB_RERUN_ONLY' — runs exist but every one is a rerun or CBB.

    scenario_name and row are forwarded so the absence check is scoped to the same
    context as fetch_gm2_runs, avoiding incorrect CBB_RERUN_ONLY diagnoses when
    a different scenario or row has runs for the same ID.
    """
    filters = [
        {"match_phrase": {"environment": "GM2"}},
        {"range": {"@timestamp": {"gte": merged_at_iso, "lte": "now"}}},
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
    if scenario_name:
        filters.append({"match_phrase": {"scenario": scenario_name}})
    if row is not None:
        filters.append({"term": {"row": row}})

    payload = {
        "size": 1,
        "track_total_hits": True,
        "query": {"bool": {"filter": filters}},
    }
    data = opensearch_query(payload)
    total = data.get("hits", {}).get("total", {})
    count = total.get("value", 0) if isinstance(total, dict) else total
    return "NO_HISTORY" if count == 0 else "CBB_RERUN_ONLY"


def fetch_gm2_runs_cbb(tc_id, merged_at_iso, cbb_branch, scenario_name=None, row=None):
    """
    Query OpenSearch for GM2 runs filtered by build_branches (CBB branch).
    The isCBB filter is removed entirely — build_branches scopes the results.
    rerun_count=0 still applied to exclude reruns.
    """
    ctx_label = _ctx_label(scenario_name, row)
    info(f"  Querying CBB GM2 runs for {tc_id}{ctx_label} on branch '{cbb_branch}' ...")

    filters = [
        {"match_phrase": {"environment": "GM2"}},
        {"range": {"@timestamp": {"gte": merged_at_iso, "lte": "now"}}},
        {
            "bool": {
                "should": [
                    {"match_phrase": {"x_ray_id": tc_id}},
                    {"match_phrase": {"test_case_id": tc_id}},
                ],
                "minimum_should_match": 1,
            }
        },
        {"match_phrase": {"build_branches": cbb_branch}},
    ]
    if scenario_name:
        filters.append({"match_phrase": {"scenario": scenario_name}})
    if row is not None:
        filters.append({"term": {"row": row}})

    payload = {
        "size": _GM2_FETCH_SIZE,
        "_source": {"excludes": []},
        "query": {"bool": {"filter": filters}},
        "sort": [{"@timestamp": {"order": "desc"}}],
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
        if rerun_count != 0:
            continue
        runs.append({
            "timestamp": src.get("@timestamp"),
            "test_result": src.get("test_result", ""),
            "vault_version": src.get("vault_version", ""),
            "test_case_id": src.get("test_case_id", ""),
            "x_ray_id": src.get("x_ray_id", ""),
            "rerun_count": rerun_count,
            "isCBB": src.get("isCBB", True),
            "build_branches": src.get("build_branches", ""),
            "error_message": src.get("error_message", ""),
            "environment": src.get("environment", ""),
        })
    runs.reverse()
    return runs


def _ctx_label(scenario_name, row):
    """Format a short context label for log messages."""
    parts = []
    if scenario_name:
        name = scenario_name if len(scenario_name) <= 40 else scenario_name[:37] + "..."
        parts.append(f"scenario='{name}'")
    if row is not None:
        parts.append(f"row={row}")
    return f" [{', '.join(parts)}]" if parts else ""
