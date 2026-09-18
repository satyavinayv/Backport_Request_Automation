import requests
from config import JIRA_URL, get_jira_headers


def jira_get(path):
    url = f"{JIRA_URL}{path}"
    resp = requests.get(url, headers=get_jira_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def jira_post_comment(issue_key, body):
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/comment"
    resp = requests.post(url, headers=get_jira_headers(), json={"body": body}, timeout=30)
    resp.raise_for_status()
    return resp.json()
