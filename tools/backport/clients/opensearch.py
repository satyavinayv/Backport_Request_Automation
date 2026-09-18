import requests
from config import OPENSEARCH_URL
from utils.log import err


def opensearch_query(payload):
    url = f"{OPENSEARCH_URL}/autoresult-*/_search"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        err(
            "Cannot reach OpenSearch. Ensure you are connected to the company VPN.\n"
            f"  Endpoint: {url}"
        )
