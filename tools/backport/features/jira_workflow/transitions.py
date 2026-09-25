import requests
from clients.jira import jira_get
from config import JIRA_URL, get_jira_headers
from utils.log import err, info, warn

# Known multi-hop paths to reach "MR to GM" from common intermediate states.
_TRANSITION_PATHS = {
    "Running on GM2": ["GM Data Creation", "MR to GM"],
    "GM Data Creation": ["MR to GM"],
}
_TARGET_STATUS = "MR to GM"


def get_jira_status(jira_id):
    issue = jira_get(f"/rest/api/2/issue/{jira_id}?fields=status")
    return issue.get("fields", {}).get("status", {}).get("name", "")


def get_jira_transition_id(jira_id, target_status):
    data = jira_get(f"/rest/api/2/issue/{jira_id}/transitions")
    transitions = data.get("transitions", [])

    for t in transitions:
        to_name = t.get("to", {}).get("name", "").lower()
        trans_name = t.get("name", "").lower()
        if target_status.lower() in (to_name, trans_name):
            return t.get("id")

    err(f"No available Jira transition matching '{target_status}' for issue {jira_id}.")


def transition_jira_issue(jira_id, target_status):
    transition_id = get_jira_transition_id(jira_id, target_status)
    url = f"{JIRA_URL}/rest/api/2/issue/{jira_id}/transitions"
    payload = {"transition": {"id": str(transition_id)}}

    try:
        resp = requests.post(url, headers=get_jira_headers(), json=payload, timeout=30)
    except requests.exceptions.ConnectionError as exc:
        err(f"Cannot connect to Jira — check your VPN and JIRA_URL/JIRA_PAT.\nDetail: {exc}")
    if resp.status_code == 204:
        info(f"Jira {jira_id} transitioned to '{target_status}'")
        return True
    resp.raise_for_status()


def ensure_transition_to_mr_to_gm(jira_id):
    """
    Transition jira_id to 'MR to GM' regardless of its current state.

    Known paths (Running on GM2, GM Data Creation) use the documented multi-hop
    sequence. For any other state the function attempts a direct single-hop
    transition to 'MR to GM'. If no transition path is available in the current
    workflow, a warning is printed and the user is asked to transition manually
    (so the backport run does not crash due to a Jira workflow configuration).
    """
    current = get_jira_status(jira_id)
    info(f"Current Jira status for {jira_id}: {current}")

    if current == _TARGET_STATUS:
        warn(f"Jira {jira_id} is already in '{_TARGET_STATUS}' — skipping transitions.")
        return

    steps = _TRANSITION_PATHS.get(current)
    if steps is not None:
        for step in steps:
            info(f"Transitioning {jira_id}: → {step} ...")
            transition_jira_issue(jira_id, step)
    else:
        info(
            f"Jira {jira_id} is in '{current}' (unexpected state) — "
            f"attempting direct transition to '{_TARGET_STATUS}' ..."
        )
        try:
            transition_jira_issue(jira_id, _TARGET_STATUS)
        except SystemExit:
            warn(
                f"Could not transition {jira_id} from '{current}' to '{_TARGET_STATUS}'. "
                "No matching transition is available for the current workflow state. "
                "Please transition the issue manually in Jira."
            )
