import requests
from clients.jira import jira_get
from config import JIRA_URL, get_jira_headers
from utils.log import err, info


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

    resp = requests.post(url, headers=get_jira_headers(), json=payload, timeout=30)
    if resp.status_code == 204:
        info(f"Jira {jira_id} transitioned to '{target_status}'")
        return True
    resp.raise_for_status()
