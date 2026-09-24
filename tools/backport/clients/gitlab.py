import urllib.parse
import requests
from config import GITLAB_URL, get_gitlab_headers
from utils.log import err, warn, info


def gitlab_get(path, params=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.get(url, headers=get_gitlab_headers(), params=params, timeout=30)
    if resp.status_code == 404:
        err(f"GitLab 404: {url}")
    resp.raise_for_status()
    return resp.json()


def gitlab_get_raw(path, params=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.get(url, headers=get_gitlab_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def gitlab_post(path, payload):
    url = f"{GITLAB_URL}/api/v4{path}"
    resp = requests.post(url, headers=get_gitlab_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_mr(project_id_encoded, mr_iid):
    info(f"Fetching MR !{mr_iid} ...")
    return gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}")


def fetch_mr_commits(project_id_encoded, mr_iid):
    return gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/commits")


def fetch_mr_file_changes(project_id_encoded, mr_iid):
    """Return the raw list of file-change dicts for an MR (includes deleted_file, new_file, renamed_file, diff)."""
    data = gitlab_get(f"/projects/{project_id_encoded}/merge_requests/{mr_iid}/changes")
    return data.get("changes", [])


def search_blobs(project_id_encoded, query, ref="develop"):
    """
    Search repository file contents via GitLab blob search.
    Returns up to 100 results across paginated calls.
    Each result has: {basename, path, ref, startline, data}
    """
    results = []
    page = 1
    while len(results) < 100:
        batch = gitlab_get(
            f"/projects/{project_id_encoded}/search",
            params={"scope": "blobs", "search": query, "ref": ref, "per_page": 20, "page": page},
        )
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 20:
            break
        page += 1
    return results


def fetch_raw_file_content(project_id_encoded, file_path, ref_sha):
    encoded_path = urllib.parse.quote(file_path, safe="")
    path = f"/projects/{project_id_encoded}/repository/files/{encoded_path}/raw"
    try:
        return gitlab_get_raw(path, params={"ref": ref_sha})
    except Exception as e:
        warn(f"Could not fetch raw file content for {file_path}: {e}")
        return ""


def get_current_user():
    """Return the authenticated GitLab user object (id, username, name)."""
    return gitlab_get("/user")


def resolve_usernames_to_ids(usernames):
    """
    Convert a list of GitLab usernames to their numeric user IDs.
    Unresolvable usernames are warned and skipped.
    """
    user_ids = []
    for username in usernames:
        username = username.strip()
        if not username:
            continue
        try:
            users = gitlab_get("/users", params={"username": username})
            if users:
                user_ids.append(users[0]["id"])
            else:
                warn(f"GitLab user not found: '{username}' — skipping reviewer.")
        except Exception as e:
            warn(f"Could not resolve GitLab user '{username}': {e}")
    return user_ids
