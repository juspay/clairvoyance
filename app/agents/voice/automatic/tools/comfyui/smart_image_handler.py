"""
Smart image handler for determining user intent and routing to appropriate image functions.
Analyzes user prompts to determine if they want to edit existing image or generate new one.
"""

import re
from typing import Any, Dict, Optional, Tuple

from pipecat.services.llm_service import FunctionCallParams

from app.agents.voice.automatic.tools.comfyui.image_generation import (
    edit_image_background,
    generate_advertisement_image,
    generate_custom_image,
    mask_and_edit_object,
)
from app.agents.voice.automatic.utils.image_context import (
    get_current_image,
    has_current_image,
)
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core.logger import logger

# Keywords that indicate editing existing image
EDIT_KEYWORDS = [
    "change",
    "edit",
    "modify",
    "update",
    "alter",
    "adjust",
    "make",
    "turn",
    "convert",
    "transform",
    "replace",
    "background",
    "color",
    "style",
    "add",
    "remove",
    "mask",
    "object",
    "subject",
    "item",
]

# Keywords that indicate background editing
BACKGROUND_KEYWORDS = [
    "background",
    "backdrop",
    "scene",
    "setting",
    "environment",
    "behind",
    "in the back",
    "surrounding",
]

# Keywords that indicate object editing
OBJECT_KEYWORDS = [
    "object",
    "subject",
    "item",
    "thing",
    "product",
    "person",
    "shoe",
    "shoes",
    "car",
    "building",
    "face",
    "hair",
    "color",
    "red",
    "blue",
    "green",
    "black",
    "white",
]

# Keywords that indicate new generation
GENERATE_KEYWORDS = [
    "create",
    "generate",
    "make",
    "build",
    "design",
    "new",
    "fresh",
    "another",
    "different",
    "advertisement",
    "ad",
]


def analyze_user_intent(prompt: str, session_id: str) -> Tuple[str, Dict[str, Any]]:
    """
    Analyze user prompt to determine intent and extract relevant parameters.

    Returns:
        Tuple of (intent, parameters) where intent is one of:
        - "generate_new": Create new image
        - "edit_background": Edit background of current image
        - "edit_object": Edit specific object in current image
        - "generate_advertisement": Create new advertisement
    """
    prompt_lower = prompt.lower()
    has_image = has_current_image(session_id)

    logger.debug(f"Analyzing intent for prompt: '{prompt}' (has_image: {has_image})")

    # If no current image, can only generate new
    if not has_image:
        if any(keyword in prompt_lower for keyword in ["advertisement", "ad"]):
            return "generate_advertisement", {"prompt": prompt}
        else:
            return "generate_new", {"prompt": prompt}

    # Check for explicit editing keywords
    has_edit_keywords = any(keyword in prompt_lower for keyword in EDIT_KEYWORDS)
    has_background_keywords = any(
        keyword in prompt_lower for keyword in BACKGROUND_KEYWORDS
    )
    has_object_keywords = any(keyword in prompt_lower for keyword in OBJECT_KEYWORDS)
    has_generate_keywords = any(
        keyword in prompt_lower for keyword in GENERATE_KEYWORDS
    )

    # Strong indicators for new generation
    if has_generate_keywords and ("new" in prompt_lower or "create" in prompt_lower):
        if any(keyword in prompt_lower for keyword in ["advertisement", "ad"]):
            return "generate_advertisement", {"prompt": prompt}
        else:
            return "generate_new", {"prompt": prompt}

    # Strong indicators for editing
    if has_edit_keywords:
        if has_background_keywords:
            # Extract background description
            background_desc = extract_background_description(prompt)
            return "edit_background", {
                "background_description": background_desc or prompt
            }
        elif has_object_keywords:
            # Extract object and edit instruction
            object_desc, edit_instruction = extract_object_edit_details(prompt)
            return "edit_object", {
                "object_description": object_desc,
                "edit_instruction": edit_instruction,
            }
        else:
            # General editing - try to determine what to edit
            if "background" in prompt_lower or "scene" in prompt_lower:
                background_desc = extract_background_description(prompt)
                return "edit_background", {
                    "background_description": background_desc or prompt
                }
            else:
                # Assume object editing
                return "edit_object", {
                    "object_description": "main subject",
                    "edit_instruction": prompt,
                }

    # Patterns that suggest editing without explicit keywords
    edit_patterns = [
        r"make.*red|blue|green|black|white",  # Color changes
        r"turn.*into",  # Transformation
        r"add.*to",  # Adding elements
        r"remove.*from",  # Removing elements
    ]

    for pattern in edit_patterns:
        if re.search(pattern, prompt_lower):
            return "edit_object", {
                "object_description": "main subject",
                "edit_instruction": prompt,
            }

    # Default to new generation if ambiguous
    if any(keyword in prompt_lower for keyword in ["advertisement", "ad"]):
        return "generate_advertisement", {"prompt": prompt}
    else:
        return "generate_new", {"prompt": prompt}


