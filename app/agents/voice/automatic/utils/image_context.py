"""
Image session context management for iterative image editing workflows.
Provides persistent storage of working images across conversation turns.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.logger import logger

# Directory for storing image session contexts
IMAGE_CONTEXT_DIR = Path("temp/image_sessions")
IMAGE_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ImageEditStep:
    """Represents a single editing step in the image history."""

    step_id: str
    image_url: str
    operation: str  # "generate", "edit_background", "mask_object", etc.
    prompt: str
    timestamp: str
    parameters: Dict[str, Any]


@dataclass
class ImageSessionContext:
    """Persistent context for image editing session."""

    session_id: str
    current_image_url: Optional[str] = None
    logo_url: Optional[str] = None
    editing_history: List[ImageEditStep] = None
    created_at: str = None
    updated_at: str = None

    def __post_init__(self):
        if self.editing_history is None:
            self.editing_history = []
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
        if self.updated_at is None:
            self.updated_at = self.created_at


def _get_context_file_path(session_id: str) -> Path:
    """Get the file path for storing image context."""
    return IMAGE_CONTEXT_DIR / f"image_context_{session_id}.json"


def store_image_context(context: ImageSessionContext) -> None:
    """Store image context to file."""
    try:
        context.updated_at = datetime.now().isoformat()
        context_file = _get_context_file_path(context.session_id)

        with open(context_file, "w") as f:
            json.dump(asdict(context), f, indent=2)

        logger.info(f"Stored image context for session {context.session_id}")
    except Exception as e:
        logger.error(
            f"Failed to store image context for session {context.session_id}: {e}"
        )


def get_image_context(session_id: str) -> Optional[ImageSessionContext]:
    """Retrieve image context from file."""
    try:
        context_file = _get_context_file_path(session_id)

        if not context_file.exists():
            logger.debug(f"No image context found for session {session_id}")
            return None

        with open(context_file, "r") as f:
            data = json.load(f)

        # Convert dict back to dataclass
        # Handle the editing_history list of dicts -> list of ImageEditStep
        if "editing_history" in data and data["editing_history"]:
            data["editing_history"] = [
                ImageEditStep(**step) for step in data["editing_history"]
            ]

        context = ImageSessionContext(**data)
        logger.debug(f"Retrieved image context for session {session_id}")
        return context

    except Exception as e:
        logger.error(f"Failed to retrieve image context for session {session_id}: {e}")
        return None


def get_or_create_image_context(session_id: str) -> ImageSessionContext:
    """Get existing context or create new one."""
    context = get_image_context(session_id)
    if context is None:
        context = ImageSessionContext(session_id=session_id)
        store_image_context(context)
        logger.info(f"Created new image context for session {session_id}")
    return context


def set_current_image(
    session_id: str,
    image_url: str,
    operation: str,
    prompt: str,
    parameters: Dict[str, Any] = None,
) -> None:
    """Set the current working image and add to editing history."""
    try:
        context = get_or_create_image_context(session_id)

        # Update current image
        context.current_image_url = image_url

        # Add to editing history
        step = ImageEditStep(
            step_id=str(uuid.uuid4()),
            image_url=image_url,
            operation=operation,
            prompt=prompt,
            timestamp=datetime.now().isoformat(),
            parameters=parameters or {},
        )
        context.editing_history.append(step)

        # Store updated context
        store_image_context(context)

        logger.info(
            f"Set current image for session {session_id}: {image_url} (operation: {operation})"
        )

    except Exception as e:
        logger.error(f"Failed to set current image for session {session_id}: {e}")


def get_current_image(session_id: str) -> Optional[str]:
    """Get the current working image URL."""
    try:
        context = get_image_context(session_id)
        if context and context.current_image_url:
            logger.debug(
                f"Retrieved current image for session {session_id}: {context.current_image_url}"
            )
            return context.current_image_url
        else:
            logger.debug(f"No current image found for session {session_id}")
            return None
    except Exception as e:
        logger.error(f"Failed to get current image for session {session_id}: {e}")
        return None


def set_logo_url(session_id: str, logo_url: str) -> None:
    """Set the logo URL for the session."""
    try:
        context = get_or_create_image_context(session_id)
        context.logo_url = logo_url
        store_image_context(context)
        logger.info(f"Set logo URL for session {session_id}: {logo_url}")
    except Exception as e:
        logger.error(f"Failed to set logo URL for session {session_id}: {e}")


def get_logo_url(session_id: str) -> Optional[str]:
    """Get the logo URL for the session."""
    try:
        context = get_image_context(session_id)
        if context and context.logo_url:
            logger.debug(
                f"Retrieved logo URL for session {session_id}: {context.logo_url}"
            )
            return context.logo_url
        else:
            logger.debug(f"No logo URL found for session {session_id}")
            return None
    except Exception as e:
        logger.error(f"Failed to get logo URL for session {session_id}: {e}")
        return None


def get_editing_history(session_id: str) -> List[ImageEditStep]:
    """Get the complete editing history for a session."""
    try:
        context = get_image_context(session_id)
        if context and context.editing_history:
            logger.debug(
                f"Retrieved {len(context.editing_history)} editing steps for session {session_id}"
            )
            return context.editing_history
        else:
            logger.debug(f"No editing history found for session {session_id}")
            return []
    except Exception as e:
        logger.error(f"Failed to get editing history for session {session_id}: {e}")
        return []


def clear_image_context(session_id: str) -> None:
    """Clear the image context for a session."""
    try:
        context_file = _get_context_file_path(session_id)
        if context_file.exists():
            context_file.unlink()
            logger.info(f"Cleared image context for session {session_id}")
        else:
            logger.debug(f"No image context to clear for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to clear image context for session {session_id}: {e}")


def has_current_image(session_id: str) -> bool:
    """Check if session has a current working image."""
    return get_current_image(session_id) is not None


def get_previous_image(session_id: str, steps_back: int = 1) -> Optional[str]:
    """Get a previous image from the editing history."""
    try:
        history = get_editing_history(session_id)
        if len(history) >= steps_back + 1:
            # Get the image from steps_back positions ago
            previous_step = history[-(steps_back + 1)]
            logger.debug(
                f"Retrieved previous image ({steps_back} steps back) for session {session_id}: {previous_step.image_url}"
            )
            return previous_step.image_url
        else:
            logger.debug(
                f"Not enough history to go back {steps_back} steps for session {session_id}"
            )
            return None
    except Exception as e:
        logger.error(f"Failed to get previous image for session {session_id}: {e}")
        return None
