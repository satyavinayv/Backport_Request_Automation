from clients.gitlab import gitlab_get, gitlab_post
from utils.log import info


def check_existing_backport_mr(project_id_encoded, backport_branch, target_branch):
    """
    Check for an existing backport MR on this branch across all states.
    Returns (mr_object, state) where state is 'opened', 'merged', or 'closed'.
    Returns (None, None) if not found.
    """
    for state in ("opened", "merged", "closed"):
        mrs = gitlab_get(
            f"/projects/{project_id_encoded}/merge_requests",
            params={"state": state, "target_branch": target_branch, "per_page": 100},
        )
        for mr in mrs:
            if mr.get("source_branch") == backport_branch:
                return mr, state
    return None, None


def create_mr(
    project_id_encoded,
    source_branch,
    target_branch,
    title,
    description,
    assignee_id=None,
    reviewer_ids=None,
    labels=None,
):
    info(f"Creating Backport MR: '{title}' ...")
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
    }
    if assignee_id:
        payload["assignee_id"] = assignee_id
    if reviewer_ids:
        payload["reviewer_ids"] = reviewer_ids
    if labels:
        # GitLab expects a comma-separated label string
        payload["labels"] = labels if isinstance(labels, str) else ",".join(labels)
    return gitlab_post(f"/projects/{project_id_encoded}/merge_requests", payload)


_EXCLUDED_LABELS = {"SCBA Approved 🤿", "GM2 Fixes", "PeerReviewed"}


def build_label_set(original_mr_labels, env_labels_str, cli_labels_str):
    """
    Combine labels from three sources and always include 'backport'.
      original_mr_labels : list of label strings from the source MR
      env_labels_str     : comma-separated string from BACKPORT_LABELS env var
      cli_labels_str     : comma-separated string from --labels CLI arg
    Returns a sorted comma-separated label string ready for the GitLab API.
    """
    labels = set(original_mr_labels or [])
    labels.add("backport")

    for raw in [env_labels_str, cli_labels_str]:
        if raw:
            for lbl in raw.split(","):
                lbl = lbl.strip()
                if lbl:
                    labels.add(lbl)

    labels -= _EXCLUDED_LABELS
    return ",".join(sorted(labels))
