import json
from typing import Any, Dict, List, Optional


def generation_rows(
    bot: Any, lead: Any, messages: List[Any], kb_header: Optional[str]
) -> List[Dict[str, str]]:
    nodes = (getattr(bot, "flow_config", None) or {}).get("nodes", {})
    template_id = getattr(getattr(bot, "template", None), "id", None)
    entered = [
        entry.get("node_name")
        for entry in (lead.metaData or {}).get("node_traversal", [])
        if entry.get("template_id") == template_id
    ]
    rows: List[Dict[str, str]] = []
    for node_name in dict.fromkeys(entered):
        role_message = (nodes.get(node_name) or {}).get("role_message")
        if role_message:
            rows.append({"role": "system", "content": role_message})

    for msg in messages:
        if not (
            isinstance(msg, dict)
            and "role" in msg
            and "content" in msg
            and isinstance(msg["content"], str)
        ):
            continue
        role = "system" if msg["role"] == "developer" else msg["role"]
        if kb_header and role == "system" and msg["content"].startswith(kb_header):
            continue
        if role == "tool" and msg["content"] == "COMPLETED":
            continue
        try:
            parsed = json.loads(msg["content"])
            if isinstance(parsed, dict) and parsed.get("type") == "async_tool":
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        rows.append({"role": role, "content": msg["content"]})
    return rows
