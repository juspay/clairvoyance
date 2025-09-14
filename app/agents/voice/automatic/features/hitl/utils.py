from app.core.config import HITL_ACTIONS


def get_hitl_actions():
    return HITL_ACTIONS


def is_dangerous_operation(function_name: str) -> bool:
    hitl_actions = get_hitl_actions()
    for action in hitl_actions:
        if action in function_name.lower():
            return True
    return False


def extract_entity_name(function_name: str) -> str:
    for prefix in ["update_", "create_", "delete_", "update", "create", "delete"]:
        if function_name.startswith(prefix):
            entity = function_name[len(prefix) :]
            break
    else:
        entity = function_name
    return entity.replace("_", " ").title()


def get_action_description(function_name: str) -> str:
    if function_name.startswith("update"):
        return f"Update {extract_entity_name(function_name)}"
    elif function_name.startswith("create"):
        return f"Create {extract_entity_name(function_name)}"
    elif function_name.startswith("delete"):
        return f"Delete {extract_entity_name(function_name)}"
    else:
        return function_name.replace("_", " ").title()


def generate_success_message(function_name: str, arguments: dict) -> str:
    operation_type = None
    entity_name = None
    if function_name.startswith("create"):
        operation_type = "created"
        entity_name = function_name[6:].replace("_", " ").strip()
    elif function_name.startswith("update"):
        operation_type = "updated"
        entity_name = function_name[6:].replace("_", " ").strip()
    elif function_name.startswith("delete"):
        operation_type = "deleted"
        entity_name = function_name[6:].replace("_", " ").strip()
    else:
        action_value = (
            arguments.get("action", "").lower() if isinstance(arguments, dict) else ""
        )
        if action_value in ["create", "update", "delete", "pause"]:
            operation_type = action_value + "d" if action_value != "pause" else "paused"
            entity_name = arguments.get("type", "item").replace("_", " ")
    if operation_type and entity_name:
        return f"Operation completed successfully. The {entity_name} has been {operation_type} as requested."
    elif operation_type:
        return f"Operation completed successfully. The item has been {operation_type} as requested."
    else:
        return "Operation completed successfully."
