import requests
from utils.log import warn


def build_dashboard_url(tc_id):
    kql_query = f'"{tc_id}"'
    base = (
        "/app/data-explorer/discover/#"
        "?_a=(discover:(columns:!(environment,vault_version,test_case_id,test_result,"
        "rerun_count,isCBB,error_message,feature,scenario,row),isDirty:!f,sort:!()),"
        "metadata:(indexPattern:'autoresult-*',view:discover))"
        "&_g=(filters:!(),refreshInterval:(pause:!t,value:0),time:(from:now-1y,to:now))"
        "&_q=(filters:!(('$state':(store:appState),meta:(alias:!n,disabled:!f,"
        "index:'autoresult-*',key:isCBB,negate:!f,params:(query:!f),type:phrase),"
        "query:(match_phrase:(isCBB:!f))),('$state':(store:appState),meta:(alias:!n,"
        "disabled:!f,index:'autoresult-*',key:rerun_count,negate:!f,params:(query:'0'),"
        "type:phrase),query:(match_phrase:(rerun_count:'0'))),('$state':(store:appState),"
        "meta:(alias:!n,disabled:!f,index:'autoresult-*',key:environment,negate:!f,"
        "params:(query:GM2),type:phrase),query:(match_phrase:(environment:GM2)))),"
        f"query:(language:kuery,query:{kql_query}))"
    )
    return base


def shorten_dashboard_url(path):
    try:
        resp = requests.post(
            "https://autoinfra-es.vaultdev.com/_dashboards/api/shorten_url",
            json={"url": path},
            headers={
                "Content-Type": "application/json",
                "osd-xsrf": "true",
            },
            timeout=10,
            verify=False,
        )
        resp.raise_for_status()
        url_id = resp.json().get("urlId")
        if url_id:
            return f"https://autoinfra-es.vaultdev.com/_dashboards/goto/{url_id}"
    except Exception as e:
        warn(f"URL shortening failed ({type(e).__name__}): {e}")
    return f"https://autoinfra-es.vaultdev.com/_dashboards{path}"
