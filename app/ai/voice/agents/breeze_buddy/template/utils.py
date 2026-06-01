"""Pure helpers shared between the voice template loader and the chat agent."""

from typing import Dict


def render_messages_with_vars(messages: list, variables: Dict[str, str]) -> list:
    """Replace ``{key}`` placeholders in each message's ``content`` from
    ``variables``. Pure function — used by both the voice loader and the
    chat agent so the substitution logic stays in one place.
    """
    rendered_messages = []
    for message in messages:
        if isinstance(message, dict) and "content" in message:
            content = message["content"]
            for key, value in variables.items():
                placeholder = f"{{{key}}}"
                content = content.replace(placeholder, str(value))
            rendered_message = message.copy()
            rendered_message["content"] = content
            rendered_messages.append(rendered_message)
        else:
            rendered_messages.append(message)
    return rendered_messages
