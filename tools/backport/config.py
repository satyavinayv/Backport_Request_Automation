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
