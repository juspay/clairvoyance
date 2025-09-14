import base64
from urllib.parse import urlencode
from app.core import config


def generate_open_observer_url_for_session_id(session_id: str) -> str:
    """Generate OpenObserve URL for a specific session ID"""
    query_string = f"sessionid='{session_id}'"
    encoded_query = base64.b64encode(query_string.encode("utf-8")).decode("utf-8")
    base_url = f"{config.OPEN_OBSERVE_BASE_URL}/web/logs"

    params = {
        "stream_type": "logs",
        "stream": "clairvoyance_logs",
        "period": "1d",
        "refresh": "0",
        "sql_mode": "false",
        "query": encoded_query,
        "defined_schemas": "user_defined_schema",
        "org_identifier": "default",
        "quick_mode": "false",
        "show_histogram": "true",
    }

    return f"{base_url}?{urlencode(params)}"
