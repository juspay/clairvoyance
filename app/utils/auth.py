from app.core.config import WRITE_ACTIONS_AUTHORIZED_USERS

def is_user_authorized(email: str) -> bool:

    if not WRITE_ACTIONS_AUTHORIZED_USERS:
        return True

    if not email:
        return False
    
    return email in WRITE_ACTIONS_AUTHORIZED_USERS

unauthorized_error_message = "User is not authorized to perform this action."