"""
Email tool functions for LLM integration.
"""

from typing import Any, Dict, List

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.agents.voice.automatic.features.charts.session_storage import (
    get_session_storage,
)
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core import config
from app.core.logger import logger

from .email_service import get_email_service

# Email function schema
email_images_function = FunctionSchema(
    name="email_images",
    description="Send specified images via email to the configured recipient",
    properties={
        "image_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of images to email (e.g., [0, 1] for images 1 and 2)",
        },
        "subject": {
            "type": "string",
            "description": "Optional custom email subject line",
        },
        "message": {
            "type": "string",
            "description": "Optional custom message to include in email body",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=["image_indices"],
)

# Test email connection function
test_email_connection_function = FunctionSchema(
    name="test_email_connection",
    description="Test the email SMTP connection and configuration",
    properties={},
    required=[],
)


async def email_images(params: FunctionCallParams):
    """
    Send specified images via email.

    Args:
        params: Function call parameters containing image_indices, subject, message, session_id
    """
    try:
        # Check if email is enabled
        if not config.ENABLE_EMAIL:
            await params.result_callback("Email functionality is disabled")
            return

        # Extract parameters
        image_indices = params.arguments.get("image_indices", [])
        subject = params.arguments.get("subject")
        message = params.arguments.get("message")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not image_indices:
            await params.result_callback("Error: No image indices provided")
            return

        if not session_id:
            await params.result_callback("Error: No session ID available")
            return

        logger.info(
            f"EmailTool: Processing email request for images {image_indices} in session {session_id}"
        )

        # Get session storage and validate indices
        storage = get_session_storage()
        image_count = storage.get_image_count(session_id)

        if image_count == 0:
            await params.result_callback("Error: No images available in this session")
            return

        invalid_indices = [i for i in image_indices if i < 0 or i >= image_count]
        if invalid_indices:
            await params.result_callback(
                f"Error: Image indices {invalid_indices} are invalid. Available images: 0-{image_count-1}"
            )
            return

        # Get images by indices
        images = storage.get_images_by_indices(session_id, image_indices)

        if not images:
            await params.result_callback(
                "Error: Could not retrieve images for emailing"
            )
            return

        logger.info(f"EmailTool: Retrieved {len(images)} images for emailing")

        # Send email
        email_service = get_email_service()
        success = await email_service.send_images_email(
            images=images, subject=subject, custom_message=message
        )

        if success:
            # Get image titles for response
            image_titles = []
            for image in images:
                title = image.get("nav_title", "Generated Image")
                image_titles.append(title)

            response_message = (
                f"Successfully emailed {len(images)} image(s) to {config.SMTP_TO_EMAIL}: "
                f"{', '.join(image_titles)}"
            )
            await params.result_callback(response_message)
            logger.info(f"EmailTool: {response_message}")
        else:
            error_message = (
                "Failed to send email. Please check email configuration and try again."
            )
            await params.result_callback(error_message)
            logger.error(f"EmailTool: {error_message}")

    except Exception as e:
        error_message = f"Error sending email: {str(e)}"
        logger.error(f"EmailTool: {error_message}")
        await params.result_callback(error_message)


async def test_email_connection(params: FunctionCallParams):
    """
    Test email SMTP connection and configuration.

    Args:
        params: Function call parameters (none required)
    """
    try:
        # Check if email is enabled
        if not config.ENABLE_EMAIL:
            await params.result_callback("Email functionality is disabled")
            return

        logger.info("EmailTool: Testing email connection...")

        # Test SMTP connection
        email_service = get_email_service()
        success = await email_service.test_connection()

        if success:
            response_message = (
                f"Email connection test successful! "
                f"SMTP server: {config.SMTP_SERVER}:{config.SMTP_PORT}, "
                f"From: {config.SMTP_FROM_EMAIL}, "
                f"To: {config.SMTP_TO_EMAIL}"
            )
            await params.result_callback(response_message)
            logger.info(f"EmailTool: {response_message}")
        else:
            error_message = (
                f"Email connection test failed. "
                f"Please check SMTP configuration: {config.SMTP_SERVER}:{config.SMTP_PORT}"
            )
            await params.result_callback(error_message)
            logger.error(f"EmailTool: {error_message}")

    except Exception as e:
        error_message = f"Error testing email connection: {str(e)}"
        logger.error(f"EmailTool: {error_message}")
        await params.result_callback(error_message)


# Standard tools list for email functionality
standard_tools = [
    email_images_function,
    test_email_connection_function,
]

# Tool functions mapping for LLM integration
tool_functions = {
    "email_images": email_images,
    "test_email_connection": test_email_connection,
}


# Create tools object for easy import
class EmailTools:
    standard_tools = standard_tools


tools = EmailTools()
