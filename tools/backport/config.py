import os

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.veevadev.com")
JIRA_URL = os.environ.get("JIRA_URL", "https://jira.veevadev.com")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "https://autoinfra-es.vaultdev.com:9200")

MANAGER_NAME = os.environ.get("BACKPORT_MANAGER_EMAIL", "vinil.pokala@veeva.com")
PROJECT_PATH = os.environ.get("GITLAB_PROJECT_PATH", "veevavault/vaultautomationtests")
PROJECT_ID_ENCODED = PROJECT_PATH.replace("/", "%2F")

# Comma-separated GitLab usernames to add as reviewers on every backport MR.
# Can also be extended at runtime with --reviewers CLI flag.
BACKPORT_REVIEWERS = os.environ.get("BACKPORT_REVIEWERS", "")

# Comma-separated labels to add on every backport MR (in addition to the
# original MR labels and the fixed "backport" label).
# Can also be extended at runtime with --labels CLI flag.
BACKPORT_LABELS = os.environ.get("BACKPORT_LABELS", "")

# Comma-separated GitLab project names (last path component of GITLAB_PROJECT_PATH)
# that contain pipeline/config files only — GM2 checks are never applicable for these.
# Example: BACKPORT_PIPELINE_PROJECTS=automation-platform-pipelines,infra-pipelines
_PIPELINE_PROJECT_NAMES = {
    p.strip().lower()
    for p in os.environ.get("BACKPORT_PIPELINE_PROJECTS", "automation-platform-pipelines").split(",")
    if p.strip()
}


def is_pipeline_project():
    """Return True when the configured project is a known pipeline/config repo."""
    last_component = PROJECT_PATH.rstrip("/").split("/")[-1].lower()
    return last_component in _PIPELINE_PROJECT_NAMES


def pipeline_project_name():
    """Return just the last path component of PROJECT_PATH (the repo name)."""
    return PROJECT_PATH.rstrip("/").split("/")[-1]


def get_gitlab_headers():
    """Read token fresh from env each call — safe even if env is set after module import."""
    return {
        "PRIVATE-TOKEN": os.environ.get("GITLAB_TOKEN", ""),
        "Content-Type": "application/json",
    }


def get_jira_headers():
    """Read token fresh from env each call — safe even if env is set after module import."""
    return {
        "Authorization": f"Bearer {os.environ.get('JIRA_PAT', '')}",
        "Content-Type": "application/json",
    }
