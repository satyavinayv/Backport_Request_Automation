import requests
from config import JIRA_URL, get_jira_headers
from utils.log import err


def _connection_err(exc):
    err(
        f"Cannot connect to Jira — check your VPN connection and that "
        f"JIRA_URL / JIRA_TOKEN are set correctly.\nDetail: {exc}"
    )


def jira_get(path):
    url = f"{JIRA_URL}{path}"
    try:
        resp = requests.get(url, headers=get_jira_headers(), timeout=30)
    except requests.exceptions.ConnectionError as exc:
        _connection_err(exc)
    resp.raise_for_status()
    return resp.json()


def jira_post_comment(issue_key, body):
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/comment"
    try:
        resp = requests.post(url, headers=get_jira_headers(), json={"body": body}, timeout=30)
    except requests.exceptions.ConnectionError as exc:
        _connection_err(exc)
    resp.raise_for_status()
    return resp.json()