def extract_background_description(prompt: str) -> Optional[str]:
    """Extract background description from prompt."""
    prompt_lower = prompt.lower()

    # Patterns to extract background descriptions
    patterns = [
        r"background.*?to\s+([^,.!?]+)",  # "change background to beach"
        r"make.*?background\s+([^,.!?]+)",  # "make background ocean"
        r"set.*?background.*?to\s+([^,.!?]+)",  # "set background to mountain"
        r"with\s+([^,.!?]+)\s+background",  # "with beach background"
        r"background.*?([a-z\s]+)$",  # "change the background forest"
    ]

    for pattern in patterns:
        match = re.search(pattern, prompt_lower)
        if match:
            desc = match.group(1).strip()
            if len(desc) > 2:  # Avoid single words that might be noise
                return desc

    # If no specific pattern, look for words after "to" or "into"
    to_match = re.search(r"to\s+([^,.!?]+)", prompt_lower)
    if to_match:
        desc = to_match.group(1).strip()
        if len(desc) > 2:
            return desc

    return None


def extract_object_edit_details(prompt: str) -> Tuple[Optional[str], str]:
    """Extract object description and edit instruction from prompt."""
    prompt_lower = prompt.lower()

    # Common object patterns
    object_patterns = [
        r"(shoes?|shoe)",
        r"(car|vehicle)",
        r"(person|people|man|woman)",
        r"(product|item|object)",
        r"(building|house)",
        r"(face|hair|eyes)",
    ]

    object_desc = None
    for pattern in object_patterns:
        match = re.search(pattern, prompt_lower)
        if match:
            object_desc = match.group(1)
            break

    # If no specific object found, use generic
    if not object_desc:
        object_desc = "main subject"

    return object_desc, prompt


async def smart_image_handler(params: FunctionCallParams):
    """
    Smart handler that analyzes user intent and routes to appropriate image function.

    This is the main entry point for image-related requests that require context awareness.
    """
    try:
        # Extract the user's prompt
        prompt = params.arguments.get("prompt", "")
        if not prompt:
            await params.result_callback(
                {
                    "success": False,
                    "error": "Prompt is required for image operations.",
                    "image_urls": [],
                }
            )
            return

        # Get session context
        session_id = get_current_session_id()
        if not session_id:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No active session found.",
                    "image_urls": [],
                }
            )
            return

        # Analyze user intent
        intent, extracted_params = analyze_user_intent(prompt, session_id)

        logger.info(f"Smart handler determined intent: {intent} for prompt: '{prompt}'")
        logger.debug(f"Extracted parameters: {extracted_params}")

        # Create new params object with extracted parameters
        new_params = type(
            "MockParams",
            (),
            {"arguments": extracted_params, "result_callback": params.result_callback},
        )()

        # Route to appropriate function based on intent
        if intent == "generate_advertisement":
            # Add default advertisement parameters
            new_params.arguments.update(
                {
                    "product_type": extracted_params.get("product_type", "product"),
                    "style": extracted_params.get("style", "modern advertising"),
                }
            )
            await generate_advertisement_image(new_params)

        elif intent == "generate_new":
            await generate_custom_image(new_params)

        elif intent == "edit_background":
            await edit_image_background(new_params)

        elif intent == "edit_object":
            await mask_and_edit_object(new_params)

        else:
            # Fallback to custom image generation
            await generate_custom_image(new_params)

    except Exception as e:
        logger.error(f"Error in smart image handler: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to process image request: {str(e)}",
                "image_urls": [],
            }
        )
