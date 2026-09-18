from clients.opensearch import opensearch_query
from utils.log import info

# Fetch enough runs to safely cover any realistic post-merge run count.
# Sort descending (newest first), then reverse so the list is oldest→newest —
# evaluator's results[-2:] then gives the 2 most recent qualifying runs.
_GM2_FETCH_SIZE = 50


def fetch_gm2_runs(tc_id, merged_at_iso):
    """Query OpenSearch for GM2 runs of a test case after the MR merge timestamp."""
    info(f"  Querying GM2 runs for {tc_id} after {merged_at_iso} ...")
    payload = {
        "size": _GM2_FETCH_SIZE,
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
        # BUG FIX: sort descending so we always get the MOST RECENT runs first,
        # then reverse before returning so the list is oldest→newest for the evaluator.
        # Previously: ascending + size:10 returned the 10 oldest runs, causing
        # results[-2:] to miss the actual latest runs when >10 existed.
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
