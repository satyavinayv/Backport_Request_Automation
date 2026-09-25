import requests
from config import GITLAB_URL, get_gitlab_headers
from clients.gitlab import gitlab_post
from utils.log import err, info, warn


class AlreadyAppliedError(Exception):
    """Raised when a commit is already present in the target branch (not a real conflict)."""
    pass


def create_backport_branch(project_id_encoded, backport_branch, target_branch):
    info(f"Creating branch '{backport_branch}' from '{target_branch}' ...")
    payload = {"branch": backport_branch, "ref": target_branch}
    try:
        return gitlab_post(f"/projects/{project_id_encoded}/repository/branches", payload)
    except requests.exceptions.HTTPError as e:
        msg = ""
        try:
            msg = e.response.json().get("message", str(e))
        except Exception:
            msg = str(e)
        if "already exists" in msg.lower():
            from utils.log import warn as _warn
            _warn(f"Branch '{backport_branch}' already exists — reusing for re-created MR.")
            return None
        err(f"Failed to create branch '{backport_branch}': {msg}")


def cherry_pick_commit(project_id_encoded, commit_sha, backport_branch):
    info(f"  Cherry-picking {commit_sha[:8]} onto '{backport_branch}' ...")
    payload = {"branch": backport_branch}
    url = f"{GITLAB_URL}/api/v4/projects/{project_id_encoded}/repository/commits/{commit_sha}/cherry_pick"
    # Uses raw requests.post (not gitlab_post) intentionally: the cherry-pick endpoint returns
    # HTTP 400 on a conflict, and we must read the response body to detect it. gitlab_post calls
    # raise_for_status() immediately, which would throw before we can inspect the 400 body.
    try:
        resp = requests.post(url, headers=get_gitlab_headers(), json=payload, timeout=30)
    except requests.exceptions.ConnectionError as exc:
        err(f"Cannot connect to GitLab — check your VPN and GITLAB_URL/GITLAB_TOKEN.\nDetail: {exc}")
    if resp.status_code == 400:
        data = resp.json()
        msg = data.get("message", str(data))
        # "already been done" must be checked FIRST — this message also contains "cherry-pick"
        # and would otherwise be misclassified as a real merge conflict.
        if "already been done" in msg.lower() or "already applied" in msg.lower():
            raise AlreadyAppliedError(msg)
        if "conflict" in msg.lower() or "cherry-pick" in msg.lower():
            err(
                f"Cherry-pick conflict on commit {commit_sha[:8]}.\n"
                f"  Message: {msg}\n"
                "  Manual resolution required. Backport MR NOT created."
            )
        err(f"Cherry-pick failed for {commit_sha[:8]}: {msg}")
    resp.raise_for_status()
    return resp.json()


def delete_branch(project_id_encoded, branch_name):
    url = (
        f"{GITLAB_URL}/api/v4/projects/{project_id_encoded}"
        f"/repository/branches/{requests.utils.quote(branch_name, safe='')}"
    )
    try:
        resp = requests.delete(url, headers=get_gitlab_headers(), timeout=30)
    except requests.exceptions.ConnectionError as exc:
        err(f"Cannot connect to GitLab — check your VPN and GITLAB_URL/GITLAB_TOKEN.\nDetail: {exc}")
    if not resp.ok:
        from utils.log import warn as _warn
        _warn(
            f"Failed to delete branch '{branch_name}' (HTTP {resp.status_code}). "
            "The branch may still exist — delete it manually before re-running to avoid conflicts."
        )
