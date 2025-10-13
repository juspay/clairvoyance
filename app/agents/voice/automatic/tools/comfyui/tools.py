"""
ComfyUI tool definitions for the automatic agent.
"""

from pipecat.adapters.schemas.function_schema import FunctionSchema

# Standard ComfyUI tools available to the agent
generate_advertisement_image_function = FunctionSchema(
    name="generate_advertisement_image",
    description="Generate an advertisement image using ComfyUI based on a text description. Perfect for creating marketing materials, product ads, and promotional content.",
    properties={
        "prompt": {
            "type": "string",
            "description": "Detailed description of the advertisement to generate. Include product details, style preferences, and any specific requirements.",
        },
        "product_type": {
            "type": "string",
            "description": "Type of product being advertised (e.g., 'shoes', 'clothing', 'electronics', 'food', etc.)",
        },
        "style": {
            "type": "string",
            "description": "Advertisement style (e.g., 'modern', 'vintage', 'minimalist', 'luxury', 'casual')",
        },
        "width": {
            "type": "integer",
            "description": "Image width in pixels",
        },
        "height": {
            "type": "integer",
            "description": "Image height in pixels",
        },
    },
    required=["prompt"],
)

generate_custom_image_function = FunctionSchema(
    name="generate_custom_image",
    description="Generate a custom image using ComfyUI with advanced parameters. Use this for general image generation beyond advertisements.",
    properties={
        "prompt": {
            "type": "string",
            "description": "Detailed text description of the image to generate",
        },
        "negative_prompt": {
            "type": "string",
            "description": "Things to avoid in the generated image",
        },
        "width": {
            "type": "integer",
            "description": "Image width in pixels",
        },
        "height": {
            "type": "integer",
            "description": "Image height in pixels",
        },
        "steps": {
            "type": "integer",
            "description": "Number of denoising steps (higher = better quality, slower)",
        },
        "cfg": {
            "type": "number",
            "description": "Classifier-free guidance scale (how closely to follow prompt)",
        },
        "sampler_name": {
            "type": "string",
            "description": "Sampling method",
        },
    },
    required=["prompt"],
)

mask_and_edit_object_function = FunctionSchema(
    name="mask_and_edit_object",
    description="Mask and edit specific objects in the current working image. Supports intelligent scaling - can make objects smaller/bigger and adjust backgrounds accordingly. Use when user wants to change, modify, resize, or replace specific objects while keeping the rest unchanged. CRITICAL: Extract parameters ONLY from the user's current request, ignore previous conversation context.",
    properties={
        "object_description": {
            "type": "string",
            "description": "ONLY from current user message: The specific object the user wants to mask/edit (e.g., 'bottle', 'logo', 'text', 'background'). If user says 'mask the bottle and change background', object_description should be 'background'.",
        },
        "edit_instruction": {
            "type": "string",
            "description": "ONLY from current user message: The exact changes the user requested for the masked object. Supports size changes like 'make smaller', 'make bigger', 'reduce size', 'enlarge', 'zoom out to show more background', etc. Examples: 'mountain landscape with sunset', 'change to blue color', 'make smaller and show more background', 'enlarge and fill the frame', 'farm land'. Do NOT mix with previous advertisement details or conversation context.",
        },
    },
    required=["object_description", "edit_instruction"],
)

edit_image_background_function = FunctionSchema(
    name="edit_image_background",
    description="Edit or replace the background of the current working image while preserving the main subject. Use this when user wants to change only the background setting or environment.",
    properties={
        "background_description": {
            "type": "string",
            "description": "Description of the new background to create (e.g., 'modern kitchen', 'farmers field with sunrise', 'office setting')",
        },
        "preserve_subject": {
            "type": "boolean",
            "description": "Whether to preserve the main subject in the image (default: true)",
        },
    },
    required=["background_description"],
)

add_to_current_image_function = FunctionSchema(
    name="add_to_current_image",
    description="Add new elements to the current working image. Use this when user wants to add objects, text, or elements to an existing image without replacing anything.",
    properties={
        "addition_description": {
            "type": "string",
            "description": "Description of what to add to the image (e.g., 'add a tagline saying Hello World', 'add decorative elements', 'add more products')",
        },
        "placement": {
            "type": "string",
            "description": "Where to place the addition (e.g., 'bottom center', 'top right', 'around the main object')",
        },
    },
    required=["addition_description"],
)

smart_image_handler_function = FunctionSchema(
    name="smart_image_handler",
    description="Intelligent image processing that automatically determines the best approach for complex image editing requests. Use this for ambiguous requests that could involve multiple types of edits or when you're unsure which specific function to use.",
    properties={
        "user_request": {
            "type": "string",
            "description": "The full user request describing what they want to do with the image",
        },
        "context": {
            "type": "string",
            "description": "Additional context about the current image or previous operations",
        },
    },
    required=["user_request"],
)

generate_advertisement_with_logo_function = FunctionSchema(
    name="generate_advertisement_with_logo",
    description="Generate an advertisement image with a brand logo overlay. Use this when user has uploaded a logo and wants it included in the advertisement. This function handles logo upload workflow automatically.",
    properties={
        "prompt": {
            "type": "string",
            "description": "Detailed description of the advertisement to generate",
        },
        "product_type": {
            "type": "string",
            "description": "Type of product being advertised",
        },
        "style": {
            "type": "string",
            "description": "Advertisement style",
        },
        "logo_position": {
            "type": "string",
            "description": "Where to place the logo (e.g., 'bottom right', 'top left', 'center')",
        },
        "width": {
            "type": "integer",
            "description": "Image width in pixels",
        },
        "height": {
            "type": "integer",
            "description": "Image height in pixels",
        },
    },
    required=["prompt"],
)

upload_user_image_function = FunctionSchema(
    name="upload_user_image",
    description="Allow user to upload their own product image (like a bottle, package, etc.) for editing. Use this when user says they have their own image they want to work with, or when they want to upload a photo of their product to edit or enhance.",
    properties={
        "image_description": {
            "type": "string",
            "description": "Description of what the user wants to upload (e.g., 'ghee bottle', 'product photo', 'bottle image')",
        },
        "next_action": {
            "type": "string",
            "description": "What the user wants to do after uploading (e.g., 'edit', 'add logo', 'change background', 'mask and edit')",
        },
    },
    required=["image_description"],
)

process_uploaded_image_function = FunctionSchema(
    name="process_uploaded_image",
    description="Process and set user's uploaded image as the current working image. This is automatically called after user uploads their image through the upload interface.",
    properties={
        "image_url": {
            "type": "string",
            "description": "URL of the uploaded image",
        },
        "image_description": {
            "type": "string",
            "description": "Description of the uploaded image",
        },
        "next_action": {
            "type": "string",
            "description": "What the user wants to do with the uploaded image",
        },
    },
    required=["image_url"],
)

standard_tools = [
    generate_advertisement_image_function,
    generate_custom_image_function,
    generate_advertisement_with_logo_function,
    mask_and_edit_object_function,
    # edit_image_background_function,
    add_to_current_image_function,
    # smart_image_handler_function,
    upload_user_image_function,
    process_uploaded_image_function,
]
