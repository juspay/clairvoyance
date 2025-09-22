import datetime


def process_langfuse_template_variables(prompt_content: str) -> str:
    """
    Replace template variables in the prompt with actual values.

    Args:
        prompt_content: The prompt content with template variables

    Returns:
        str: Prompt content with template variables replaced
    """
    # Replace {current_time} with actual current date
    current_time = datetime.datetime.now().strftime("%B %d, %Y")
    prompt_content = prompt_content.replace("{current_time}", current_time)

    return prompt_content
